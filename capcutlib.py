"""capcutlib - build CapCut desktop drafts by cloning a template project.

Why a template? CapCut 9.x validates drafts against its own format markers and
rejects drafts built from stale schemas ("This draft comes from an unconventional
path and cannot be used"). The reliable approach (used by every working community
server) is to clone the JSON shapes of a project the installed CapCut build made
itself, then swap materials/timings.

The template draft must:
  * live in CapCut's drafts folder,
  * contain the string "template" in its folder name (or set CAPCUT_TEMPLATE),
  * contain at least one video clip and one text clip.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from subs import parse_subs  # noqa: E402

ENV_ROOT = 'CAPCUT_DRAFTS_ROOT'
ENV_TEMPLATE = 'CAPCUT_TEMPLATE'
CONTENT_NAMES = ('draft_content.json', 'draft_info.json')
BACKUP_DIR = '.mcp_backups'


# ---------------------------------------------------------------- basics

def uid():
    # CapCut writes ids as uppercase dashed GUIDs; matching that keeps the
    # draft consistent with everything the app itself creates.
    return str(uuid.uuid4()).upper()


def now_us():
    return int(time.time() * 1_000_000)


def posix(path):
    return str(path).replace('\\', '/')


def default_root():
    return os.path.join(
        os.environ.get('LOCALAPPDATA', ''),
        'CapCut', 'User Data', 'Projects', 'com.lveditor.draft')


def drafts_root(root=None):
    return os.path.abspath(root or os.environ.get(ENV_ROOT) or default_root())


def read_json(path):
    with open(path, encoding='utf-8-sig') as f:
        return json.load(f)


def _backup(path):
    bdir = os.path.join(os.path.dirname(path), BACKUP_DIR)
    os.makedirs(bdir, exist_ok=True)
    dst = os.path.join(bdir, time.strftime('%Y%m%d_%H%M%S_') + os.path.basename(path))
    shutil.copy2(path, dst)
    for old in sorted(os.listdir(bdir))[:-40]:
        try:
            os.remove(os.path.join(bdir, old))
        except OSError:
            pass


def write_json(path, obj):
    if os.path.exists(path):
        _backup(path)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)


def blank_keyframes(seg):
    """CapCut requires these arrays on every segment; omitting them crashes it."""
    seg['common_keyframes'] = []
    seg['keyframe_refs'] = []
    return seg


def sync_backup(path, obj):
    """Keep draft_content.json.bak identical to the content we just wrote."""
    bak = path + '.bak'
    if os.path.isdir(os.path.dirname(bak)):
        with open(bak, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))


def set_timeline_layout(folder, draft_id):
    """Point timeline_layout.json at this draft's timeline id."""
    path = os.path.join(folder, 'timeline_layout.json')
    if not os.path.isfile(path):
        return
    try:
        layout = read_json(path)
    except Exception:
        return
    for item in layout.get('dockItems') or []:
        if isinstance(item, dict):
            item['timelineIds'] = [draft_id]
            item['timelineNames'] = ['Timeline 01']
    write_json(path, layout)


SNAP_FPS = 30


def snap_us(us, fps=SNAP_FPS):
    """Snap a microsecond value to the nearest frame boundary.

    CapCut lays out clips at a fixed fps: ranges between frames show on the
    timeline but never render, so every range must land on the grid.
    """
    return int(round(round(us * fps / 1_000_000) * 1_000_000 / fps))


def find_multi_clip_donor(root=None):
    """Find a project CapCut itself built with a real per-clip material graph.

    Cloning the one-clip template cannot express a multi-clip timeline; the
    per-segment speed/placeholder/canvas/sound/vocal refs and the parallel
    audio track only exist in a project CapCut created from many clips.
    """
    root = drafts_root(root)
    explicit = os.environ.get('CAPCUT_DONOR')
    best = None
    names = ([explicit] if explicit and os.path.isdir(os.path.join(root, explicit))
             else []) + (sorted(os.listdir(root)) if os.path.isdir(root) else [])
    for name in names:
        folder = name if os.path.isabs(name) else os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        cp = content_path(folder)
        if not cp:
            continue
        try:
            content = read_json(cp)
        except Exception:
            continue
        vtrack = find_track(content, 'video')
        atrack = find_track(content, 'audio')
        if not vtrack or not atrack:
            continue
        vsegs = vtrack.get('segments') or []
        asegs = atrack.get('segments') or []
        if len(vsegs) < 2 or len(asegs) < 2:
            continue
        # needs per-segment support refs (speed/canvas/...), not an empty list
        if not all(s.get('extra_material_refs') for s in vsegs[:2]):
            continue
        score = min(len(vsegs), len(asegs))
        if best is None or score > best['segments']:
            best = {'folder': folder, 'name': os.path.basename(folder),
                    'segments': score, 'content_path': cp, 'content': content}
    return best


def capcut_running():
    try:
        out = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq CapCut.exe', '/NH'],
            capture_output=True, text=True, timeout=15)
        return 'capcut.exe' in out.stdout.lower()
    except Exception:
        return False


