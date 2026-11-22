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
    return uuid.uuid4().hex


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
        if find_track(content, 'video') and find_track(content, 'text'):
            meta_name = os.path.basename(folder)
            try:
                meta = read_json(os.path.join(folder, 'draft_meta_info.json'))
                meta_name = meta.get('draft_name') or meta_name
            except Exception:
                pass
            return {'folder': folder, 'name': os.path.basename(folder),
                    'draft_name': meta_name, 'content_path': cp, 'content': content}
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
        for key in ('common_keyframes', 'keyframe_refs'):
            seg.pop(key, None)
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
            if not ttrack or not ttrack.get('segments'):
                raise RuntimeError(
                    'Template has no text clip. Add one text clip to the template '
                    '(its font/position/style are reused for captions).')
            tseg_tpl = ttrack['segments'][0]
            tmat_tpl = find_material(content, tseg_tpl.get('material_id'))
            if not tmat_tpl:
                raise RuntimeError('Template text clip has no material.')
            ttrack['segments'] = []

            def add_text(track, text, at, length):
                tmat = deepcopy(tmat_tpl)
                tmat['id'] = uid()
                set_text_material(tmat, text)
                content.setdefault('materials', {}).setdefault('texts', []).append(tmat)
                tseg = deepcopy(tseg_tpl)
                tseg['id'] = uid()
                tseg['material_id'] = tmat['id']
                tseg['target_timerange'] = {'start': int(at * 1e6),
                                            'duration': max(100_000, int(length * 1e6))}
                if 'source_timerange' in tseg:
                    tseg['source_timerange'] = {'start': 0,
                                                'duration': tseg['target_timerange']['duration']}
                for key in ('common_keyframes', 'keyframe_refs'):
                    tseg.pop(key, None)
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
        if fold.endswith(tpl['name']) or entry.get('draft_name') == tpl.get('draft_name'):
            template_entry = entry
            break
    new_entry = deepcopy(template_entry) if template_entry else {}
    new_entry.update({
        'draft_name': name,
        'draft_fold_path': posix(dest),
        'tm_duration': duration_us,
        'tm_draft_create': now_us(),
        'tm_draft_modified': now_us(),
    })
    if 'draft_id' in new_entry:
        new_entry['draft_id'] = draft_id
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
                   if track_type(t) == 'text' and t.get('segments')]
    if not text_tracks:
        text_tracks = [t for t in content.get('tracks') or [] if track_type(t) == 'text']
    if not text_tracks:
        raise RuntimeError('Draft has no text clip to clone caption styling from.')
    # the caption track is the text track with the most cues (title tracks have 1)
    ttrack = max(text_tracks, key=lambda t: len(t.get('segments') or []))
    if not ttrack.get('segments'):
        raise RuntimeError('Draft has no text clip to clone caption styling from.')
    tseg_tpl = ttrack['segments'][0]
    tmat_tpl = find_material(content, tseg_tpl.get('material_id'))
    if not tmat_tpl:
        raise RuntimeError('Draft text track has no text material.')

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
        tseg['target_timerange'] = {
            'start': int(cue['start'] * 1e6),
            'duration': max(100_000, int((cue['end'] - cue['start']) * 1e6))}
        if 'source_timerange' in tseg:
            tseg['source_timerange'] = {'start': 0,
                                        'duration': tseg['target_timerange']['duration']}
        for key in ('common_keyframes', 'keyframe_refs'):
            tseg.pop(key, None)
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
    return {
        'drafts_root': root,
        'drafts_root_exists': os.path.isdir(root),
        'draft_count': len(drafts),
        'capcut_installed': capcut_version(),
        'capcut_running': capcut_running(),
        'template_ready': bool(tpl),
        'template': ({'draft_name': tpl['draft_name'], 'folder': tpl['folder']}
                     if tpl else None),
        'bootstrap': None if tpl else (
            "Create a CapCut project named 'template': add any video clip and one "
            "text clip (position/style it where captions should appear), save, close CapCut."),
        'drafts': drafts,
    }