def capcut_version():
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for sub in (r'Software\Microsoft\Windows\CurrentVersion\Uninstall',
                    r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
            try:
                key = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        child = winreg.EnumKey(key, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(key, child) as sk:
                            name = winreg.QueryValueEx(sk, 'DisplayName')[0]
                            if 'capcut' in str(name).lower():
                                try:
                                    ver = winreg.QueryValueEx(sk, 'DisplayVersion')[0]
                                except OSError:
                                    ver = ''
                                return {'name': name, 'version': ver}
                    except OSError:
                        continue
    return None


def ffprobe_media(path):
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-print_format', 'json',
             '-show_format', '-show_streams', path],
            capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        raise RuntimeError('ffprobe not found on PATH (install ffmpeg)')
    if out.returncode != 0:
        raise RuntimeError(f'ffprobe failed: {out.stderr.strip()[:300]}')
    data = json.loads(out.stdout)
    fmt = data.get('format') or {}
    streams = data.get('streams') or []
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    duration = float(fmt.get('duration') or (video or {}).get('duration') or 0)
    return {
        'duration': duration,
        'width': int(video['width']) if video and video.get('width') else None,
        'height': int(video['height']) if video and video.get('height') else None,
        'has_audio': any(s.get('codec_type') == 'audio' for s in streams),
    }


# ---------------------------------------------------------------- lookups

def content_path(folder):
    for name in CONTENT_NAMES:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return None


def track_type(track):
    return str(track.get('type') or track.get('flag') or '').lower()


def _materials(content):
    mats = content.get('materials')
    return mats if isinstance(mats, dict) else {}


def find_material(content, material_id):
    if not material_id:
        return None
    mats = _materials(content)
    for value in mats.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get('id') == material_id:
                    return item
    return None


def find_track(content, kind):
    tracks = content.get('tracks') or []
    for track in tracks:
        if track_type(track) == kind:
            return track
    # fallback: infer from the material type of the first segment
    key = {'video': 'videos', 'text': 'texts', 'audio': 'audios'}.get(kind)
    if key:
        ids = {m.get('id') for m in _materials(content).get(key, []) if isinstance(m, dict)}
        for track in tracks:
            for seg in track.get('segments') or []:
                if seg.get('material_id') in ids:
                    return track
    return None


def set_text_material(material, text, size=None):
    raw = material.get('content')
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:
            data = {'text': raw}
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        data = {'text': str(raw or '')}
    data['text'] = text
    for style in data.get('styles') or []:
        style['range'] = [0, len(text)]
        if size is not None and 'size' in style:
            style['size'] = size
    material['content'] = json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def _deoverlap(cues, gap=0.02):
    """Sort cues and trim ends so no two cues overlap on the same track."""
    cues = sorted(cues, key=lambda c: c['start'])
    out = []
    for i, cue in enumerate(cues):
        cue = dict(cue)
        if i + 1 < len(cues):
            limit = cues[i + 1]['start'] - gap
            if cue['end'] > limit:
                cue['end'] = limit
        if cue['end'] - cue['start'] >= 0.08:
            out.append(cue)
    return out


def set_canvas(content, width, height):
    canvas = content.get('canvas_config')
    if isinstance(canvas, dict):
        canvas['width'] = width
        canvas['height'] = height
        if 'ratio' in canvas:
            canvas['ratio'] = '16:9' if width >= height else '9:16'
    config = content.get('config')
    if isinstance(config, dict):
        c2 = config.get('canvas_config')
        if isinstance(c2, dict):
            c2['width'] = width
            c2['height'] = height


def list_drafts(root=None):
    root = drafts_root(root)
    drafts = []
    if not os.path.isdir(root):
        return drafts
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        if not os.path.isdir(folder) or not content_path(folder):
            continue
        item = {'folder_name': name, 'folder': folder}
        try:
            meta = read_json(os.path.join(folder, 'draft_meta_info.json'))
            item['draft_name'] = meta.get('draft_name') or name
            item['duration_sec'] = round((meta.get('tm_duration') or 0) / 1e6, 2)
        except Exception:
            item['draft_name'] = name
        drafts.append(item)
    return drafts


def find_draft(name, root=None):
    wanted = (name or '').strip().lower()
    drafts = list_drafts(root)
    for item in drafts:
        if item['folder_name'].lower() == wanted or item['draft_name'].lower() == wanted:
            return item
    for item in drafts:
        if wanted and wanted in item['draft_name'].lower():
            return item
    raise RuntimeError(f'CapCut draft not found: {name!r}')


def find_template(root=None):
    root = drafts_root(root)
    explicit = os.environ.get(ENV_TEMPLATE)
    candidates = []
    if explicit:
        folder = explicit if os.path.isdir(explicit) else os.path.join(root, explicit)
        if os.path.isdir(folder):
            candidates.append(folder)
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            folder = os.path.join(root, name)
            if os.path.isdir(folder) and 'template' in name.lower():
                candidates.append(folder)
    for folder in candidates:
        cp = content_path(folder)
        if not cp:
            continue
        try:
            content = read_json(cp)
        except Exception:
            continue
        if find_track(content, 'video'):
            meta_name = os.path.basename(folder)
            try:
                meta = read_json(os.path.join(folder, 'draft_meta_info.json'))
                meta_name = meta.get('draft_name') or meta_name
            except Exception:
                pass
            return {'folder': folder, 'name': os.path.basename(folder),
                    'draft_name': meta_name, 'content_path': cp, 'content': content}
    return None


def find_text_donor(root=None):
    """Find a text segment + material in any draft, to style captions/titles.

    The one-clip template is allowed to have no text clip (CapCut 9.x projects
    sometimes lose it). In that case any real draft's text style is a better
    source than refusing to build.
    """
    for item in list_drafts(root):
        folder = item['folder']
        if not content_path(folder):
            continue
        try:
            content = read_json(content_path(folder))
        except Exception:
            continue
        for track in content.get('tracks') or []:
            if track_type(track) != 'text' or not track.get('segments'):
                continue
            seg = track['segments'][0]
            mat = find_material(content, seg.get('material_id'))
            if mat:
                return {'segment': deepcopy(seg), 'material': deepcopy(mat),
                        'folder': folder, 'draft_name': item.get('draft_name')}
    return None


# ---------------------------------------------------------------- validation

def validate(content, video_path=None):
    errors = []
    seen = {}
    for track in content.get('tracks') or []:
        tid = track.get('id')
        if tid:
            seen.setdefault(tid, []).append('track')
        spans = []
        for seg in track.get('segments') or []:
            sid = seg.get('id')
            if sid:
                seen.setdefault(sid, []).append('segment')
            tr = seg.get('target_timerange') or {}
            start, dur = tr.get('start'), tr.get('duration')
            if start is None or dur is None or start < 0 or dur <= 0:
                errors.append(f'bad target_timerange on segment {sid}')
            else:
                spans.append((start, start + dur, sid))
            mid = seg.get('material_id')
            if mid and not find_material(content, mid):
                errors.append(f'segment {sid} references missing material {mid}')
            for key in ('common_keyframes', 'keyframe_refs'):
                if not isinstance(seg.get(key), list):
                    errors.append(f'segment {sid} needs a {key} array (CapCut crashes without it)')
        spans.sort()
        for i in range(1, len(spans)):
            if spans[i][0] < spans[i - 1][1] - 1000:
                errors.append(f'overlap on track {tid}: {spans[i - 1][2]} / {spans[i][2]}')
    dup = [k for k, v in seen.items() if len(v) > 1]
    if dup:
        errors.append(f'duplicate ids: {dup[:5]}')
    if video_path and not os.path.isfile(video_path):
        errors.append(f'video not found: {video_path}')
    return errors


def _prune_unused(content, keys=('videos', 'texts', 'audios', 'stickers', 'images')):
    referenced = set()
    for track in content.get('tracks') or []:
        for seg in track.get('segments') or []:
            if seg.get('material_id'):
                referenced.add(seg['material_id'])
            for ref in seg.get('extra_material_refs') or []:
                referenced.add(ref)
    mats = _materials(content)
    for key in keys:
        arr = mats.get(key)
        if isinstance(arr, list):
            mats[key] = [m for m in arr
                         if not isinstance(m, dict) or not m.get('id') or m['id'] in referenced]


# ---------------------------------------------------------------- build

def make_clip_draft(name, video, start, end, subs=None, canvas='16:9',
                    title=None, subs_offset=0.0, root=None):
    """Create a CapCut draft: one trimmed video + captions + optional title."""
    root = drafts_root(root)
    if capcut_running():
        raise RuntimeError('CapCut is running. Close CapCut fully, then retry '
                           '(it autosaves over agent-written drafts).')
    if not os.path.isfile(video):
        raise RuntimeError(f'video not found: {video}')
    start_s, end_s = float(start), float(end)
    dur_s = end_s - start_s
    if dur_s <= 0:
        raise RuntimeError('end must be after start')

    tpl = find_template(root)
    if not tpl:
        raise RuntimeError(
            "No CapCut template draft found. In CapCut: create a new project named "
            "'template', add any video clip and one text clip (place the text where you "
            "want captions), save, and close CapCut. The template lets the server clone "
            "your installed CapCut build's exact JSON format.")

    probe = ffprobe_media(video)
    if probe['duration'] and end_s > probe['duration'] + 0.5:
        raise RuntimeError(f'end ({end_s}s) is past the video duration '
                           f'({probe["duration"]:.2f}s)')
    clip_us = int(round(dur_s * 1_000_000))
    in_us = int(round(start_s * 1_000_000))
    src_us = int(round(probe['duration'] * 1_000_000)) or (in_us + clip_us)

    # 1. clone the template folder
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('_')[:48] or 'clip'
    folder_name = f"{time.strftime('%Y%m%d_%H%M%S')}_{base}"
    dest = os.path.join(root, folder_name)
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(root, f'{folder_name}_{n}')
    shutil.copytree(tpl['folder'], dest)
    shutil.rmtree(os.path.join(dest, BACKUP_DIR), ignore_errors=True)

    draft_id = uid()
    try:
        cp = content_path(dest)
        content = read_json(cp)

        # 2. video
        vtrack = find_track(content, 'video')
        if not vtrack or not vtrack.get('segments'):
            raise RuntimeError('Template has no video clip. Add one to the template project.')
        vseg_tpl = vtrack['segments'][0]
        vmat_tpl = find_material(content, vseg_tpl.get('material_id'))
        if not vmat_tpl:
            raise RuntimeError('Template video clip has no material.')
        vmat = deepcopy(vmat_tpl)
        vmat['id'] = uid()
        vmat['path'] = posix(os.path.abspath(video))
        vmat['material_name'] = os.path.basename(video)
        if 'duration' in vmat:
            vmat['duration'] = src_us
        if probe.get('width') and 'width' in vmat:
            vmat['width'] = probe['width']
        if probe.get('height') and 'height' in vmat:
            vmat['height'] = probe['height']
        if 'has_audio' in vmat:
            vmat['has_audio'] = probe['has_audio']
        content.setdefault('materials', {}).setdefault('videos', []).append(vmat)

        seg = deepcopy(vseg_tpl)
        seg['id'] = uid()
        seg['material_id'] = vmat['id']
        seg['target_timerange'] = {'start': 0, 'duration': clip_us}
        seg['source_timerange'] = {'start': in_us, 'duration': clip_us}
        if 'speed' in seg:
            seg['speed'] = 1.0
        blank_keyframes(seg)
        vtrack['segments'] = [seg]

        # 3. canvas + vertical fit
        if str(canvas).replace(' ', '') in ('9:16', 'vertical', '1080x1920'):
            width, height = 1080, 1920
            set_canvas(content, width, height)
            src_w = probe.get('width') or 1920
            scale = width / float(src_w)
            clip = seg.setdefault('clip', {})
            clip['scale'] = {'x': scale, 'y': scale}
            clip['transform'] = {'x': 0.0, 'y': 0.0}
            if 'rotation' in clip:
                clip['rotation'] = 0.0
        else:
            set_canvas(content, 1920, 1080)

        # 4. captions + title from the template's text style
        cues = []
        if subs:
            cues = parse_subs(subs)
            offset = float(subs_offset or 0)
            cleaned = []
            for cue in cues:
                a = cue['start'] - offset
                b = cue['end'] - offset
                if b <= 0 or a >= dur_s:
                    continue
                a = max(0.0, a)
                b = min(dur_s, b)
                if b - a >= 0.08:
                    cleaned.append({'start': a, 'end': b, 'text': cue['text']})
            cues = cleaned

        if cues or title:
            ttrack = find_track(content, 'text')
            tseg_tpl = tmat_tpl = None
            if ttrack and ttrack.get('segments'):
                tseg_tpl = ttrack['segments'][0]
                tmat_tpl = find_material(content, tseg_tpl.get('material_id'))
            if not tmat_tpl:
                donor = find_text_donor(root)
                if donor:
                    tseg_tpl = donor['segment']
                    tmat_tpl = donor['material']
            if not tmat_tpl:
                tmat_tpl = {
                    'id': uid(), 'type': 'text', 'name': '',
                    'content': json.dumps({'text': 'TEXT', 'styles': [{
                        'range': [0, 4], 'size': 9.0,
                        'fill': {'content': {'render_type': 'solid',
                                             'solid': {'color': [1, 1, 1]}}}}]}),
                    'global_alpha': 1.0, 'line_spacing': 0.02,
                    'has_shadow': False, 'text_color': '#FFFFFF',
                    'text_alpha': 1.0, 'font_name': '', 'font_size': 7.0,
                    'font_path': 'C:/Windows/Fonts/arial.ttf', 'font_id': '',
                    'alignment': 1, 'line_feed': 1, 'is_rich_text': False,
                    'underline': False, 'italic_degree': 0, 'bold_width': 0.0,
                    'border_alpha': 1.0, 'border_color': '', 'border_width': 0.08,
                    'background_color': '', 'background_alpha': 1.0,
                    'background_style': 0, 'background_round_radius': 0.0,
                    'background_width': 0.14, 'background_height': 0.14,
                    'fixed_width': -1.0, 'fixed_height': -1.0,
                    'line_max_width': 0.82, 'force_apply_line_max_width': False,
                    'language': 'en-US', 'source_from': '',
                    'current_words': {'start_time': [], 'end_time': [], 'text': []},
                    'words': {'start_time': [], 'end_time': [], 'text': []},
                }
            if not tseg_tpl:
                tseg_tpl = {
                    'id': uid(), 'source_timerange': None,
                    'target_timerange': {'start': 0, 'duration': 1_000_000},
                    'render_timerange': {'start': 0, 'duration': 0}, 'desc': '',
                    'state': 0, 'speed': 1.0, 'is_loop': False,
                    'is_tone_modify': False, 'reverse': False,
                    'intensifies_audio': False, 'cartoon': False, 'volume': 1.0,
                    'last_nonzero_volume': 1.0,
                    'clip': {'scale': {'x': 1.0, 'y': 1.0}, 'rotation': 0.0,
                             'transform': {'x': 0.0, 'y': -0.7},
                             'flip': {'vertical': False, 'horizontal': False},
                             'alpha': 1.0},
                    'uniform_scale': {'on': True, 'value': 1.0},
                    'material_id': '', 'extra_material_refs': [],
                    'render_index': 14000, 'keyframe_refs': [],
                    'enable_lut': False, 'enable_adjust': False,
                    'enable_hsl': False, 'visible': True, 'group_id': '',
                    'track_render_index': 1, 'common_keyframes': [],
                }
            if ttrack is None:
                ttrack = {'id': uid(), 'type': 'text', 'segments': [],
                          'flag': 1, 'attribute': 0, 'name': 'text',
                          'is_default_name': True}
                content.setdefault('tracks', []).append(ttrack)
            ttrack['segments'] = []

            def add_text(track, text, at, length):
                tmat = deepcopy(tmat_tpl)
                tmat['id'] = uid()
                set_text_material(tmat, text)
                content.setdefault('materials', {}).setdefault('texts', []).append(tmat)
                tseg = deepcopy(tseg_tpl)
                tseg['id'] = uid()
                tseg['material_id'] = tmat['id']
                tseg['extra_material_refs'] = []
                tseg['target_timerange'] = {'start': int(at * 1e6),
                                            'duration': max(100_000, int(length * 1e6))}
                if 'source_timerange' in tseg:
                    tseg['source_timerange'] = {'start': 0,
                                                'duration': tseg['target_timerange']['duration']}
                blank_keyframes(tseg)
                track['segments'].append(tseg)

            if title:
                # title on its own track so it can overlap captions visually
                title_track = deepcopy(ttrack)
                title_track['id'] = uid()
                title_track['segments'] = []
                content.setdefault('tracks', []).append(title_track)
                add_text(title_track, title, 0.0, min(3.0, dur_s))
            for cue in _deoverlap(cues):
                add_text(ttrack, cue['text'], cue['start'], cue['end'] - cue['start'])

        _prune_unused(content)

        # 5. duration + id
        end_us = 0
        for track in content.get('tracks') or []:
            for s in track.get('segments') or []:
                tr = s.get('target_timerange') or {}
                end_us = max(end_us, int(tr.get('start', 0)) + int(tr.get('duration', 0)))
        content['duration'] = end_us
        if 'id' in content:
            content['id'] = draft_id

        errors = validate(content, video)
        if errors:
            raise RuntimeError('draft validation failed: ' + '; '.join(errors[:8]))
        write_json(cp, content)
        sync_backup(cp, content)
        set_timeline_layout(dest, draft_id)

        # 6. meta
        meta_path = os.path.join(dest, 'draft_meta_info.json')
        meta = read_json(meta_path) if os.path.isfile(meta_path) else {}
        meta['draft_name'] = name
        if 'draft_id' in meta:
            meta['draft_id'] = draft_id
        if 'draft_name' in meta:
            meta['draft_name'] = name
        meta['tm_duration'] = end_us
        meta['tm_draft_create'] = now_us()
        meta['tm_draft_modified'] = now_us()
        meta['draft_fold_path'] = posix(dest)
        meta['draft_root_path'] = posix(root)
        _register_media(meta, video, probe)
        cover = os.path.join(dest, 'draft_cover.jpg')
        try:
            mid = start_s + dur_s / 2.0
            subprocess.run(
                ['ffmpeg', '-y', '-v', 'error', '-ss', f'{mid:.3f}', '-i', video,
                 '-frames:v', '1', '-vf', 'scale=480:-2', cover],
                capture_output=True, timeout=180)
            if os.path.isfile(cover):
                meta['draft_cover'] = posix(cover)
        except Exception:
            pass
        write_json(meta_path, meta)

        # 7. root registry
        _register_root(root, tpl, name, dest, end_us, draft_id)

        subs_count = len(cues)
        return {
            'ok': True,
            'draft_name': name,
            'folder': dest,
            'duration_sec': round(end_us / 1e6, 2),
            'canvas': '1080x1920' if str(canvas).replace(' ', '') in ('9:16', 'vertical', '1080x1920') else '1920x1080',
            'video_segments': 1,
            'caption_segments': subs_count,
            'title': title,
            'template': tpl['draft_name'],
            'next': 'Open CapCut, the draft appears in the project list; polish and Export.',
        }
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def make_multi_clip_draft(name, stream_dir, plan=None, video=None, canvas='16:9',
                          root=None, clip_dir=None):
    """Create one CapCut draft from a stream's clips_plan.json.

    Built by cloning a *multi-clip* project that CapCut itself created, because
    the one-clip template cannot express a many-clip timeline. Requirements
    learned the hard way:

      * each distinct file needs its own material id, and each segment must
        point at its own material (otherwise every clip plays the first file);
      * the donor's draft id / meta draft id / timeline layout must be kept --
        regenerating them makes CapCut fail to bind the timeline, show clips
        that never render, and rename the folder on quit;
      * all ranges must land on the frame grid, or playback never starts;
      * video and audio need parallel tracks.

    clip_dir:   optional folder of pre-cut .mp4 clips to use directly (one
                material per file). When omitted, one source video plus the
                plan's ranges is used.

    stream_dir: folder containing clips_plan.json and the source video.
    plan:       optional explicit path to clips_plan.json.
    video:      optional source video (default: largest .mkv/.mp4 in stream_dir).
    """
    root = drafts_root(root)
    if capcut_running():
        raise RuntimeError('CapCut is running. Close CapCut fully, then retry '
                           '(it autosaves over agent-written drafts).')
    name = str(name).strip()
    if not name:
        raise RuntimeError('draft name is required')
    stream_dir = os.path.abspath(str(stream_dir))
    if not os.path.isdir(stream_dir):
        raise RuntimeError(f'stream folder not found: {stream_dir}')

    # ---- collect (source_file, in_point_s, duration_s) triples
    jobs = []
    if clip_dir:
        clip_dir = os.path.abspath(str(clip_dir))
        files = sorted(p for p in (os.path.join(clip_dir, f) for f in os.listdir(clip_dir))
                       if p.lower().endswith(('.mp4', '.mov', '.mkv')))
        if not files:
            raise RuntimeError(f'no clips in {clip_dir}')
        for path in files:
            info = ffprobe_media(path)
            jobs.append((path, 0.0, float(info.get('duration') or 0), info))
    else:
        plan_path = (os.path.abspath(str(plan)) if plan
                     else os.path.join(stream_dir, 'clips_plan.json'))
        if not os.path.isfile(plan_path):
            raise RuntimeError(f'clips plan not found: {plan_path}')
        clips = read_json(plan_path)
        if not isinstance(clips, list) or not clips:
            raise RuntimeError('clips plan must be a non-empty JSON list')
        if video:
            video_path = os.path.abspath(str(video))
        else:
            cands = [e for e in os.scandir(stream_dir)
                     if e.is_file() and e.name.lower().endswith(('.mkv', '.mp4'))]
            if not cands:
                raise RuntimeError(f'no .mkv/.mp4 source video in {stream_dir}')
            video_path = max(cands, key=lambda e: e.stat().st_size).path
        if not os.path.isfile(video_path):
            raise RuntimeError(f'video not found: {video_path}')
        src = ffprobe_media(video_path)
        src_dur = float(src.get('duration') or 0)
        for i, clip in enumerate(clips, 1):
            if not isinstance(clip, dict):
                raise RuntimeError(f'clip {i} is not an object')
            try:
                start = float(clip.get('start', 0))
                if clip.get('duration') is not None:
                    dur = float(clip['duration'])
                elif clip.get('end') is not None:
                    dur = float(clip['end']) - start
                else:
                    raise KeyError('duration')
            except (TypeError, ValueError, KeyError):
                raise RuntimeError(f'clip {i} needs numeric start + duration (or end)')
            if start < 0 or dur <= 0:
                raise RuntimeError(f'clip {i} has invalid range: start={start}, duration={dur}')
            if src_dur and start + dur > src_dur + 0.5:
                raise RuntimeError(f'clip {i} ends past the video '
                                   f'({start + dur:.2f}s > {src_dur:.2f}s)')
            jobs.append((video_path, start, dur, src))

    donor = find_multi_clip_donor(root)
    if not donor:
        raise RuntimeError(
            'No multi-clip donor project found. In CapCut, create one project with '
            'several clips, save, clear its timeline to a few clips you keep, and '
            'close CapCut. Set CAPCUT_DONOR=<folder> to point at it explicitly.')

    base = re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('_')[:48] or 'clips'
    folder_name = f"{time.strftime('%Y%m%d_%H%M%S')}_{base}"
    dest = os.path.join(root, folder_name)
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(root, f'{folder_name}_{n}')
    os.makedirs(root, exist_ok=True)
    shutil.copytree(donor['folder'], dest)
    shutil.rmtree(os.path.join(dest, BACKUP_DIR), ignore_errors=True)
    for junk in ('draft.extra', 'mini_draft.json', 'template-2.tmp', '.locked'):
        p = os.path.join(dest, junk)
        if os.path.isfile(p):
            os.remove(p)

    try:
        cp = content_path(dest)
        content = read_json(cp)
        mats = content.setdefault('materials', {})
        vtrack = find_track(content, 'video')
        atrack = find_track(content, 'audio')
        if not vtrack or not atrack:
            raise RuntimeError('donor has no video+audio track pair')
        v_seg_tpl = vtrack['segments'][0]
        a_seg_tpl = atrack['segments'][0]
        v_mats = mats.get('videos') or []
        a_mats = mats.get('audios') or []
        if not v_mats or not a_mats:
            raise RuntimeError('donor has no video/audio materials')
        v_mat_tpl, a_mat_tpl = v_mats[0], a_mats[0]
        v_refs = [s.get('extra_material_refs') or [] for s in vtrack['segments']]
        a_refs = [s.get('extra_material_refs') or [] for s in atrack['segments']]

        videos, audios, vsegs, asegs = [], [], [], []
        acc = 0
        for i, (path, start_s, dur_s, info) in enumerate(jobs):
            raw = int(round(float(info.get('duration') or dur_s) * 1e6))
            dur_us = snap_us(int(round(dur_s * 1e6)))
            if dur_us <= 0:
                raise RuntimeError(f'clip {i + 1} snaps to zero frames')
            if raw and dur_us > raw:
                dur_us -= int(round(1_000_000 / SNAP_FPS))
            src_us = snap_us(int(round(start_s * 1e6)))

            vmat = deepcopy(v_mat_tpl)
            vmat['id'] = uid()
            vmat['path'] = posix(path)
            vmat['material_name'] = os.path.basename(path)
            if 'duration' in vmat:
                vmat['duration'] = raw or dur_us
            if info.get('width') and 'width' in vmat:
                vmat['width'] = info['width']
            if info.get('height') and 'height' in vmat:
                vmat['height'] = info['height']
            if 'has_audio' in vmat:
                vmat['has_audio'] = info.get('has_audio', True)
            videos.append(vmat)

            amat = deepcopy(a_mat_tpl)
            amat['id'] = uid()
            amat['path'] = posix(path)
            if 'duration' in amat:
                amat['duration'] = raw or dur_us
            if 'material_name' in amat:
                amat['material_name'] = os.path.basename(path)
            audios.append(amat)

            vs = deepcopy(v_seg_tpl)
            vs['id'] = uid()
            vs['material_id'] = vmat['id']
            vs['source_timerange'] = {'start': src_us, 'duration': dur_us}
            vs['target_timerange'] = {'start': acc, 'duration': dur_us}
            vs['speed'] = 1.0
            vs['volume'] = 1.0
            vs['extra_material_refs'] = list(v_refs[i % len(v_refs)])
            blank_keyframes(vs)
            vsegs.append(vs)

            asg = deepcopy(a_seg_tpl)
            asg['id'] = uid()
            asg['material_id'] = amat['id']
            asg['source_timerange'] = {'start': src_us, 'duration': dur_us}
            asg['target_timerange'] = {'start': acc, 'duration': dur_us}
            asg['speed'] = 1.0
            asg['extra_material_refs'] = list(a_refs[i % len(a_refs)])
            blank_keyframes(asg)
            asegs.append(asg)
            acc += dur_us

        mats['videos'] = videos
        mats['audios'] = audios
        vtrack['segments'] = vsegs
        atrack['segments'] = asegs
        content['tracks'] = [vtrack, atrack]

        # canvas
        vertical = str(canvas).replace(' ', '') in ('9:16', 'vertical', '1080x1920')
        if vertical:
            set_canvas(content, 1080, 1920)
            for seg, (path, _s, _d, info) in zip(vsegs, jobs):
                scale = 1080 / float(info.get('width') or 1920)
                clip = seg.setdefault('clip', {})
                clip['scale'] = {'x': scale, 'y': scale}
                clip['transform'] = {'x': 0.0, 'y': 0.0}
                if 'rotation' in clip:
                    clip['rotation'] = 0.0
        else:
            set_canvas(content, 1920, 1080)
        content['duration'] = snap_us(acc)
        # deliberately keep the donor's content id / meta id / timeline layout

        errors = validate(content, jobs[0][0])
        if errors:
            raise RuntimeError('draft validation failed: ' + '; '.join(errors[:8]))
        write_json(cp, content)
        sync_backup(cp, content)

        meta_path = os.path.join(dest, 'draft_meta_info.json')
        meta = read_json(meta_path) if os.path.isfile(meta_path) else {}
        meta['draft_name'] = name
        meta['tm_duration'] = content['duration']
        meta['tm_draft_create'] = now_us()
        meta['tm_draft_modified'] = now_us()
        meta['draft_fold_path'] = posix(dest)
        meta['draft_root_path'] = posix(root)
        if 'draft_is_invisible' in meta:
            meta['draft_is_invisible'] = False
        proto = None
        for group in meta.get('draft_materials') or []:
            if isinstance(group, dict) and group.get('type') == 0 and group.get('value'):
                proto = group['value'][0]
                break
        if proto is not None:
            items = []
            for path, _s, _d, info in jobs:
                item = deepcopy(proto)
                item.update({
                    'create_time': now_us(),
                    'duration': int(round(float(info.get('duration') or 0) * 1e6)),
                    'file_Path': posix(path),
                    'height': info.get('height') or 0,
                    'material_name': os.path.basename(path),
                    'metetype': 'video',
                    'type': 0,
                    'width': info.get('width') or 0,
                })
                items.append(item)
            for group in meta.get('draft_materials') or []:
                if isinstance(group, dict) and group.get('type') == 0:
                    group['value'] = items
        try:
            cover = os.path.join(dest, 'draft_cover.jpg')
            mid = jobs[0][1] + jobs[0][2] / 2.0
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', f'{mid:.3f}',
                            '-i', jobs[0][0], '-frames:v', '1',
                            '-vf', 'scale=480:-2', cover],
                           capture_output=True, timeout=180)
            if os.path.isfile(cover):
                meta['draft_cover'] = posix(cover)
        except Exception:
            pass
        write_json(meta_path, meta)

        _register_root(root, donor, name, dest, content['duration'],
                       meta.get('draft_id') or content.get('id') or donor['name'])

        return {
            'ok': True,
            'draft_name': name,
            'folder': dest,
            'clips': len(jobs),
            'segments': len(vsegs),
            'audio_segments': len(asegs),
            'duration_sec': round(content['duration'] / 1e6, 2),
            'canvas': '1080x1920' if vertical else '1920x1080',
            'donor': donor['name'],
            'next': 'Open CapCut, the draft appears in the project list; polish and Export.',
        }
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _register_media(meta, video, probe):
    """Best-effort: mirror the video into draft_meta_info's draft_materials."""
    groups = meta.get('draft_materials')
    if not isinstance(groups, list):
        return
    entry = {
        'create_time': now_us(),
        'duration': int(round(probe['duration'] * 1e6)) if probe.get('duration') else 0,
        'file_Path': posix(os.path.abspath(video)),
        'height': probe.get('height') or 0,
        'id': uid(),
        'material_name': os.path.basename(video),
        'metetype': 'video',
        'roughcut_time_range': {'start': -1, 'duration': -1},
        'sub_time_range': {'start': -1, 'duration': -1},
        'type': 0,
        'width': probe.get('width') or 0,
    }
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('value'), list):
            continue
        template_item = None
        for item in group['value']:
            if isinstance(item, dict) and str(item.get('metetype', '')).lower() == 'video':
                template_item = item
                break
        if template_item is not None:
            item = deepcopy(template_item)
            item.update({k: v for k, v in entry.items() if v not in (None, '')})
            # replace the template's own video entry: appending would leave a
            # stale source (the template's clip) in the media pool
            group['value'] = [v for v in group['value']
                              if v is not template_item]
            group['value'].append(item)
            return
    # no video group found: append a standard one
    groups.append({'type': 0, 'value': [entry]})


def _register_root(root, tpl, name, dest, duration_us, draft_id):
    path = os.path.join(root, 'root_meta_info.json')
    root_meta = read_json(path) if os.path.isfile(path) else {
        'all_draft_store': [], 'draft_ids': 0, 'root_path': posix(root)}
    entries = root_meta.get('all_draft_store')
    if not isinstance(entries, list):
        entries = []
        root_meta['all_draft_store'] = entries
    template_entry = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        fold = posix(entry.get('draft_fold_path', '')).rstrip('/')
        if fold.lower().endswith(str(tpl['name']).lower()) or \
                entry.get('draft_name') == tpl.get('draft_name'):
            template_entry = entry
            break
    new_entry = deepcopy(template_entry) if template_entry else {}
    cover = os.path.join(dest, 'draft_cover.jpg')
    new_entry.update({
        'draft_name': name,
        'draft_fold_path': posix(dest),
        'draft_json_file': posix(os.path.join(dest, 'draft_content.json')),
        'draft_root_path': posix(root),
        'draft_cover': posix(cover) if os.path.isfile(cover) else '',
        'tm_duration': duration_us,
        'tm_draft_create': now_us(),
        'tm_draft_modified': now_us(),
    })
    if draft_id:
        new_entry['draft_id'] = draft_id
    new_entry.setdefault('draft_is_invisible', False)
    new_entry.setdefault('cloud_draft_cover', False)
    new_entry.setdefault('cloud_draft_sync', False)
    new_entry.setdefault('tm_draft_removed', 0)
    entries.append(new_entry)
    try:
        root_meta['draft_ids'] = max(int(root_meta.get('draft_ids') or 0), len(entries))
    except Exception:
        pass
    write_json(path, root_meta)


def read_draft(name, root=None):
    item = find_draft(name, root)
    folder = item['folder']
    content = read_json(content_path(folder))
    tracks = []
    captions = []
    for track in content.get('tracks') or []:
        segs = track.get('segments') or []
        tracks.append({
            'type': track_type(track),
            'id': track.get('id'),
            'segments': len(segs),
            'duration_sec': round(sum((s.get('target_timerange') or {}).get('duration', 0)
                                      for s in segs) / 1e6, 2),
        })
        if track_type(track) == 'text':
            for seg in segs:
                mat = find_material(content, seg.get('material_id')) or {}
                raw = mat.get('content')
                text = ''
                if isinstance(raw, str):
                    try:
                        text = json.loads(raw).get('text', '')
                    except Exception:
                        text = raw
                elif isinstance(raw, dict):
                    text = raw.get('text', '')
                tr = seg.get('target_timerange') or {}
                captions.append({
                    'start': round((tr.get('start') or 0) / 1e6, 2),
                    'end': round(((tr.get('start') or 0) + (tr.get('duration') or 0)) / 1e6, 2),
                    'text': text,
                })
    media = []
    for key in ('videos', 'audios'):
        for m in _materials(content).get(key, []) or []:
            if isinstance(m, dict):
                media.append({'type': key[:-1], 'path': m.get('path'),
                              'exists': os.path.isfile(m.get('path') or '')})
    return {
        'ok': True,
        'draft_name': item['draft_name'],
        'folder': folder,
        'canvas': content.get('canvas_config'),
        'duration_sec': round((content.get('duration') or 0) / 1e6, 2),
        'tracks': tracks,
        'media': media,
        'captions': sorted(captions, key=lambda c: c['start']),
    }


def delete_draft(name, confirm=False, root=None):
    if not confirm:
        raise RuntimeError('Refusing to delete without confirm=True')
    if capcut_running():
        raise RuntimeError('Close CapCut before deleting a draft.')
    item = find_draft(name, root)
    root = drafts_root(root)
    trash = os.path.join(root, '.mcp_trash')
    os.makedirs(trash, exist_ok=True)
    dest = os.path.join(trash, os.path.basename(item['folder']) + '_' + time.strftime('%Y%m%d_%H%M%S'))
    shutil.move(item['folder'], dest)
    path = os.path.join(root, 'root_meta_info.json')
    if os.path.isfile(path):
        root_meta = read_json(path)
        entries = root_meta.get('all_draft_store')
        if isinstance(entries, list):
            root_meta['all_draft_store'] = [
                e for e in entries
                if not (isinstance(e, dict)
                        and posix(e.get('draft_fold_path', '')).rstrip('/').endswith(
                            os.path.basename(item['folder'])))]
            write_json(path, root_meta)
    return {'ok': True, 'moved_to': dest}


def parse_time(value):
    """Accept HH:MM:SS, MM:SS, or plain seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(',', '.')
    parts = [float(p) for p in text.split(':')]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


def import_subs_into_draft(name, subs, subs_offset=0.0, replace=True, root=None):
    """Add/replace a caption track on an existing draft, cloning its text style."""
    root = drafts_root(root)
    if capcut_running():
        raise RuntimeError('CapCut is running. Close it before writing drafts.')
    item = find_draft(name, root)
    folder = item['folder']
    cp = content_path(folder)
    content = read_json(cp)
    text_tracks = [t for t in content.get('tracks') or []
                   if track_type(t) == 'text']
    ttrack = None
    if text_tracks:
        # the caption track is the text track with the most cues (title tracks have 1)
        ttrack = max(text_tracks, key=lambda t: len(t.get('segments') or []))
    tseg_tpl = tmat_tpl = None
    if ttrack and ttrack.get('segments'):
        tseg_tpl = ttrack['segments'][0]
        tmat_tpl = find_material(content, tseg_tpl.get('material_id'))
    if not tseg_tpl or not tmat_tpl:
        # Multi-clip drafts start with an empty text track; borrow any real
        # draft's text segment/style so captions can still be imported.
        donor = find_text_donor(root)
        if donor:
            tseg_tpl = donor['segment']
            tmat_tpl = donor['material']
        if not tmat_tpl:
            raise RuntimeError('Draft has no text clip to clone caption styling from.')
        if ttrack is None:
            ttrack = {'id': uid(), 'type': 'text', 'segments': [],
                      'flag': 1, 'attribute': 0, 'name': 'text',
                      'is_default_name': True}
            content.setdefault('tracks', []).append(ttrack)

    duration_s = (content.get('duration') or 0) / 1e6
    cues = parse_subs(subs)
    if duration_s <= 0:
        duration_s = max((c['end'] for c in cues), default=0.0)
    offset = float(subs_offset or 0)
    cleaned = []
    for cue in cues:
        a, b = cue['start'] - offset, cue['end'] - offset
        if b <= 0 or (duration_s and a >= duration_s):
            continue
        a = max(0.0, a)
        b = min(duration_s, b)
        if b - a >= 0.08:
            cleaned.append({'start': a, 'end': b, 'text': cue['text']})

    if replace:
        ttrack['segments'] = []
    for cue in _deoverlap(cleaned):
        tmat = deepcopy(tmat_tpl)
        tmat['id'] = uid()
        set_text_material(tmat, cue['text'])
        content.setdefault('materials', {}).setdefault('texts', []).append(tmat)
        tseg = deepcopy(tseg_tpl)
        tseg['id'] = uid()
        tseg['material_id'] = tmat['id']
        tseg['extra_material_refs'] = []
        tseg['target_timerange'] = {
            'start': int(cue['start'] * 1e6),
            'duration': max(100_000, int((cue['end'] - cue['start']) * 1e6))}
        if 'source_timerange' in tseg:
            tseg['source_timerange'] = {'start': 0,
                                        'duration': tseg['target_timerange']['duration']}
        blank_keyframes(tseg)
        ttrack['segments'].append(tseg)

    _prune_unused(content)
    end_us = 0
    for track in content.get('tracks') or []:
        for seg in track.get('segments') or []:
            tr = seg.get('target_timerange') or {}
            end_us = max(end_us, int(tr.get('start', 0)) + int(tr.get('duration', 0)))
    content['duration'] = end_us
    errors = validate(content)
    if errors:
        raise RuntimeError('draft validation failed: ' + '; '.join(errors[:8]))
    write_json(cp, content)
    sync_backup(cp, content)

    meta_path = os.path.join(folder, 'draft_meta_info.json')
    if os.path.isfile(meta_path):
        meta = read_json(meta_path)
        meta['tm_duration'] = end_us
        meta['tm_draft_modified'] = now_us()
        write_json(meta_path, meta)

    return {'ok': True, 'draft_name': item['draft_name'], 'folder': folder,
            'captions': len(cleaned), 'replaced': bool(replace)}


def doctor(root=None):
    root = drafts_root(root)
    tpl = None
    try:
        tpl = find_template(root)
    except Exception:
        pass
    drafts = list_drafts(root)
    tpl_text = find_track(tpl['content'], 'text') if tpl else None
    text_donor = None if (tpl_text and tpl_text.get('segments')) else find_text_donor(root)
    captions_ready = bool(tpl_text and tpl_text.get('segments')) or bool(text_donor)
    return {
        'drafts_root': root,
        'drafts_root_exists': os.path.isdir(root),
        'draft_count': len(drafts),
        'capcut_installed': capcut_version(),
        'capcut_running': capcut_running(),
        'template_ready': bool(tpl),
        'captions_ready': captions_ready,
        'caption_style_source': (
            'template' if (tpl_text and tpl_text.get('segments')) else
            (text_donor['draft_name'] if text_donor else None)),
        'template': ({'draft_name': tpl['draft_name'], 'folder': tpl['folder']}
                     if tpl else None),
        'bootstrap': None if tpl else (
            "Create a CapCut project named 'template': add any video clip, save, "
            "and close CapCut. Add one text clip too if you want a specific caption style."),
        'caption_bootstrap': None if captions_ready else (
            "No text clip was found in any draft. Add one text clip to the "
            "'template' project (position/style it where captions should appear), "
            "save, and close CapCut."),
        'drafts': drafts,
    }
