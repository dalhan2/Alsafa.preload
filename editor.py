"""editor.py - agentic editing for CapCut drafts.

CapCut has no public API; the editor reads and writes the draft JSON. The tool
surface mirrors Concat's editing commands so an agent can do the same work:
project/timeline/media management, clip add/move/trim/split/remove, speed,
transforms, crop, masks, keyframes, captions/titles, effects/transitions,
timeline background/linkage, preview and export.

CapCut must be closed while drafts are written: its autosave overwrites edits.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib
from contextlib import contextmanager
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capcutlib as cc  # noqa: E402
import catalogue as cat  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_NAME = '.capcut_mcp_media.json'
TEMPLATE_DIRS = ('templates',)
MAX_HISTORY = 40

# Concat property name -> (CapCut property_type, companion material key)
PROPERTY_MAP = {
    'scale': ('UNIFORM_SCALE', None),
    'offsetX': ('KFTypePositionX', None),
    'offsetY': ('KFTypePositionY', None),
    'rotation': ('KFTypeRotation', None),
    'stretchX': ('KFTypeScaleX', None),
    'stretchY': ('KFTypeScaleY', None),
    'opacity': ('KFTypeAlpha', None),
    'volume': ('KFTypeVolume', None),
    'saturation': ('KFTypeSaturation', None),
    'contrast': ('KFTypeContrast', None),
    'brightness': ('KFTypeBrightness', None),
}

MASK_NUMBERS = ('x', 'y', 'rotation', 'scale', 'feather', 'expand', 'opacity',
                'width', 'height', 'roundness')

# English names for CapCut's mix-mode resources (the app stores Chinese names).
MIX_MODES = {
    'multiply': ('正片叠底', '6758325895519277582', '871333'),
    'color-dodge': ('颜色减淡', '6758325800031752712', '871334'),
    'color-burn': ('颜色加深', '6758325724848853518', '871335'),
    'linear-burn': ('线性加深', '6758325619253056013', '871336'),
    'soft-light': ('柔光', '6758325439212556814', '871337'),
    'hard-light': ('强光', '6758325264670790152', '871338'),
    'screen': ('滤色', '6758325170760323597', '871339'),
    'overlay': ('叠加', '6758324989931295240', '871340'),
    'lighten': ('变亮', '6758324919789949453', '871341'),
    'darken': ('变暗', '6758324839670354445', '871342'),
}

_KIND_BY_TRACK = {
    'video': 'video', 'image': 'image', 'audio': 'audio', 'text': 'text',
    'subtitle': 'text', 'sticker': 'sticker', 'effect': 'effect',
    'filter': 'effect',
}

_PROJECTS = {}
_LOCK = threading.RLock()
_SCHEMA_CACHE = {}


# ---------------------------------------------------------------- small utils

def sec(us):
    return round(float(us or 0) / 1_000_000.0, 3)


def usec(seconds):
    return int(round(float(seconds or 0) * 1_000_000.0))


def clamp(value, low, high):
    return max(low, min(high, value))


def project_fps(content):
    try:
        fps = float(content.get('fps') or 30)
    except (TypeError, ValueError):
        fps = 30.0
    return fps if fps > 0 else 30.0


def snap_project(content, us):
    fps = project_fps(content)
    return int(round(round(float(us) * fps / 1_000_000.0) * 1_000_000.0 / fps))


def frame_us(content):
    return 1_000_000.0 / project_fps(content)


def parse_hex_color(value, default_alpha=1.0):
    """#rgb / #rrggbb / #rrggbbaa -> (r, g, b, a) floats 0..1."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith('#'):
        text = text[1:]
    had_alpha = len(text) == 8
    if len(text) == 3:
        text = ''.join(ch * 2 for ch in text)
    if len(text) == 6:
        text += 'ff'
    if len(text) != 8:
        raise RuntimeError(f'bad colour: {value!r} (use #rrggbb or #rrggbbaa)')
    try:
        r, g, b, a = (int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4, 6))
    except ValueError:
        raise RuntimeError(f'bad colour: {value!r}')
    if not had_alpha:
        a = default_alpha
    return r, g, b, a


def hex_from_rgb(rgb, alpha=None):
    r, g, b = (int(round(clamp(float(c), 0, 1) * 255)) for c in rgb)
    if alpha is None:
        return f'#{r:02x}{g:02x}{b:02x}'
    return f'#{r:02x}{g:02x}{b:02x}{int(round(clamp(alpha, 0, 1) * 255)):02x}'


def utc16_len(text):
    return len(text.encode('utf-16-le')) // 2


def cp_to_utf16(text, index):
    index = max(0, min(len(text), int(index)))
    return len(text[:index].encode('utf-16-le')) // 2


def utf16_slice(text, start, end):
    raw = text.encode('utf-16-le')
    return raw[start * 2:end * 2].decode('utf-16-le', errors='ignore')


def _sanitize(name):
    text = re.sub(r'[^A-Za-z0-9._-]+', '_', str(name or '')).strip('_').strip('.')
    return text[:64] or 'project'


def _require_capcut_closed():
    if os.environ.get('CAPCUT_MCP_ALLOW_RUNNING', '').lower() in ('1', 'true', 'yes'):
        return
    if cc.capcut_running():
        raise RuntimeError('CapCut is running. Close CapCut fully, then retry '
                           '(its autosave overwrites agent-written drafts).')


def _expand(path):
    return os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))


# ---------------------------------------------------------------- media probing

_IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff')
_AUDIO_EXT = ('.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg', '.opus', '.wma')


def probe_media(path):
    """ffprobe a file, returning the fields the editor needs plus raw streams."""
    path = _expand(path)
    if not os.path.isfile(path):
        raise RuntimeError(f'media not found: {path}')
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-print_format', 'json',
             '-show_format', '-show_streams', path],
            capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        raise RuntimeError('ffprobe not found on PATH (install ffmpeg)')
    if out.returncode != 0:
        raise RuntimeError(f'ffprobe failed: {out.stderr.strip()[:400]}')
    data = json.loads(out.stdout or '{}')
    fmt = data.get('format') or {}
    streams = data.get('streams') or []
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    ext = os.path.splitext(path)[1].lower()
    duration = float(fmt.get('duration') or 0)
    if not duration and video:
        duration = float(video.get('duration') or 0)
    if not duration and audio:
        duration = float(audio.get('duration') or 0)
    if video:
        kind = 'image' if (ext in _IMAGE_EXT and
                           str(video.get('codec_name', '')).lower() in
                           ('png', 'mjpeg', 'bmp', 'gif', 'webp', 'tiff')) else 'video'
    elif audio:
        kind = 'audio'
    else:
        kind = 'image' if ext in _IMAGE_EXT else 'video'
    rate = 0.0
    if video and video.get('avg_frame_rate'):
        try:
            num, den = video['avg_frame_rate'].split('/')
            rate = float(num) / float(den or 1)
        except Exception:
            rate = 0.0
    return {
        'path': cc.posix(path),
        'kind': kind,
        'duration': duration,
        'width': int(video['width']) if video and video.get('width') else None,
        'height': int(video['height']) if video and video.get('height') else None,
        'has_audio': bool(audio),
        'video_codec': video.get('codec_name') if video else None,
        'audio_codec': audio.get('codec_name') if audio else None,
        'frame_rate': round(rate, 3),
        'streams': [{
            'index': s.get('index'),
            'type': s.get('codec_type'),
            'codec': s.get('codec_name'),
            'width': s.get('width'),
            'height': s.get('height'),
            'sample_rate': s.get('sample_rate'),
            'channels': s.get('channels'),
            'duration': _float_or_none(s.get('duration')),
        } for s in streams],
    }


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- schema donors

def _schema_contents(root=None):
    root = cc.drafts_root(root)
    if root in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[root]
    contents = []
    if os.path.isdir(root):
        names = sorted(os.listdir(root))
        # biggest first is normally the most complete donor
        names.sort(key=lambda n: -os.path.getsize(os.path.join(root, n, 'draft_content.json'))
                   if os.path.isfile(os.path.join(root, n, 'draft_content.json')) else 0)
        for name in names:
            folder = os.path.join(root, name)
            if not os.path.isdir(folder):
                continue
            cp = cc.content_path(folder)
            if not cp:
                continue
            try:
                content = cc.read_json(cp)
            except Exception:
                continue
            if content.get('tracks'):
                contents.append({'folder': folder, 'name': name,
                                 'draft_name': (content.get('name') or name),
                                 'content': content})
    _SCHEMA_CACHE[root] = contents
    return contents


def _schema_material(kind):
    for donor in _schema_contents():
        arr = (donor['content'].get('materials') or {}).get(kind) or []
        for item in arr:
            if isinstance(item, dict):
                return deepcopy(item)
    return None


def _schema_track(kind):
    for donor in _schema_contents():
        for track in donor['content'].get('tracks') or []:
            if cc.track_type(track) == kind and track.get('segments'):
                return deepcopy(track)
    return None


def _schema_segment(kind):
    track = _schema_track(kind)
    if track and track.get('segments'):
        return deepcopy(track['segments'][0])
    return None


def _schema_donor(root=None):
    contents = _schema_contents(root)
    if not contents:
        raise RuntimeError(
            'No CapCut project is available to clone the draft schema from. '
            'Create one project in CapCut with a video clip, save it, and close CapCut.')
    def score(donor):
        content = donor['content']
        mats = content.get('materials') or {}
        tracks = {cc.track_type(t) for t in content.get('tracks') or []}
        value = 0
        value += 20 if 'video' in tracks else 0
        value += 8 if 'text' in tracks else 0
        value += 8 if 'audio' in tracks else 0
        value += 8 if any(t in tracks for t in ('effect', 'filter')) else 0
        value += min(10, len(mats.get('texts') or []))
        value += min(10, len(mats.get('video_effects') or []))
        value += min(10, len(mats.get('material_animations') or []))
        value += 2 if content.get('version') else 0
        return value
    return max(contents, key=score)


def invalidate_schema():
    _SCHEMA_CACHE.clear()


# ---------------------------------------------------------------- material factories

def _base_material(kind, fallback):
    template = _schema_material(kind)
    if template:
        return template
    return deepcopy(fallback)


def new_speed_material(speed=1.0):
    m = _base_material('speeds', {'type': 'speed', 'mode': 0, 'speed': 1.0,
                                  'curve_speed': None})
    m['id'] = cc.uid()
    m['type'] = 'speed'
    m['mode'] = 0
    m['speed'] = float(speed)
    m['curve_speed'] = None
    return m


def new_placeholder_info():
    m = _base_material('placeholder_infos', {'type': 'placeholder_info',
                                             'meta_type': 'none', 'res_path': '',
                                             'res_text': '', 'error_path': '',
                                             'error_text': ''})
    m['id'] = cc.uid()
    m['type'] = 'placeholder_info'
    return m


def new_canvas_material(kind='none', color='', blur=0.0, image='', image_id=''):
    m = _base_material('canvases', {'type': 'canvas_color', 'color': '',
                                    'blur': 0.0, 'image': '', 'album_image': '',
                                    'image_id': '', 'image_name': '',
                                    'source_platform': 0, 'team_id': ''})
    m['id'] = cc.uid()
    m['type'] = {'none': 'canvas_color', 'color': 'canvas_color',
                 'blur': 'canvas_blur', 'image': 'canvas_image'}.get(kind, 'canvas_color')
    m['color'] = color or ''
    m['blur'] = float(blur or 0.0)
    m['image'] = image or ''
    m['image_id'] = image_id or ''
    return m


def new_sound_channel_mapping():
    m = _base_material('sound_channel_mappings', {
        'type': 'none', 'audio_channel_mapping': 0, 'is_config_open': False})
    m['id'] = cc.uid()
    m['type'] = 'none'
    return m


def new_material_color():
    m = _base_material('material_colors', {
        'is_color_clip': False, 'is_gradient': False, 'solid_color': '',
        'gradient_colors': [], 'gradient_percents': [], 'gradient_angle': 90.0,
        'width': 0.0, 'height': 0.0})
    m['id'] = cc.uid()
    return m


def new_vocal_separation():
    m = _base_material('vocal_separations', {
        'type': 'vocal_separation', 'choice': 0, 'removed_sounds': [],
        'time_range': None, 'production_path': '', 'final_algorithm': '',
        'enter_from': ''})
    m['id'] = cc.uid()
    m['type'] = 'vocal_separation'
    return m


def new_beats():
    m = _base_material('beats', {
        'type': 'beats', 'enable_ai_beats': False, 'gear': 404,
        'gear_count': 0, 'mode': 404, 'user_beats': [],
        'user_delete_ai_beats': None,
        'ai_beats': {'melody_url': '', 'melody_path': '', 'beats_url': '',
                     'beats_path': '', 'melody_percents': [0.6],
                     'beat_speed_infos': []}})
    m['id'] = cc.uid()
    m['type'] = 'beats'
    return m


def new_audio_fade(fade_in=0.0, fade_out=0.0):
    m = _base_material('audio_fades', {})
    if not m or 'id' not in m:
        m = {}
    m.update({
        'id': cc.uid(),
        'type': 'audio_fade',
        'fade_in_duration': usec(fade_in),
        'fade_out_duration': usec(fade_out),
        'fade_type': 0,
    })
    return m


def new_video_material(media):
    m = _base_material('videos', {
        'type': 'video', 'path': '', 'material_name': '', 'duration': 0,
        'width': 0, 'height': 0, 'has_audio': False, 'local_material_id': '',
        'crop': {'upper_left_x': 0.0, 'upper_left_y': 0.0,
                 'upper_right_x': 1.0, 'upper_right_y': 0.0,
                 'lower_left_x': 0.0, 'lower_left_y': 1.0,
                 'lower_right_x': 1.0, 'lower_right_y': 1.0},
        'crop_ratio': 'free', 'crop_scale': 1.0,
        'stable': {'stable_level': 0, 'matrix_path': '',
                   'time_range': {'start': 0, 'duration': 0}},
    })
    m['id'] = cc.uid()
    m['type'] = 'photo' if media.get('kind') == 'image' else 'video'
    m['path'] = cc.posix(media['path'])
    m['material_name'] = os.path.basename(media['path'])
    m['duration'] = usec(media.get('duration') or 10_800)
    m['width'] = int(media.get('width') or 0)
    m['height'] = int(media.get('height') or 0)
    m['has_audio'] = bool(media.get('has_audio'))
    m['local_material_id'] = media.get('id') or ''
    if m.get('type') == 'photo':
        m['duration'] = usec(10_800)
    return m


def new_audio_material(media):
    m = _base_material('audios', {
        'type': 'extract_music', 'name': '', 'path': '', 'duration': 0,
        'wave_points': [], 'category_id': '', 'category_name': 'local',
        'source_platform': 0, 'music_id': '',
    })
    m['id'] = cc.uid()
    m['type'] = 'extract_music'
    m['name'] = os.path.basename(media['path'])
    m['path'] = cc.posix(media['path'])
    m['duration'] = usec(media.get('duration') or 0)
    m['local_material_id'] = media.get('id') or ''
    return m


def _font_path(family, bold=False, italic=False):
    family = str(family or '').strip().strip('"').strip("'")
    key = re.sub(r'[^a-z0-9]+', '', family.lower())
    known = {
        'arial': ('arial.ttf', 'arialbd.ttf', 'ariali.ttf', 'arialbi.ttf'),
        'arialblack': ('ariblk.ttf',) * 4,
        'impact': ('impact.ttf',) * 4,
        'georgia': ('georgia.ttf', 'georgiab.ttf', 'georgiai.ttf', 'georgiaz.ttf'),
        'timesnewroman': ('times.ttf', 'timesbd.ttf', 'timesi.ttf', 'timesbi.ttf'),
        'verdana': ('verdana.ttf', 'verdanab.ttf', 'verdani.ttf', 'verdanaz.ttf'),
        'tahoma': ('tahoma.ttf', 'tahomabd.ttf', 'tahoma.ttf', 'tahomabd.ttf'),
        'trebuchetms': ('trebuc.ttf', 'trebucbd.ttf', 'trebucit.ttf', 'trebucbi.ttf'),
        'comicsansms': ('comic.ttf', 'comicbd.ttf', 'comici.ttf', 'comicz.ttf'),
        'couriernew': ('cour.ttf', 'courbd.ttf', 'couri.ttf', 'courbi.ttf'),
        'segoeui': ('segoeui.ttf', 'segoeuib.ttf', 'segoeuii.ttf', 'segoeuiz.ttf'),
        'calibri': ('calibri.ttf', 'calibrib.ttf', 'calibrii.ttf', 'calibriz.ttf'),
        'hanken grotesk': ('arial.ttf', 'arialbd.ttf', 'ariali.ttf', 'arialbi.ttf'),
    }
    win = os.path.join(os.environ.get('WINDIR', 'C:/Windows'), 'Fonts')
    candidates = known.get(key)
    if not candidates:
        candidates = (f'{key}.ttf', f'{key}bd.ttf', 'arial.ttf', 'arialbd.ttf')
    order = (1 if bold and italic else 2 if bold else 3 if italic else 0)
    for name in (candidates[order],) + tuple(candidates):
        path = os.path.join(win, name)
        if os.path.isfile(path):
            return cc.posix(path)
    return cc.posix(os.path.join(win, 'arial.ttf'))


def _text_content(material):
    raw = material.get('content')
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {'text': raw, 'styles': []}
    if isinstance(raw, dict):
        return dict(raw)
    return {'text': str(raw or ''), 'styles': []}


def _write_text_content(material, data):
    material['content'] = json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def _style_from_material(material):
    """Best-effort read of the current visual style."""
    data = _text_content(material)
    styles = data.get('styles') or [{}]
    first = styles[0] if isinstance(styles[0], dict) else {}
    solid = (((first.get('fill') or {}).get('content') or {}).get('solid') or {})
    rgb = solid.get('color') or [1, 1, 1]
    strokes = (first.get('strokes') or [{}])
    stroke = strokes[0] if strokes and isinstance(strokes[0], dict) else {}
    stroke_solid = ((stroke.get('content') or {}).get('solid') or {})
    base = {
        'content': data.get('text', material.get('name') or ''),
        'color': hex_from_rgb(rgb),
        'fontFamily': material.get('font_name') or first.get('font', {}).get('family') or 'Arial',
        'fontSize': float(first.get('size') or 9.0) / 100.0,
        'fontWeight': 700 if first.get('bold') else 400,
        'italic': bool(first.get('italic')),
        'underline': bool(material.get('underline') or first.get('underline')),
        'strikethrough': bool(material.get('strikethrough')),
        'strokeColor': hex_from_rgb(stroke_solid.get('color') or [0, 0, 0]),
        'strokeWidth': float(stroke.get('width') or 0.0),
        'shadow': bool(material.get('has_shadow')),
        'align': {0: 'left', 1: 'center', 2: 'right'}.get(
            material.get('alignment'), 'center'),
        'lineHeight': 1.2 + (float(material.get('line_spacing') or 0.02) - 0.02) * 20,
        'tracking': float(material.get('letter_spacing') or 0.0) / 0.05,
        'maxWidth': float(material.get('line_max_width') or 0.82),
        'maxHeight': 0.0,
        'background': '',
        'backgroundPaddingX': 0.0315,
        'backgroundPaddingY': 0.018,
        'backgroundRadius': 0.0,
        'opacity': float(material.get('global_alpha') or 1.0),
        'textCase': 'original',
    }
    if material.get('background_color'):
        base['background'] = material.get('background_color')
        base['backgroundRadius'] = float(material.get('background_round_radius') or 0.0)
        base['backgroundPaddingX'] = float(material.get('background_width') or 0.0315)
        base['backgroundPaddingY'] = float(material.get('background_height') or 0.018)
    material.setdefault('mcp_style', base)
    material.setdefault('mcp_runs', [])
    return base


def _normalize_text_style(style, base=None):
    base = dict(base or {})
    merged = {
        'content': base.get('content', 'Your text'),
        'color': base.get('color', '#ffffff'),
        'fontFamily': base.get('fontFamily', 'Arial'),
        'fontSize': base.get('fontSize', 0.09),
        'fontWeight': base.get('fontWeight', 700),
        'italic': base.get('italic', False),
        'underline': base.get('underline', False),
        'strikethrough': base.get('strikethrough', False),
        'strokeColor': base.get('strokeColor', '#000000'),
        'strokeWidth': base.get('strokeWidth', 0.0),
        'shadow': base.get('shadow', True),
        'align': base.get('align', 'center'),
        'lineHeight': base.get('lineHeight', 1.2),
        'tracking': base.get('tracking', 0.0),
        'maxWidth': base.get('maxWidth', 0.82),
        'maxHeight': base.get('maxHeight', 0.0),
        'background': base.get('background', ''),
        'backgroundPaddingX': base.get('backgroundPaddingX', 0.0315),
        'backgroundPaddingY': base.get('backgroundPaddingY', 0.018),
        'backgroundRadius': base.get('backgroundRadius', 0.0),
        'opacity': base.get('opacity', 1.0),
        'textCase': base.get('textCase', 'original'),
        'runs': list(base.get('runs') or []),
    }
    for key, value in (style or {}).items():
        if key == 'runs':
            merged['runs'] = list(value or [])
        elif key in merged:
            merged[key] = value
    return merged


def _apply_case(text, case):
    case = str(case or 'original').lower()
    if case == 'upper':
        return text.upper()
    if case == 'lower':
        return text.lower()
    if case == 'sentence':
        return text[:1].upper() + text[1:].lower() if text else text
    return text


def _build_text_styles(material):
    style = material.get('mcp_style') or _style_from_material(material)
    text = style.get('content', '')
    display = _apply_case(text, style.get('textCase'))
    material['mcp_original_text'] = text
    material['mcp_text_case'] = style.get('textCase', 'original')

    color = parse_hex_color(style.get('color') or '#ffffff') or (1, 1, 1, 1)
    stroke = parse_hex_color(style.get('strokeColor') or '#000000') or (0, 0, 0, 1)
    font_path = _font_path(style.get('fontFamily'), int(style.get('fontWeight') or 400) >= 600,
                           bool(style.get('italic')))
    size = round(float(style.get('fontSize') or 0.09) * 100.0, 3)
    base_style = {
        'fill': {'content': {'render_type': 'solid',
                             'solid': {'alpha': 1.0, 'color': list(color[:3])}}},
        'font': {'id': '', 'path': font_path},
        'range': [0, utc16_len(display)],
        'size': size,
        'bold': int(style.get('fontWeight') or 400) >= 600,
        'italic': bool(style.get('italic')),
        'underline': bool(style.get('underline')),
        'strokes': [],
    }
    if float(style.get('strokeWidth') or 0) > 0:
        base_style['strokes'] = [{
            'content': {'solid': {'alpha': 1.0, 'color': list(stroke[:3])}},
            'width': float(style.get('strokeWidth')),
            'mode': 0,
        }]
    if style.get('shadow'):
        base_style['shadows'] = [{
            'diffuse': 0.025,
            'alpha': 0.9,
            'distance': 5.0,
            'content': {'solid': {'color': [0, 0, 0]}},
            'angle': -45.0,
        }]

    styles = [base_style]
    for run in style.get('runs') or []:
        start_cp = run.get('start')
        end_cp = run.get('end')
        if start_cp is None or end_cp is None or end_cp <= start_cp:
            continue
        start = cp_to_utf16(text, start_cp)
        end = cp_to_utf16(text, end_cp)
        if end <= start:
            continue
        item = deepcopy(base_style)
        item['range'] = [start, end]
        run_color = parse_hex_color(run.get('color')) if run.get('color') else None
        if run_color:
            item['fill'] = {'content': {'render_type': 'solid',
                                        'solid': {'alpha': 1.0,
                                                  'color': list(run_color[:3])}}}
        if run.get('fontScale'):
            item['size'] = size * float(run['fontScale'])
        if run.get('fontWeight') is not None:
            item['bold'] = int(run['fontWeight']) >= 600
        if run.get('italic') is not None:
            item['italic'] = bool(run['italic'])
        if run.get('underline') is not None:
            item['underline'] = bool(run['underline'])
        if run.get('strikethrough'):
            item['strike_through'] = True
        if run.get('strokeColor'):
            rc = parse_hex_color(run['strokeColor']) or stroke
            item['strokes'] = [{'content': {'solid': {'alpha': 1.0,
                                                       'color': list(rc[:3])}},
                                'width': float(style.get('strokeWidth') or 0.04),
                                'mode': 0}]
        styles.append(item)

    data = {
        'text': display,
        'styles': styles,
        'layer_weight': 1,
        'effect': [],
    }
    _write_text_content(material, data)

    # top-level style fields CapCut also reads
    material['type'] = 'subtitle' if str(style.get('textCase')) != 'original' else 'text'
    material['font_name'] = style.get('fontFamily') or 'Arial'
    material['font_path'] = font_path
    material['font_size'] = size
    material['text_color'] = hex_from_rgb(color[:3])
    material['text_alpha'] = float(color[3])
    material['global_alpha'] = float(style.get('opacity') or 1.0)
    material['alignment'] = {'left': 0, 'center': 1, 'right': 2}.get(
        style.get('align'), 1)
    material['bold_width'] = 0.05 if int(style.get('fontWeight') or 400) >= 600 else 0.0
    material['italic_degree'] = 1 if style.get('italic') else 0
    material['underline'] = bool(style.get('underline'))
    material['strikethrough'] = bool(style.get('strikethrough'))
    material['letter_spacing'] = float(style.get('tracking') or 0.0) * 0.05
    material['line_spacing'] = 0.02 + (float(style.get('lineHeight') or 1.2) - 1.2) * 0.2
    material['line_max_width'] = float(style.get('maxWidth') or 0.82)
    material['force_apply_line_max_width'] = bool(style.get('maxWidth'))
    material['fixed_height'] = float(style.get('maxHeight') or -1.0)
    material['border_color'] = hex_from_rgb(stroke[:3])
    material['border_width'] = float(style.get('strokeWidth') or 0.0)
    material['border_alpha'] = 1.0
    if style.get('shadow'):
        material['has_shadow'] = True
        material['shadow_color'] = '#000000'
        material['shadow_alpha'] = 0.9
        material['shadow_distance'] = 5.0
        material['shadow_smoothing'] = 0.45
        material['shadow_angle'] = -45.0
    else:
        material['has_shadow'] = False
        material['shadow_color'] = ''
    background = parse_hex_color(style.get('background')) if style.get('background') else None
    if background:
        material['background_color'] = hex_from_rgb(background[:3], background[3])
        material['background_alpha'] = float(background[3])
        material['background_style'] = 1
        material['background_round_radius'] = float(style.get('backgroundRadius') or 0.0)
        material['background_width'] = float(style.get('backgroundPaddingX') or 0.0315) * 2
        material['background_height'] = float(style.get('backgroundPaddingY') or 0.018) * 2
        material['background_horizontal_offset'] = 0.0
        material['background_vertical_offset'] = 0.0
    else:
        material['background_color'] = ''
        material['background_alpha'] = 1.0
        material['background_style'] = 0
    return display


def _default_text_style(content='Your text'):
    return {
        'content': content,
        'color': '#ffffff',
        'fontFamily': 'Arial',
        'fontSize': 0.09,
        'fontWeight': 700,
        'italic': False,
        'underline': False,
        'strikethrough': False,
        'strokeColor': '#000000',
        'strokeWidth': 0.0,
        'shadow': True,
        'align': 'center',
        'lineHeight': 1.2,
        'tracking': 0.0,
        'maxWidth': 0.82,
        'maxHeight': 0.0,
        'background': '',
        'backgroundPaddingX': 0.0315,
        'backgroundPaddingY': 0.018,
        'backgroundRadius': 0.0,
        'opacity': 1.0,
        'textCase': 'original',
        'runs': [],
    }


def new_text_material(text, style=None):
    m = _base_material('texts', {
        'id': '', 'type': 'text', 'content': '', 'name': '', 'global_alpha': 1.0,
        'line_spacing': 0.02, 'has_shadow': False, 'text_color': '#FFFFFF',
        'text_alpha': 1.0, 'font_name': '', 'font_size': 7.0, 'font_path': '',
        'font_id': '', 'initial_scale': 1.0, 'typesetting': 0, 'alignment': 1,
        'line_feed': 1, 'is_rich_text': False, 'underline': False,
        'italic_degree': 0, 'bold_width': 0.0, 'border_alpha': 1.0,
        'border_color': '', 'border_width': 0.08, 'border_mode': 0,
        'background_color': '', 'background_alpha': 1.0, 'background_style': 0,
        'background_round_radius': 0.0, 'background_width': 0.14,
        'background_height': 0.14, 'background_vertical_offset': 0.0,
        'background_horizontal_offset': 0.0, 'background_fill': '',
        'single_char_bg_enable': False, 'single_char_bg_color': '',
        'single_char_bg_alpha': 1.0, 'single_char_bg_round_radius': 0.3,
        'single_char_bg_width': 0.0, 'single_char_bg_height': 0.0,
        'single_char_bg_vertical_offset': 0.0,
        'single_char_bg_horizontal_offset': 0.0, 'font_team_id': '',
        'tts_auto_update': False, 'text_preset_resource_id': '', 'group_id': '',
        'preset_id': '', 'preset_name': '', 'preset_category': '',
        'preset_category_index': 0, 'preset_has_set_alignment': False,
        'force_apply_line_max_width': False, 'language': 'en-US',
        'relevance_segment': [], 'original_size': [], 'fixed_width': -1.0,
        'fixed_height': -1.0, 'autoAdaptCanvasEnabled': False,
        'line_max_width': 0.82, 'oneline_cutoff': False, 'cutoff_postfix': '',
        'subtitle_template_original_fontsize': 0.0, 'subtitle_keywords': None,
        'inner_padding': -1.0, 'multi_language_current': 'none',
        'source_from': '', 'is_lyric_effect': False, 'lyric_group_id': '',
        'is_batch_replace': False, 'is_words_linear': False, 'ssml_content': '',
        'subtitle_keywords_config': None, 'sub_template_id': -1,
        'translate_original_text': '', 'adjust_params': [],
    })
    m['id'] = cc.uid()
    for key, value in {
        'recognize_task_id': '', 'recognize_text': '', 'recognize_model': '',
        'punc_model': '', 'base_content': '', 'group_id': '', 'preset_id': '',
        'preset_name': '', 'preset_category': '', 'preset_category_id': '',
        'preset_index': 0, 'text_preset_resource_id': '',
        'relevance_segment': [], 'original_size': [],
        'translate_original_text': '', 'subtitle_keywords': None,
        'subtitle_keywords_config': None, 'words': {'start_time': [],
                                                     'end_time': [], 'text': []},
        'current_words': {'start_time': [], 'end_time': [], 'text': []},
    }.items():
        m[key] = value
    base = _default_text_style(text or 'Your text')
    base.update(style or {})
    m['mcp_style'] = _normalize_text_style(base, base)
    m['mcp_runs'] = list(m['mcp_style'].get('runs') or [])
    _build_text_styles(m)
    m['name'] = (text or 'Your text').splitlines()[0][:40]
    return m


def new_effect_material(ident, params=None, target='segment', segment_id=''):
    found = cat.find_effect(ident)
    m = _base_material('video_effects', {
        'type': 'video_effect', 'name': str(ident), 'effect_id': str(ident),
        'resource_id': str(ident), 'value': 1.0, 'adjust_params': [],
        'apply_target_type': 0 if target == 'segment' else 2,
        'category_id': '', 'category_name': '', 'platform': 'all',
        'source_platform': 1, 'time_range': None, 'render_index': 11000,
        'track_render_index': 0, 'common_keyframes': [], 'request_id': '',
        'algorithm_artifact_path': '', 'disable_effect_faces': [],
        'covering_relation_change': 0, 'enable_mask': True, 'effect_mask': [],
        'enable_video_mask_stroke': True, 'enable_video_mask_shadow': True,
        'aigc_current_artifact_path': '', 'aigc_current_artifact_cnt': 0,
        'aigc_current_artifact_freeze_time': -1,
        'aigc_current_artifact_freeze_progress': 0.0, 'sdk_extra': '',
    })
    m['id'] = cc.uid()
    if found:
        m['name'] = found.get('name') or found.get('slug') or str(ident)
        m['effect_id'] = found.get('effect_id') or str(ident)
        m['resource_id'] = found.get('resource_id') or m['effect_id']
        m['type'] = 'face_effect' if found.get('group') == 'character_effects' else 'video_effect'
    m['apply_target_type'] = 0 if target == 'segment' else 2
    m['bind_segment_id'] = segment_id if target == 'segment' else ''
    m['adjust_params'] = []
    for key, value in (params or {}).items():
        try:
            m['adjust_params'].append({'name': str(key), 'value': float(value),
                                       'default_value': float(value)})
        except (TypeError, ValueError):
            continue
    m['mcp_params'] = dict(params or {})
    m['mcp_keys'] = {}
    return m


def new_filter_material(ident, params=None, target='segment', segment_id=''):
    m = new_effect_material(ident, params, target, segment_id)
    m['type'] = 'filter'
    m['sub_type'] = 'none'
    m['value'] = 1.0
    m['algorithm_artifact_path'] = ''
    m['bloom_params'] = None
    m['color_match_info'] = {'source_feature_path': '',
                             'target_feature_path': '',
                             'target_image_path': ''}
    m['enable_skin_tone_correction'] = False
    m['exclusion_group'] = []
    m['face_adjust_params'] = []
    m['intensity_key'] = ''
    m['multi_language_current'] = ''
    m['panel_id'] = ''
    return m


def new_mix_mode_material(blend):
    key = str(blend or '').strip().lower().replace('_', '-').replace(' ', '-')
    name, effect_id, resource_id = MIX_MODES.get(key, (str(blend), str(blend), str(blend)))
    m = _base_material('video_effects', {
        'type': 'mix_mode', 'name': name, 'effect_id': effect_id,
        'resource_id': resource_id, 'value': 1.0, 'apply_target_type': 0,
        'platform': 'all', 'source_platform': 0, 'category_id': '',
        'category_name': '', 'sub_type': 'none', 'time_range': None,
    })
    m.update({
        'id': cc.uid(), 'type': 'mix_mode', 'name': name,
        'effect_id': effect_id, 'resource_id': resource_id, 'value': 1.0,
        'apply_target_type': 0, 'platform': 'all', 'source_platform': 0,
        'category_id': '', 'category_name': '', 'sub_type': 'none',
        'time_range': None,
    })
    return m


def _set_blend(project, segment, blend):
    content = project.content
    refs = segment.setdefault('extra_material_refs', [])
    for ref in list(refs):
        mat = _find_material_by_id(content, ref)
        if isinstance(mat, dict) and mat.get('type') == 'mix_mode':
            refs.remove(ref)
            _remove_material(content, ref)
    if not blend:
        segment['mcp_blend'] = ''
        return None
    mat = new_mix_mode_material(blend)
    _add_material(content, 'video_effects', mat)
    refs.append(mat['id'])
    segment['mcp_blend'] = str(blend)
    return mat['id']


def new_transition_material(ident, seconds=None):
    found = cat.find_transition(ident)
    m = _base_material('transitions', {
        'type': 'transition', 'name': str(ident), 'effect_id': str(ident),
        'resource_id': str(ident), 'duration': usec(seconds or 0.5),
        'is_overlap': False, 'category_id': '', 'category_name': '',
        'platform': 'all',
    })
    m['id'] = cc.uid()
    if found:
        m['name'] = found.get('name') or str(ident)
        m['effect_id'] = found.get('effect_id') or str(ident)
        m['resource_id'] = found.get('resource_id') or m['effect_id']
        m['duration'] = usec(seconds) if seconds is not None else int(
            found.get('default_duration') or usec(0.5))
        m['is_overlap'] = bool(found.get('is_overlap'))
    elif seconds is not None:
        m['duration'] = usec(seconds)
    m['type'] = 'transition'
    return m


def new_mask_material(spec):
    shape = str((spec.get('source') or {}).get('shape') or spec.get('shape') or 'circle')
    lookup = {'linear': 'split', 'mirror': 'filmstrip', 'star': 'stars'}.get(shape, shape)
    found = cat.find_mask(lookup) or cat.find_mask(shape)
    m = _base_material('common_mask', {
        'type': 'mask', 'resource_type': shape, 'name': shape.title(),
        'platform': 'all', 'resource_id': '', 'position_info': '',
        'category': 'video', 'category_id': 'jichu',
        'category_name': '基础', 'is_old_version': False,
        'loader_work_space': '', 'panel': '', 'source_platform': 0,
        'track_segment': '',
        'text_config': {'align_type': 15, 'bold_width': 0.0,
                        'char_spacing': 0.0, 'content': '', 'font_name': '',
                        'font_path': '', 'font_resource_id': '',
                        'font_size': 15.0, 'has_underline': False,
                        'italic_degree': 0, 'line_gap': 0.0, 'scale': 1.0},
    })
    m['id'] = cc.uid()
    m['type'] = 'mask'
    m['resource_type'] = found.get('resource_type') if found else shape
    m['name'] = (found or {}).get('name') or shape.title()
    m['resource_id'] = (found or {}).get('resource_id') or ''
    src = spec.get('source') or {}
    scale = float(spec.get('scale') or 1.0)
    width = float(src.get('width') if src.get('width') is not None else 0.66)
    height = float(src.get('height') if src.get('height') is not None else 0.66)
    config = {
        'aspectRatio': float((found or {}).get('default_aspect_ratio') or 1.0),
        'centerX': float(spec.get('x') or 0.0) * 2.0,
        'centerY': -float(spec.get('y') or 0.0) * 2.0,
        'feather': clamp(float(spec.get('feather') or 0.0), 0, 1),
        'height': height * scale,
        'invert': bool(spec.get('invert')),
        'rotation': float(spec.get('rotation') or 0.0),
        'roundCorner': clamp(float(src.get('roundness') or spec.get('roundness') or 0.0), 0, 1),
        'width': width * scale,
        'expansion': float(spec.get('expand') or 0.0),
    }
    m['config'] = config
    m['mcp_mask'] = {
        'shape': shape,
        'mode': spec.get('mode') or 'add',
        'opacity': float(spec.get('opacity') if spec.get('opacity') is not None else 1.0),
        'id': spec.get('id') or '',
        'keys': {} if not spec.get('keys') else deepcopy(spec.get('keys')),
    }
    m['mcp_keys'] = deepcopy(spec.get('keys') or {})
    return m


def new_animation_material(animations, track_kind='video', segment_duration=0):
    """animations: list of (slot, package, seconds)."""
    m = _base_material('material_animations', {
        'type': 'sticker_animation', 'animations': [],
        'multi_language_current': 'none',
    })
    m['id'] = cc.uid()
    m['type'] = 'sticker_animation'
    m['animations'] = []
    for slot, package, seconds in animations:
        duration = usec(seconds) if seconds is not None else int(
            package.get('duration') or 500_000)
        if segment_duration:
            duration = min(duration, int(segment_duration))
        start = max(0, int(segment_duration) - duration) if slot == 'outro' else 0
        kind = 'in' if slot == 'intro' else 'out' if slot == 'outro' else 'group'
        category = {'in': 'ruchang', 'out': 'chuchang',
                    'group': 'zuhe'}.get(kind, 'ruchang')
        material_type = 'text' if track_kind == 'text' else (
            'sticker' if track_kind == 'sticker' else 'video')
        m['animations'].append({
            'id': package.get('effect_id') or package.get('resource_id') or cc.uid(),
            'type': kind,
            'start': start,
            'duration': duration,
            'path': '',
            'platform': 'all',
            'resource_id': package.get('resource_id') or package.get('effect_id'),
            'third_resource_id': '0',
            'source_platform': 1,
            'name': package.get('title') or package.get('name') or package.get('slug'),
            'category_id': '',
            'category_name': category,
            'panel': '',
            'material_type': material_type,
            'anim_adjust_params': None,
            'request_id': '',
        })
    return m


def new_effect_track():
    track = _schema_track('effect')
    if track:
        track = deepcopy(track)
        track['id'] = cc.uid()
        track['segments'] = []
        track['name'] = 'effects'
        return track
    return {'id': cc.uid(), 'type': 'effect', 'segments': [], 'flag': 0,
            'attribute': 0, 'name': 'effects', 'is_default_name': True}


def new_track(kind):
    kind = _KIND_BY_TRACK.get(kind, kind)
    track = _schema_track(kind)
    if track:
        track = deepcopy(track)
        track['id'] = cc.uid()
        track['segments'] = []
        track['type'] = kind if kind != 'image' else 'video'
        track['flag'] = 1 if kind == 'text' else 0
        track['name'] = kind
        track['is_default_name'] = True
        return track
    return {'id': cc.uid(), 'type': kind if kind != 'image' else 'video',
            'segments': [], 'flag': 1 if kind == 'text' else 0,
            'attribute': 0, 'name': kind, 'is_default_name': True}


# ---------------------------------------------------------------- material lookups

def _materials(content):
    return content.setdefault('materials', {})


def _find_material_by_id(content, material_id):
    if not material_id:
        return None
    for value in _materials(content).values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get('id') == material_id:
                    return item
    return None


def _material_list(content, key):
    arr = _materials(content).get(key)
    if not isinstance(arr, list):
        arr = []
        _materials(content)[key] = arr
    return arr


def _add_material(content, key, material):
    _material_list(content, key).append(material)
    return material


def _remove_material(content, material_id):
    for key, arr in _materials(content).items():
        if isinstance(arr, list):
            _materials(content)[key] = [m for m in arr
                                        if not (isinstance(m, dict) and m.get('id') == material_id)]


def _segment_material(content, segment):
    return _find_material_by_id(content, segment.get('material_id'))


def _material_path(material):
    return (material or {}).get('path') or ''


def _segment_track(content, segment_id):
    for track in content.get('tracks') or []:
        for segment in track.get('segments') or []:
            if segment.get('id') == segment_id:
                return track
    return None


def _find_segment(content, ident):
    wanted = str(ident or '').strip()
    if not wanted:
        raise RuntimeError('clip id is required')
    exact = []
    prefix = []
    for track in content.get('tracks') or []:
        for segment in track.get('segments') or []:
            sid = str(segment.get('id') or '')
            if sid == wanted:
                exact.append((track, segment))
            elif len(wanted) >= 6 and sid.startswith(wanted):
                prefix.append((track, segment))
    found = exact or prefix
    if not found:
        raise RuntimeError(f'clip not found: {ident!r}')
    if len(found) > 1:
        raise RuntimeError(f'clip id prefix is ambiguous: {ident!r} ({len(found)} matches)')
    return found[0]


def _track_by_id(content, track_id):
    for track in content.get('tracks') or []:
        if track.get('id') == track_id:
            return track
    raise RuntimeError(f'track not found: {track_id!r}')


def _track_kind(track):
    return _KIND_BY_TRACK.get(cc.track_type(track), cc.track_type(track))


def _main_video_track(content):
    for track in content.get('tracks') or []:
        if _track_kind(track) == 'video' and track.get('segments'):
            return track
    return None


def _linkage_on(content):
    cfg = content.get('config') or {}
    value = cfg.get('maintrack_adsorb')
    if value is None:
        value = content.get('mcp_linkage', True)
    return bool(value)


def _set_linkage(content, on):
    cfg = content.setdefault('config', {})
    cfg['maintrack_adsorb'] = bool(on)
    content['mcp_linkage'] = bool(on)


def _segment_end(segment):
    tr = segment.get('target_timerange') or {}
    return int(tr.get('start') or 0) + int(tr.get('duration') or 0)


def _segment_start(segment):
    return int((segment.get('target_timerange') or {}).get('start') or 0)


def _segment_duration(segment):
    return int((segment.get('target_timerange') or {}).get('duration') or 0)


def _source_start(segment):
    return int((segment.get('source_timerange') or {}).get('start') or 0)


def _source_duration(segment):
    return int((segment.get('source_timerange') or {}).get('duration') or 0)


def _segments_overlapping(track, start, end, ignore=()):
    out = []
    for segment in track.get('segments') or []:
        if segment.get('id') in ignore:
            continue
        if _segment_start(segment) < end and _segment_end(segment) > start:
            out.append(segment)
    return out


def _shift_linked(project, track, after, delta, exclude=()):
    if not delta or not _linkage_on(project.content):
        return
    main = _main_video_track(project.content)
    if track is not main:
        return
    skip = set(exclude or ())
    for other in project.content.get('tracks') or []:
        if other is track or other.get('id') in skip:
            continue
        _shift_segments(other, after, delta)


def _shift_segments(track, after, delta, ignore=()):
    if not delta:
        return
    for segment in track.get('segments') or []:
        if segment.get('id') in ignore:
            continue
        if _segment_start(segment) >= after:
            tr = segment.setdefault('target_timerange', {})
            tr['start'] = max(0, int(tr.get('start') or 0) + int(delta))
    track['segments'] = sorted(track.get('segments') or [],
                               key=lambda s: _segment_start(s))


def _refresh_track_indices(content):
    for index, track in enumerate(content.get('tracks') or []):
        for segment in track.get('segments') or []:
            segment['track_render_index'] = index
            segment['render_index'] = (segment.get('render_index') or 0) if index == 0 else max(
                int(segment.get('render_index') or 0), index * 1000)


def _sort_tracks(content):
    # keep JSON order (bottom to top); only sort segments within a track
    for track in content.get('tracks') or []:
        track['segments'] = sorted(track.get('segments') or [],
                                   key=lambda s: _segment_start(s))


def _recompute_duration(content):
    end = 0
    for track in content.get('tracks') or []:
        for segment in track.get('segments') or []:
            end = max(end, _segment_end(segment))
    content['duration'] = int(end)
    return end


def _prune_materials(content):
    """Drop companion materials no segment references anymore."""
    referenced = set()
    for track in content.get('tracks') or []:
        for segment in track.get('segments') or []:
            if segment.get('material_id'):
                referenced.add(segment['material_id'])
            for ref in segment.get('extra_material_refs') or []:
                referenced.add(ref)
    mats = _materials(content)
    for key, arr in list(mats.items()):
        if not isinstance(arr, list):
            continue
        mats[key] = [m for m in arr
                     if not isinstance(m, dict) or not m.get('id') or m['id'] in referenced]


# ---------------------------------------------------------------- project model

class Project:
    def __init__(self, folder):
        self.folder = os.path.abspath(folder)
        self.content_path = cc.content_path(self.folder)
        if not self.content_path:
            raise RuntimeError(f'not a CapCut draft folder: {self.folder}')
        self.meta_path = os.path.join(self.folder, 'draft_meta_info.json')
        self.content = cc.read_json(self.content_path)
        self.meta = cc.read_json(self.meta_path) if os.path.isfile(self.meta_path) else {}
        self.history = []
        self.redo_stack = []
        self.dirty = False
        self._editing = 0
        self._last_label = ''
        self.bin = self._load_bin()

    # -- state
    @property
    def name(self):
        return (self.meta.get('draft_name') or self.content.get('name')
                or os.path.basename(self.folder))

    @property
    def canvas(self):
        c = self.content.get('canvas_config') or {}
        return {'width': int(c.get('width') or 1920),
                'height': int(c.get('height') or 1080),
                'ratio': c.get('ratio') or '16:9'}

    @property
    def fps(self):
        return project_fps(self.content)

    def _load_bin(self):
        path = os.path.join(self.folder, BIN_NAME)
        if os.path.isfile(path):
            try:
                data = cc.read_json(path)
                return {'items': list(data.get('items') or [])}
            except Exception:
                pass
        return {'items': []}

    def _save_bin(self):
        path = os.path.join(self.folder, BIN_NAME)
        cc.write_json(path, {'items': self.bin.get('items') or []})

    # -- history
    def _pack(self):
        return (zlib.compress(json.dumps(self.content, ensure_ascii=False,
                                         separators=(',', ':')).encode()),
                zlib.compress(json.dumps(self.meta, ensure_ascii=False,
                                         separators=(',', ':')).encode()))

    def _unpack(self, packed):
        content, meta = packed
        self.content = json.loads(zlib.decompress(content).decode())
        self.meta = json.loads(zlib.decompress(meta).decode())

    def snapshot(self, label=''):
        self.history.append((self._pack(), self.dirty))
        if len(self.history) > MAX_HISTORY:
            self.history.pop(0)
        self.redo_stack.clear()
        self._last_label = label
        self.dirty = True

    @contextmanager
    def edit(self, label='edit'):
        _require_capcut_closed()
        if self._editing:
            yield self
            return
        self.snapshot(label)
        self._editing = 1
        try:
            yield self
            _recompute_duration(self.content)
            _sort_tracks(self.content)
        except Exception:
            self._editing = 0
            if self.history:
                self._unpack(self.history.pop()[0])
            raise
        finally:
            self._editing = 0

    def undo(self):
        if not self.history:
            raise RuntimeError('nothing to undo')
        self.redo_stack.append((self._pack(), self.dirty))
        packed, dirty = self.history.pop()
        self._unpack(packed)
        self.dirty = True
        self._last_label = 'undo'
        return self.summary()

    def redo(self):
        if not self.redo_stack:
            raise RuntimeError('nothing to redo')
        self.history.append((self._pack(), self.dirty))
        packed, dirty = self.redo_stack.pop()
        self._unpack(packed)
        self.dirty = True
        self._last_label = 'redo'
        return self.summary()

    # -- io
    def _make_cover(self):
        cover = os.path.join(self.folder, 'draft_cover.jpg')
        if os.path.isfile(cover):
            return cover
        for track in self.content.get('tracks') or []:
            if _track_kind(track) not in ('video', 'image'):
                continue
            for segment in track.get('segments') or []:
                material = _segment_material(self.content, segment) or {}
                path = material.get('path')
                if not path or not os.path.isfile(path):
                    continue
                src = _source_start(segment) + max(0, _source_duration(segment) // 2)
                try:
                    subprocess.run(
                        ['ffmpeg', '-y', '-v', 'error', '-ss', f'{src / 1e6:.3f}',
                         '-i', path, '-frames:v', '1', '-vf', 'scale=480:-2', cover],
                        capture_output=True, timeout=120)
                except Exception:
                    return None
                if os.path.isfile(cover):
                    return cover
                return None
        return None

    def save(self, name=None):
        _require_capcut_closed()
        if name:
            new_name = str(name).strip()
            if not new_name:
                raise RuntimeError('project name cannot be empty')
            self.content['name'] = new_name
            self.meta['draft_name'] = new_name
        _recompute_duration(self.content)
        _sort_tracks(self.content)
        errors = cc.validate(self.content)
        if errors:
            raise RuntimeError('draft validation failed: ' + '; '.join(errors[:8]))
        cc.write_json(self.content_path, self.content)
        cc.sync_backup(self.content_path, self.content)
        self.meta['tm_duration'] = self.content['duration']
        self.meta['tm_draft_modified'] = cc.now_us()
        self.meta['draft_name'] = self.name
        self.meta['draft_fold_path'] = cc.posix(self.folder)
        self.meta['draft_json_file'] = cc.posix(self.content_path)
        cover = self._make_cover()
        if cover:
            self.meta['draft_cover'] = os.path.basename(cover)
        if os.path.isfile(self.meta_path):
            cc.write_json(self.meta_path, self.meta)
        self._save_bin()
        # keep root listing in step
        root = os.path.dirname(self.folder)
        root_meta_path = os.path.join(root, 'root_meta_info.json')
        if os.path.isfile(root_meta_path):
            try:
                root_meta = cc.read_json(root_meta_path)
                for entry in root_meta.get('all_draft_store') or []:
                    if not isinstance(entry, dict):
                        continue
                    fold = cc.posix(entry.get('draft_fold_path', '')).rstrip('/')
                    if fold.lower().endswith(os.path.basename(self.folder).lower()):
                        entry['draft_name'] = self.name
                        entry['tm_duration'] = self.content['duration']
                        entry['tm_draft_modified'] = cc.now_us()
                        if self.meta.get('draft_cover'):
                            entry['draft_cover'] = cc.posix(
                                os.path.join(self.folder, self.meta['draft_cover']))
                cc.write_json(root_meta_path, root_meta)
            except Exception:
                pass
        self.dirty = False
        return self.summary()

    def summary(self):
        return {
            'ok': True,
            'path': self.folder,
            'name': self.name,
            'duration': sec(self.content.get('duration')),
            'frame': {
                'width': self.canvas['width'],
                'height': self.canvas['height'],
                'ratio': self.canvas['ratio'],
                'fps': self.fps,
                'rateNum': (self.content.get('mcp_rate') or {}).get('rateNum') or int(round(self.fps)),
                'rateDen': (self.content.get('mcp_rate') or {}).get('rateDen') or 1,
            },
            'dirty': self.dirty,
            'tracks': len(self.content.get('tracks') or []),
            'segments': sum(len(t.get('segments') or []) for t in self.content.get('tracks') or []),
        }

    def timeline(self):
        tracks = []
        for index, track in enumerate(reversed(self.content.get('tracks') or [])):
            items = []
            for segment in track.get('segments') or []:
                mat = _segment_material(self.content, segment) or {}
                kind = _track_kind(track)
                item = {
                    'id': segment.get('id'),
                    'kind': kind,
                    'start': sec(_segment_start(segment)),
                    'duration': sec(_segment_duration(segment)),
                    'end': sec(_segment_end(segment)),
                    'speed': float(segment.get('speed') or 1.0),
                    'volume': float(segment.get('volume') if segment.get('volume') is not None else 1.0),
                    'materialId': segment.get('material_id'),
                    'name': mat.get('material_name') or mat.get('name') or os.path.basename(_material_path(mat) or ''),
                    'path': _material_path(mat),
                    'visible': bool(segment.get('visible', True)),
                }
                if kind == 'text':
                    data = _text_content(mat)
                    item['text'] = data.get('text', '')
                    item['style'] = mat.get('mcp_style') or _style_from_material(mat)
                if kind in ('video', 'image'):
                    src = segment.get('source_timerange') or {}
                    item['sourceStart'] = sec(src.get('start'))
                    item['sourceDuration'] = sec(src.get('duration'))
                    item['transform'] = deepcopy(segment.get('clip') or {})
                    item['effects'] = [
                        {'id': r.get('id'), 'name': r.get('name'),
                         'type': r.get('type')}
                        for r in (_find_material_by_id(self.content, ref)
                                  for ref in segment.get('extra_material_refs') or [])
                        if isinstance(r, dict) and r.get('type') in ('video_effect', 'filter', 'face_effect')
                    ]
                    item['mask'] = bool(segment.get('mcp_masks'))
                if kind == 'audio':
                    src = segment.get('source_timerange') or {}
                    item['sourceStart'] = sec(src.get('start'))
                    item['sourceDuration'] = sec(src.get('duration'))
                items.append(item)
            tracks.append({'id': track.get('id'), 'type': cc.track_type(track),
                           'kind': _track_kind(track), 'segments': items,
                           'segmentCount': len(items)})
        media = []
        seen = set()
        for track in self.content.get('tracks') or []:
            for segment in track.get('segments') or []:
                mat = _segment_material(self.content, segment)
                if not mat:
                    continue
                key = mat.get('id')
                if key in seen:
                    continue
                seen.add(key)
                media.append({'id': key, 'path': _material_path(mat),
                              'kind': 'image' if mat.get('type') == 'photo' else (
                                  'audio' if _track_kind(track) == 'audio' else 'video'),
                              'name': mat.get('material_name') or mat.get('name'),
                              'duration': sec(mat.get('duration')),
                              'used': True,
                              'exists': os.path.isfile(mat.get('path') or '')})
        for item in self.bin.get('items') or []:
            if item.get('id') in seen:
                continue
            seen.add(item.get('id'))
            media.append({**item, 'used': False,
                          'exists': os.path.isfile(item.get('path') or '')})
        captions = []
        for track in self.content.get('tracks') or []:
            if _track_kind(track) != 'text':
                continue
            for segment in track.get('segments') or []:
                mat = _segment_material(self.content, segment)
                data = _text_content(mat or {})
                captions.append({
                    'id': segment.get('id'),
                    'start': sec(_segment_start(segment)),
                    'end': sec(_segment_end(segment)),
                    'text': data.get('text', ''),
                })
        return {
            'ok': True,
            'name': self.name,
            'path': self.folder,
            'duration': sec(self.content.get('duration')),
            'frame': {'width': self.canvas['width'], 'height': self.canvas['height'],
                      'ratio': self.canvas['ratio'], 'fps': self.fps,
                      'rateNum': (self.content.get('mcp_rate') or {}).get('rateNum') or int(round(self.fps)),
                      'rateDen': (self.content.get('mcp_rate') or {}).get('rateDen') or 1},
            'tracks': tracks,
            'media': media,
            'captions': sorted(captions, key=lambda c: c['start']),
        }


# ---------------------------------------------------------------- registry / paths

def resolve_folder(path):
    if isinstance(path, Project):
        return path.folder
    text = str(path or '').strip()
    if not text:
        raise RuntimeError('project path is required')
    candidates = []
    expanded = _expand(text)
    candidates.append(expanded)
    if os.path.isdir(text):
        candidates.append(os.path.abspath(text))
    candidates.append(os.path.join(cc.drafts_root(), text))
    for candidate in candidates:
        if os.path.isdir(candidate):
            folder = candidate
            if os.path.isfile(os.path.join(folder, 'draft_content.json')) or \
                    os.path.isfile(os.path.join(folder, 'draft_info.json')):
                return os.path.abspath(folder)
    try:
        return cc.find_draft(text)['folder']
    except Exception:
        pass
    raise RuntimeError(f'CapCut project not found: {path!r}')


def open_project(path, create=False):
    folder = resolve_folder(path)
    with _LOCK:
        if folder in _PROJECTS:
            return _PROJECTS[folder]
        project = Project(folder)
        _PROJECTS[folder] = project
        return project


def _project(path):
    return open_project(path)


def project_list():
    root = cc.drafts_root()
    drafts = cc.list_drafts(root)
    out = []
    for item in drafts:
        folder = item['folder']
        entry = {
            'path': folder,
            'name': item.get('draft_name') or item['folder_name'],
            'folder': item['folder_name'],
            'duration': item.get('duration_sec'),
            'open': folder in _PROJECTS,
        }
        try:
            content = cc.read_json(cc.content_path(folder))
            canvas = content.get('canvas_config') or {}
            entry['frame'] = {
                'width': int(canvas.get('width') or 1920),
                'height': int(canvas.get('height') or 1080),
                'fps': project_fps(content),
                'rateNum': (content.get('mcp_rate') or {}).get('rateNum') or int(round(project_fps(content))),
                'rateDen': (content.get('mcp_rate') or {}).get('rateDen') or 1,
            }
        except Exception:
            pass
        out.append(entry)
    out.sort(key=lambda e: os.path.getmtime(e['path']), reverse=True)
    return {'ok': True, 'drafts_root': root, 'projects': out}


def project_open(path):
    project = open_project(path)
    return {**project.summary(), 'project': project.timeline()}


def project_get(path):
    project = open_project(path)
    return {
        'ok': True,
        'summary': project.summary(),
        'content': project.content,
        'meta': project.meta,
        'bin': project.bin,
        'timeline': project.timeline(),
    }


def project_save(path, name=None):
    project = _project(path)
    result = project.save(name=name)
    return result


def project_close(path, save=True):
    folder = resolve_folder(path)
    project = _PROJECTS.get(folder)
    if not project:
        return {'ok': True, 'closed': folder, 'saved': False,
                'note': 'project was not open'}
    saved = False
    if save and project.dirty:
        project.save()
        saved = True
    _PROJECTS.pop(folder, None)
    return {'ok': True, 'closed': folder, 'saved': saved}


def _new_project_folder(location, name):
    location = _expand(location) if location else cc.drafts_root()
    os.makedirs(location, exist_ok=True)
    base = _sanitize(name)
    folder = os.path.join(location, base)
    n = 1
    while os.path.exists(folder):
        n += 1
        folder = os.path.join(location, f'{base}_{n}')
    return folder


def project_create(location, name, video=None):
    _require_capcut_closed()
    name = str(name or '').strip()
    if not name:
        raise RuntimeError('project name is required')
    root = cc.drafts_root()
    location = _expand(location) if location else root
    if os.path.basename(location).lower() == 'com.lveditor.draft':
        root = location
    donor = _schema_donor(root)
    _require_capcut_closed()
    dest = _new_project_folder(location, name)
    shutil.copytree(donor['folder'], dest)
    shutil.rmtree(os.path.join(dest, cc.BACKUP_DIR), ignore_errors=True)
    # The donor's timeline caches belong to the donor's clips. Keeping them
    # makes CapCut bind the new project to stale timelines.
    for junk_dir in ('Timelines', 'subdraft', 'adjust_mask', 'matting',
                     'qr_upload', 'smart_crop', 'Resources'):
        shutil.rmtree(os.path.join(dest, junk_dir), ignore_errors=True)
    for junk in ('draft.extra', 'mini_draft.json', 'template-2.tmp', '.locked',
                 'draft_content.json.bak', 'draft_cover.jpg', 'key_value.json',
                 'attachment_editing.json', 'attachment_pc_common.json',
                 BIN_NAME):
        path = os.path.join(dest, junk)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    try:
        cp = cc.content_path(dest)
        content = cc.read_json(cp)
        content['name'] = name
        content['duration'] = 0
        content['tracks'] = []
        content['keyframes'] = {k: [] for k in (content.get('keyframes') or {}).keys()} or {
            'videos': [], 'audios': [], 'texts': [], 'stickers': [],
            'filters': [], 'adjusts': [], 'handwrites': [], 'effects': []}
        content['keyframe_graph_list'] = []
        content['relationships'] = []
        content['group_container'] = None
        content['lyrics_effects'] = []
        content['time_marks'] = None
        content['uneven_animation_template_info'] = {}
        content['extra_info'] = {}
        config = content.setdefault('config', {})
        config['subtitle_recognition_id'] = ''
        config['subtitle_taskinfo'] = []
        config['lyrics_recognition_id'] = ''
        config['lyrics_taskinfo'] = []
        content['create_time'] = content.get('create_time') or 0
        content['update_time'] = content.get('update_time') or 0
        for key in list((content.get('materials') or {}).keys()):
            content['materials'][key] = []
        width, height = 1920, 1080
        rate_num, rate_den = 30, 1
        if isinstance(video, dict):
            width = int(video.get('width') or width)
            height = int(video.get('height') or height)
            rate_num = int(video.get('rateNum') or rate_num)
            rate_den = int(video.get('rateDen') or rate_den)
            canvas_color = video.get('canvas')
        else:
            canvas_color = None
        content['canvas_config'] = dict(content.get('canvas_config') or {},
                                        width=width, height=height,
                                        ratio=_ratio(width, height))
        fps_value = rate_num / float(rate_den or 1)
        content['fps'] = int(round(fps_value)) if abs(fps_value - round(fps_value)) < 1e-6 else round(fps_value, 3)
        content['mcp_rate'] = {'rateNum': rate_num, 'rateDen': rate_den}
        content['mcp_canvas_color'] = canvas_color or ''
        content['mcp_background'] = {'kind': 'none'}
        _set_linkage(content, True)
        cc.write_json(cp, content)

        meta_path = os.path.join(dest, 'draft_meta_info.json')
        meta = cc.read_json(meta_path) if os.path.isfile(meta_path) else {}
        meta['draft_name'] = name
        meta['tm_duration'] = 0
        meta['tm_draft_create'] = cc.now_us()
        meta['tm_draft_modified'] = cc.now_us()
        meta['draft_fold_path'] = cc.posix(dest)
        meta['draft_root_path'] = cc.posix(root)
        meta['draft_cover'] = ''
        meta['draft_json_file'] = cc.posix(cp)
        # Keep the donor's content id / meta draft id / timeline layout ids
        # untouched: CapCut binds the timeline through that trio.
        for group in meta.get('draft_materials') or []:
            if isinstance(group, dict) and isinstance(group.get('value'), list):
                group['value'] = []
        cc.write_json(meta_path, meta)
        cc._register_root(root, donor, name, dest, 0,
                          meta.get('draft_id') or content.get('id') or donor['name'])
        project = Project(dest)
        _PROJECTS[dest] = project
        return {**project.summary(), 'created': True,
                'frame': {'width': width, 'height': height, 'fps': content['fps']}}
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _ratio(width, height):
    def gcd(a, b):
        while b:
            a, b = b, a % b
        return a or 1
    g = gcd(int(width), int(height))
    return f'{int(width) // g}:{int(height) // g}'


def _resnap_all(project):
    fps = project.fps
    content = project.content
    for track in content.get('tracks') or []:
        for segment in track.get('segments') or []:
            tr = segment.get('target_timerange') or {}
            if tr:
                tr['start'] = int(round(round(float(tr.get('start') or 0) * fps / 1e6) * 1e6 / fps))
                tr['duration'] = max(1, int(round(round(float(tr.get('duration') or 0) * fps / 1e6) * 1e6 / fps)))
            sr = segment.get('source_timerange') or {}
            if sr:
                sr['start'] = int(round(round(float(sr.get('start') or 0) * fps / 1e6) * 1e6 / fps))
                sr['duration'] = max(1, int(round(round(float(sr.get('duration') or 0) * fps / 1e6) * 1e6 / fps)))


def project_set_video(path, video):
    project = _project(path)
    if not isinstance(video, dict):
        raise RuntimeError('video must be an object with width, height, rateNum, rateDen')
    width = int(video.get('width') or 1920)
    height = int(video.get('height') or 1080)
    rate_num = int(video.get('rateNum') or 30)
    rate_den = int(video.get('rateDen') or 1)
    if width <= 0 or height <= 0 or rate_num <= 0 or rate_den <= 0:
        raise RuntimeError('width, height and rate must be positive')
    with project.edit('project_set_video'):
        content = project.content
        content['canvas_config'] = dict(content.get('canvas_config') or {},
                                        width=width, height=height,
                                        ratio=_ratio(width, height))
        fps_value = rate_num / float(rate_den)
        content['fps'] = int(round(fps_value)) if abs(fps_value - round(fps_value)) < 1e-6 else round(fps_value, 3)
        content['mcp_rate'] = {'rateNum': rate_num, 'rateDen': rate_den}
        if video.get('canvas'):
            content['mcp_canvas_color'] = str(video['canvas'])
            content['canvas_config']['background'] = None
            for track in content.get('tracks') or []:
                if _track_kind(track) != 'video':
                    continue
                for segment in track.get('segments') or []:
                    _set_segment_canvas(project, segment,
                                        {'kind': 'color', 'color': str(video['canvas'])})
        if video.get('colorSpace'):
            content['mcp_color_space'] = video['colorSpace']
        _resnap_all(project)
    return project.summary()


def timeline_read(path):
    return _project(path).timeline()


def timeline_linkage(path, timelineId, on):
    project = _project(path)
    if timelineId and timelineId != project.content.get('id'):
        return {'ok': True, 'changed': False, 'reason': 'unknown timeline id'}
    with project.edit('timeline_linkage'):
        _set_linkage(project.content, bool(on))
    return {**project.summary(), 'linkage': bool(on)}


def timeline_background(path, timelineId, background):
    project = _project(path)
    content = project.content
    if timelineId and timelineId != content.get('id'):
        return {'ok': True, 'changed': False, 'reason': 'unknown timeline id'}
    background = background or {'kind': 'none'}
    kind = str(background.get('kind') or 'none').lower()
    with project.edit('timeline_background'):
        content['mcp_background'] = deepcopy(background)
        if kind == 'none':
            content.setdefault('canvas_config', {})['background'] = None
        elif kind == 'color':
            content['mcp_canvas_color'] = background.get('color') or ''
            content.setdefault('canvas_config', {})['background'] = None
        for track in content.get('tracks') or []:
            if _track_kind(track) != 'video':
                continue
            for segment in track.get('segments') or []:
                _set_segment_canvas(project, segment, background)
    return {**project.summary(), 'background': deepcopy(background)}


def _set_segment_canvas(project, segment, background):
    content = project.content
    refs = segment.setdefault('extra_material_refs', [])
    old = None
    for ref in list(refs):
        mat = _find_material_by_id(content, ref)
        if isinstance(mat, dict) and str(mat.get('type', '')).startswith('canvas'):
            old = mat
            refs.remove(ref)
    if old:
        _remove_material(content, old['id'])
    kind = str((background or {}).get('kind') or 'none').lower()
    if kind == 'none':
        return
    if kind == 'color':
        mat = new_canvas_material('color', color=background.get('color') or '#000000')
    elif kind == 'blur':
        mat = new_canvas_material('blur', blur=float(background.get('amount') or 0.375))
    elif kind == 'image':
        media = _media_spec(project, background.get('mediaId'))
        mat = new_canvas_material('image', image=media['path'],
                                  image_id=media.get('id') or '')
    else:
        return
    _add_material(content, 'canvases', mat)
    refs.append(mat['id'])


def track_add(path, bottom=False, kind='video'):
    project = _project(path)
    if kind not in ('video', 'audio', 'text', 'effect', 'sticker', 'image'):
        raise RuntimeError('track kind must be video, audio, text, effect, sticker or image')
    with project.edit('track_add'):
        track = new_track(kind)
        if bottom:
            project.content.setdefault('tracks', []).insert(0, track)
        else:
            project.content.setdefault('tracks', []).append(track)
        _refresh_track_indices(project.content)
    return {**project.summary(), 'createdId': track['id'], 'track': track['id']}


# ---------------------------------------------------------------- media bin

def _register_media_entry(project, media):
    meta = project.meta
    groups = meta.get('draft_materials')
    if not isinstance(groups, list):
        groups = []
        meta['draft_materials'] = groups
    entry = {
        'ai_group_type': '', 'create_time': -1,
        'duration': usec(media.get('duration') or 0),
        'enter_from': 0,
        'extra_info': media.get('name') or os.path.basename(media['path']),
        'file_Path': cc.posix(media['path']),
        'height': int(media.get('height') or 0),
        'id': media['id'],
        'import_time': -1, 'import_time_ms': -1, 'item_source': 1,
        'material_color_tag': '', 'md5': '',
        'metetype': 'music' if media.get('kind') == 'audio' else (
            'photo' if media.get('kind') == 'image' else 'video'),
        'roughcut_time_range': {'duration': -1, 'start': -1},
        'sub_time_range': {'duration': -1, 'start': -1},
        'type': 0,
        'width': int(media.get('width') or 0),
    }
    group = next((g for g in groups if isinstance(g, dict) and g.get('type') == 0), None)
    if group is None:
        group = {'type': 0, 'value': []}
        groups.append(group)
    values = group.setdefault('value', [])
    values[:] = [v for v in values
                 if not (isinstance(v, dict) and
                         cc.posix(v.get('file_Path', '')).lower() == entry['file_Path'].lower())]
    values.append(entry)


def media_import(path, file):
    project = _project(path)
    probe = probe_media(file)
    wanted = cc.posix(probe['path']).lower()
    for item in project.bin.get('items') or []:
        if cc.posix(item.get('path', '')).lower() == wanted:
            return {'ok': True, 'createdId': item.get('id'), 'media': item,
                    'binCount': len(project.bin.get('items') or []),
                    'existing': True}
    duration = probe['duration']
    if probe['kind'] == 'image' and (not duration or duration > 60):
        duration = 5.0
    media = {
        'id': cc.uid(),
        'path': probe['path'],
        'kind': probe['kind'],
        'name': os.path.basename(probe['path']),
        'duration': duration,
        'width': probe['width'],
        'height': probe['height'],
        'has_audio': probe['has_audio'],
        'frameRate': probe.get('frame_rate'),
        'importedAt': int(time.time()),
    }
    with project.edit('media_import'):
        project.bin.setdefault('items', []).append(media)
        _register_media_entry(project, media)
        project._save_bin()
    return {'ok': True, 'createdId': media['id'], 'media': media,
            'binCount': len(project.bin.get('items') or [])}


def media_probe(path):
    return {'ok': True, 'media': probe_media(path)}


def _media_spec(project, ident):
    ident = str(ident or '').strip()
    if not ident:
        raise RuntimeError('media id is required')
    items = project.bin.get('items') or []
    for item in items:
        if item.get('id') == ident:
            return dict(item)
    for item in items:
        if ident and (item.get('path') == ident or item.get('name') == ident):
            return dict(item)
    if len(ident) >= 6:
        matches = [i for i in items if str(i.get('id') or '').startswith(ident)]
        if len(matches) == 1:
            return dict(matches[0])
    # allow a bin id to be a material id already in the draft
    mat = _find_material_by_id(project.content, ident)
    if mat:
        kind = 'audio' if str(mat.get('type', '')).startswith(('extract', 'music', 'record', 'sound')) else (
            'image' if mat.get('type') == 'photo' else 'video')
        return {'id': mat['id'], 'path': mat.get('path'), 'kind': kind,
                'name': mat.get('material_name') or mat.get('name') or '',
                'duration': float(mat.get('duration') or 0) / 1e6,
                'width': mat.get('width'), 'height': mat.get('height'),
                'has_audio': bool(mat.get('has_audio'))}
    if os.path.isfile(_expand(ident)):
        probe = probe_media(ident)
        return {'id': probe['path'], 'path': probe['path'], 'kind': probe['kind'],
                'name': os.path.basename(probe['path']),
                'duration': probe['duration'], 'width': probe['width'],
                'height': probe['height'], 'has_audio': probe['has_audio']}
    raise RuntimeError(f'media item not found in the bin: {ident!r}')


def _ensure_material(project, media):
    content = project.content
    path = cc.posix(media['path']).lower()
    kind = media.get('kind')
    if kind == 'audio':
        for mat in _material_list(content, 'audios'):
            if cc.posix(mat.get('path', '')).lower() == path:
                return mat
        mat = new_audio_material(media)
        _add_material(content, 'audios', mat)
        return mat
    for mat in _material_list(content, 'videos'):
        if cc.posix(mat.get('path', '')).lower() == path:
            return mat
    mat = new_video_material(media)
    _add_material(content, 'videos', mat)
    return mat


def _companions(content, kind, speed=1.0):
    speed_mat = new_speed_material(speed)
    _add_material(content, 'speeds', speed_mat)
    ph = new_placeholder_info()
    _add_material(content, 'placeholder_infos', ph)
    refs = [speed_mat['id'], ph['id']]
    if kind in ('video', 'image'):
        canvas = new_canvas_material('none')
        _add_material(content, 'canvases', canvas)
        snd = new_sound_channel_mapping()
        _add_material(content, 'sound_channel_mappings', snd)
        col = new_material_color()
        _add_material(content, 'material_colors', col)
        voc = new_vocal_separation()
        _add_material(content, 'vocal_separations', voc)
        refs += [canvas['id'], snd['id'], col['id'], voc['id']]
    elif kind == 'audio':
        beats = new_beats()
        _add_material(content, 'beats', beats)
        snd = new_sound_channel_mapping()
        _add_material(content, 'sound_channel_mappings', snd)
        voc = new_vocal_separation()
        _add_material(content, 'vocal_separations', voc)
        refs += [beats['id'], snd['id'], voc['id']]
    elif kind == 'text':
        anim = new_animation_material([], 'text')
        _add_material(content, 'material_animations', anim)
        refs = [anim['id']]
    elif kind == 'effect':
        refs = []
    return refs, speed_mat


def _clip_defaults():
    return {
        'scale': {'x': 1.0, 'y': 1.0}, 'rotation': 0.0, 'rotation3d': [],
        'transform': {'x': 0.0, 'y': 0.0},
        'flip': {'vertical': False, 'horizontal': False}, 'alpha': 1.0,
    }


def _make_segment(project, material, kind, start, source_start=0,
                  source_duration=0, target_duration=0, speed=1.0,
                  volume=1.0):
    content = project.content
    template = _schema_segment(kind)
    if template is None:
        template = {
            'id': '', 'target_timerange': {}, 'source_timerange': {},
            'render_timerange': {'start': 0, 'duration': 0}, 'desc': '',
            'state': 0, 'speed': 1.0, 'is_loop': False, 'is_tone_modify': False,
            'reverse': False, 'intensifies_audio': False, 'cartoon': False,
            'volume': 1.0, 'last_nonzero_volume': 1.0, 'clip': _clip_defaults(),
            'uniform_scale': {'on': True, 'value': 1.0}, 'material_id': '',
            'extra_material_refs': [], 'render_index': 0,
            'keyframe_refs': [], 'enable_lut': False, 'enable_adjust': False,
            'enable_hsl': False, 'visible': True, 'group_id': '',
            'enable_color_curves': True, 'enable_hsl_curves': True,
            'track_render_index': 0, 'hdr_settings': None,
            'hdr_vivid_settings': None, 'enable_color_wheels': True,
            'track_attribute': 0, 'is_placeholder': False, 'template_id': '',
            'enable_smart_color_adjust': False, 'template_scene': 'default',
            'common_keyframes': [], 'caption_info': None,
            'responsive_layout': {'enable': False, 'target_follow': '',
                                  'size_layout': 0, 'horizontal_pos_layout': 0,
                                  'vertical_pos_layout': 0},
            'enable_color_match_adjust': False,
            'enable_color_correct_adjust': False,
            'enable_adjust_mask': False, 'raw_segment_id': '',
            'lyric_keyframes': None, 'enable_video_mask': True,
            'digital_human_template_group_id': '', 'color_correct_alg_result': '',
            'source': 'segmentsourcenormal', 'enable_mask_stroke': False,
            'enable_mask_shadow': False, 'enable_color_adjust_pro': False,
            'segment_color_tag': '', 'lumi_occlusion': None,
        }
    seg = deepcopy(template)
    seg['id'] = cc.uid()
    seg['material_id'] = material['id']
    seg['target_timerange'] = {'start': int(start), 'duration': int(target_duration)}
    seg['render_timerange'] = {'start': 0, 'duration': 0}
    if kind in ('text', 'effect', 'sticker'):
        seg['source_timerange'] = None
    else:
        seg['source_timerange'] = {'start': int(source_start),
                                    'duration': int(source_duration)}
    seg['speed'] = float(speed)
    seg['volume'] = float(volume)
    seg['last_nonzero_volume'] = float(volume)
    seg['visible'] = True
    seg['is_loop'] = False
    seg['reverse'] = False
    seg['is_tone_modify'] = False
    seg['common_keyframes'] = []
    seg['keyframe_refs'] = []
    if kind in ('video', 'image', 'text', 'sticker'):
        clip = seg.get('clip')
        if not isinstance(clip, dict):
            clip = _clip_defaults()
        clip.setdefault('scale', {'x': 1.0, 'y': 1.0})
        clip.setdefault('transform', {'x': 0.0, 'y': 0.0})
        clip.setdefault('rotation', 0.0)
        clip.setdefault('flip', {'vertical': False, 'horizontal': False})
        clip.setdefault('alpha', 1.0)
        seg['clip'] = clip
        seg['uniform_scale'] = {'on': True, 'value': 1.0}
    else:
        seg['clip'] = None
        seg['uniform_scale'] = None
    refs, speed_mat = _companions(content, kind, speed)
    seg['extra_material_refs'] = refs
    return seg, speed_mat


def _track_for_kind(project, kind, start=None, end=None):
    content = project.content
    if kind in ('video', 'image'):
        needle = 'video'
    elif kind in ('audio', 'text'):
        needle = kind
    else:
        needle = kind
    for track in content.get('tracks') or []:
        if _track_kind(track) != needle:
            continue
        if start is None or end is None:
            return track
        if not _segments_overlapping(track, start, end):
            return track
    return None


def _choose_free_track(project, kind, start, end):
    track = _track_for_kind(project, kind, start, end)
    if track:
        return track
    track = new_track(kind)
    project.content.setdefault('tracks', []).append(track)
    return track


def _add_media_clip(project, media, track, start, ripple=False):
    kind = media.get('kind')
    if kind == 'image':
        duration = 5.0
    else:
        duration = float(media.get('duration') or 0)
    if duration <= 0:
        raise RuntimeError(f'media has no duration: {media.get("path")}')
    start = max(0, snap_project(project.content, usec(start)))
    target_duration = max(1, snap_project(project.content, usec(duration)))
    material = _ensure_material(project, media)
    source_duration = target_duration if kind == 'image' else max(
        1, snap_project(project.content, usec(duration)))
    if source_duration > int(material.get('duration') or source_duration):
        source_duration = max(1, snap_project(
            project.content, int(material.get('duration') or source_duration)))
    target_duration = source_duration
    if ripple:
        for other in _segments_overlapping(track, start, start + target_duration):
            _shift_segments(track, _segment_start(other), target_duration,
                            ignore=(other.get('id'),))
        _shift_linked(project, track, start, target_duration)
    if _segments_overlapping(track, start, start + target_duration):
        raise RuntimeError('the drop overlaps a clip on that track; '
                           'use ripple=true or pick another start')
    seg, _speed = _make_segment(project, material, kind, start, 0,
                                source_duration, target_duration)
    track.setdefault('segments', []).append(seg)
    track['segments'] = sorted(track['segments'], key=lambda s: _segment_start(s))
    _refresh_track_indices(project.content)
    return seg


def clip_add(path, mediaId, trackId, start, ripple=False):
    project = _project(path)
    media = _media_spec(project, mediaId)
    with project.edit('clip_add'):
        track = _track_by_id(project.content, trackId) if trackId else None
        kind = media.get('kind')
        if track is None:
            track = _choose_free_track(project, kind,
                                       snap_project(project.content, usec(start)),
                                       snap_project(project.content, usec(start)) + 1)
        track_kind = _track_kind(track)
        if kind in ('video', 'image') and track_kind != 'video':
            raise RuntimeError(f'{kind} clips go on a video track, not {track_kind}')
        if kind == 'audio' and track_kind != 'audio':
            raise RuntimeError('audio clips go on an audio track')
        seg = _add_media_clip(project, media, track, start, ripple=bool(ripple))
    return {**project.summary(), 'createdId': seg['id'],
            'clip': _segment_summary(project.content, track, seg)}


def clips_add_sequence(path, mediaIds, start, trackId=None, ripple=False):
    project = _project(path)
    specs = [_media_spec(project, mid) for mid in mediaIds]
    with project.edit('clips_add_sequence'):
        cursor = snap_project(project.content, usec(start))
        run_kind = 'audio' if specs and all(s.get('kind') == 'audio' for s in specs) else 'video'
        if trackId:
            track = _track_by_id(project.content, trackId)
        else:
            total = sum(usec(s.get('duration') or 5.0) for s in specs)
            track = _track_for_kind(project, run_kind, cursor, cursor + total)
        if track is None:
            track = new_track(run_kind)
            project.content.setdefault('tracks', []).append(track)
        kind = _track_kind(track)
        created = []
        for spec in specs:
            spec_kind = spec.get('kind')
            if spec_kind == 'audio' and kind != 'audio':
                raise RuntimeError('audio media needs an audio track')
            if spec_kind in ('video', 'image') and kind != 'video':
                raise RuntimeError(f'{spec_kind} media needs a video track')
            seg = _add_media_clip(project, spec, track, sec(cursor),
                                  ripple=bool(ripple))
            cursor = _segment_end(seg)
            created.append(seg['id'])
        return {**project.summary(), 'createdId': created[-1] if created else None,
                'createdIds': created, 'trackId': track['id'],
                'count': len(created)}


def _segment_summary(content, track, segment):
    mat = _segment_material(content, segment) or {}
    src = segment.get('source_timerange') or {}
    return {
        'id': segment.get('id'),
        'trackId': track.get('id'),
        'kind': _track_kind(track),
        'start': sec(_segment_start(segment)),
        'duration': sec(_segment_duration(segment)),
        'end': sec(_segment_end(segment)),
        'sourceStart': sec(src.get('start')),
        'sourceDuration': sec(src.get('duration')),
        'speed': float(segment.get('speed') or 1.0),
        'volume': float(segment.get('volume') if segment.get('volume') is not None else 1.0),
        'materialId': segment.get('material_id'),
        'path': _material_path(mat),
        'name': mat.get('material_name') or mat.get('name'),
    }


# ---------------------------------------------------------------- move / remove / split

def _paired_segments(content, segment, track):
    """Audio twins of a video segment (same range and source path)."""
    mat = _segment_material(content, segment) or {}
    path = cc.posix(_material_path(mat)).lower()
    out = []
    for other_track in content.get('tracks') or []:
        if other_track is track:
            continue
        for other in other_track.get('segments') or []:
            omat = _segment_material(content, other) or {}
            if omat.get('id') == mat.get('id'):
                continue
            if cc.posix(_material_path(omat)).lower() != path:
                continue
            if abs(_segment_start(other) - _segment_start(segment)) <= frame_us(content) and \
                    abs(_segment_duration(other) - _segment_duration(segment)) <= frame_us(content) and \
                    abs(_source_start(other) - _source_start(segment)) <= frame_us(content):
                out.append(other)
    return out


def _check_track_overlap(track, start, end, ignore=()):
    overlap = _segments_overlapping(track, start, end, ignore=ignore)
    if overlap:
        raise RuntimeError(
            f'clip would overlap {len(overlap)} clip(s) on track {track.get("id")}; '
            'move or remove them first, or use ripple=true where available')


def clips_move(path, moves):
    project = _project(path)
    with project.edit('clips_move'):
        content = project.content
        moved = []
        for move in moves or []:
            clip_id = move.get('clipId') or move.get('clip_id')
            try:
                track, segment = _find_segment(content, clip_id)
            except RuntimeError:
                continue
            start = max(0, snap_project(content, usec(move.get('start') or 0)))
            target = track
            if move.get('trackId'):
                try:
                    target = _track_by_id(content, move['trackId'])
                except RuntimeError:
                    target = track
            source_kind = _track_kind(track)
            target_kind = _track_kind(target)
            if source_kind == 'video':
                if target_kind != 'video':
                    raise RuntimeError('video clips can only live on video tracks')
            elif target_kind != source_kind:
                raise RuntimeError(f'{source_kind} clips can only live on {source_kind} tracks')
            _check_track_overlap(target, start, start + _segment_duration(segment),
                                 ignore=(segment.get('id'),))
            delta = start - _segment_start(segment)
            segment.setdefault('target_timerange', {})['start'] = start
            if target is not track:
                track['segments'].remove(segment)
                target.setdefault('segments', []).append(segment)
            for pair in _paired_segments(content, segment, track):
                pair.setdefault('target_timerange', {})['start'] = max(
                    0, _segment_start(pair) + delta)
            moved.append(segment['id'])
        _refresh_track_indices(content)
        _sort_tracks(content)
    return {**project.summary(), 'moved': moved, 'movedCount': len(moved)}


def clips_remove(path, clipIds, ripple=False):
    project = _project(path)
    with project.edit('clips_remove'):
        content = project.content
        ids = set()
        for ident in clipIds or []:
            try:
                _track, segment = _find_segment(content, ident)
            except RuntimeError:
                continue
            ids.add(segment['id'])
            for pair in _paired_segments(content, segment, _track):
                ids.add(pair['id'])
        removed = []
        per_track = {}
        for track in content.get('tracks') or []:
            keep = []
            gone = []
            for segment in track.get('segments') or []:
                if segment.get('id') in ids:
                    gone.append(segment)
                else:
                    keep.append(segment)
            if gone:
                removed.extend(s['id'] for s in gone)
                per_track[track['id']] = gone
            track['segments'] = keep
        if ripple:
            for track in content.get('tracks') or []:
                gone = per_track.get(track['id']) or []
                if not gone:
                    continue
                spans = sorted((_segment_start(s), _segment_duration(s)) for s in gone)
                for segment in track.get('segments') or []:
                    shift = sum(duration for start, duration in spans
                                if start < _segment_start(segment))
                    if shift:
                        segment['target_timerange']['start'] = max(
                            0, _segment_start(segment) - shift)
            # the main lane carries the other lanes with it while linkage is on
            if _linkage_on(content):
                main = _main_video_track(content)
                gone = per_track.get(main['id']) if main else None
                if gone:
                    spans = sorted((_segment_start(s), _segment_duration(s)) for s in gone)
                    for track in content.get('tracks') or []:
                        if track is main or track.get('id') in per_track:
                            continue
                        for segment in track.get('segments') or []:
                            shift = sum(duration for start, duration in spans
                                        if start < _segment_start(segment))
                            if shift:
                                segment['target_timerange']['start'] = max(
                                    0, _segment_start(segment) - shift)
        _prune_materials(content)
        _recompute_duration(content)
    return {**project.summary(), 'removed': removed, 'removedCount': len(removed)}


def _material_array_key(content, material_id):
    for key, arr in _materials(content).items():
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict) and item.get('id') == material_id:
                    return key
    return None


def _clone_companions(content, source, target):
    """Give a split half its own companion materials (except transitions)."""
    cloned = []
    transitions = []
    for ref in source.get('extra_material_refs') or []:
        mat = _find_material_by_id(content, ref)
        if not isinstance(mat, dict):
            continue
        if mat.get('type') == 'transition':
            transitions.append(ref)
            continue
        key = _material_array_key(content, ref)
        if not key:
            continue
        clone = deepcopy(mat)
        clone['id'] = cc.uid()
        _add_material(content, key, clone)
        cloned.append(clone['id'])
    target['extra_material_refs'] = cloned
    # An outgoing transition belongs to the first clip of the pair, which is
    # now the right half.
    if transitions:
        source_refs = source.setdefault('extra_material_refs', [])
        for ref in transitions:
            if ref in source_refs:
                source_refs.remove(ref)
        target['extra_material_refs'].extend(transitions)
    return cloned


def _split_keyframes(segment, cut):
    old = segment.get('common_keyframes') or []
    left, right = [], []
    for item in old:
        keys = item.get('keyframe_list') or []
        a = [k for k in keys if int(k.get('time_offset') or 0) < cut]
        b = [k for k in keys if int(k.get('time_offset') or 0) >= cut]
        if a:
            left.append({**deepcopy(item), 'keyframe_list': deepcopy(a)})
        if b:
            for k in b:
                k['time_offset'] = int(k.get('time_offset') or 0) - cut
            right.append({**deepcopy(item), 'keyframe_list': deepcopy(b)})
    segment['common_keyframes'] = left
    return right


def _split_segment(project, track, segment, at):
    content = project.content
    at = snap_project(content, usec(at))
    start = _segment_start(segment)
    end = _segment_end(segment)
    if not (start < at < end):
        return None
    cut = at - start
    left_duration = cut
    right_duration = end - at
    if left_duration < frame_us(content) or right_duration < frame_us(content):
        return None
    right = deepcopy(segment)
    right['id'] = cc.uid()
    right['target_timerange'] = {'start': at, 'duration': right_duration}
    if segment.get('source_timerange'):
        speed = float(segment.get('speed') or 1.0)
        source_cut = int(round(cut * speed))
        source_start = _source_start(segment)
        source_duration = _source_duration(segment)
        source_cut = max(0, min(source_duration, source_cut))
        segment['source_timerange'] = {'start': source_start,
                                       'duration': source_cut}
        right['source_timerange'] = {'start': source_start + source_cut,
                                     'duration': max(1, source_duration - source_cut)}
    segment['target_timerange'] = {'start': start, 'duration': left_duration}
    right['common_keyframes'] = _split_keyframes(segment, cut)
    right['keyframe_refs'] = []
    if _track_kind(track) == 'text':
        material = _segment_material(content, segment)
        if material:
            clone = deepcopy(material)
            clone['id'] = cc.uid()
            _add_material(content, 'texts', clone)
            right['material_id'] = clone['id']
    _clone_companions(content, segment, right)
    track.setdefault('segments', []).append(right)
    track['segments'] = sorted(track['segments'], key=lambda s: _segment_start(s))
    return right


def clips_split(path, clipIds, time):
    project = _project(path)
    with project.edit('clips_split'):
        content = project.content
        created = []
        handled = set()
        for ident in clipIds or []:
            try:
                track, segment = _find_segment(content, ident)
            except RuntimeError:
                continue
            if segment['id'] in handled:
                continue
            pairs = _paired_segments(content, segment, track)
            right = _split_segment(project, track, segment, time)
            handled.add(segment['id'])
            if right:
                created.append(right['id'])
            for pair in pairs:
                pair_track = _segment_track(content, pair['id'])
                if not pair_track or pair['id'] in handled:
                    continue
                pair_right = _split_segment(project, pair_track, pair, time)
                handled.add(pair['id'])
                if pair_right:
                    created.append(pair_right['id'])
        _refresh_track_indices(content)
    return {**project.summary(), 'createdId': created[-1] if created else None,
            'createdIds': created, 'splitCount': len(created)}


# ---------------------------------------------------------------- trim / speed / frame / transform

def _trim_keyframes(segment, edge, delta_us, new_duration):
    for entry in list(segment.get('common_keyframes') or []):
        keys = entry.get('keyframe_list') or []
        if edge == 'start':
            for key in keys:
                key['time_offset'] = int(key.get('time_offset') or 0) - int(delta_us)
            keys = [k for k in keys if int(k.get('time_offset') or 0) >= 0]
        else:
            keys = [k for k in keys if int(k.get('time_offset') or 0) <= int(new_duration)]
        entry['keyframe_list'] = sorted(keys, key=lambda k: int(k.get('time_offset') or 0))
        if not entry['keyframe_list']:
            segment['common_keyframes'].remove(entry)


def _scale_keyframes(segment, factor):
    if factor <= 0:
        return
    for entry in segment.get('common_keyframes') or []:
        for key in entry.get('keyframe_list') or []:
            key['time_offset'] = int(round(int(key.get('time_offset') or 0) * factor))
        entry['keyframe_list'] = sorted(entry.get('keyframe_list') or [],
                                        key=lambda k: int(k.get('time_offset') or 0))


def _material_duration(content, segment):
    mat = _segment_material(content, segment) or {}
    try:
        return int(mat.get('duration') or 0)
    except (TypeError, ValueError):
        return 0


def _trim_segment(project, track, segment, edge, delta, ripple=False):
    content = project.content
    fps = project_fps(content)
    frame = 1_000_000.0 / fps
    delta_us = snap_project(content, usec(delta))
    if edge not in ('start', 'end'):
        raise RuntimeError("edge must be 'start' or 'end'")
    start = _segment_start(segment)
    duration = _segment_duration(segment)
    source_start = _source_start(segment)
    speed = float(segment.get('speed') or 1.0)
    source_has = bool(segment.get('source_timerange'))

    if edge == 'start':
        new_duration = duration - delta_us
        new_start = start if ripple else start + delta_us
        new_source_start = source_start + int(round(delta_us * speed)) if source_has else source_start
    else:
        new_duration = duration + delta_us
        new_start = start
        new_source_start = source_start

    if new_duration < frame:
        raise RuntimeError('a clip cannot be shorter than one frame')
    if new_start < 0:
        raise RuntimeError('the clip cannot start before 0')
    new_source_duration = int(round(new_duration * speed)) if source_has else 0
    if source_has:
        mat_duration = _material_duration(content, segment)
        if new_source_start < 0:
            raise RuntimeError('trim runs before the start of the media')
        if mat_duration and new_source_start + new_source_duration > mat_duration + int(frame):
            over = new_source_start + new_source_duration - mat_duration
            new_duration = max(int(frame), new_duration - over)
            new_source_duration = int(round(new_duration * speed))
        segment['source_timerange'] = {'start': new_source_start,
                                       'duration': new_source_duration}
    old_end = _segment_end(segment)
    segment['target_timerange'] = {'start': new_start,
                                   'duration': new_duration}
    _trim_keyframes(segment, edge, delta_us, new_duration)
    if not ripple and edge == 'end':
        _check_track_overlap(track, new_start, new_start + new_duration,
                             ignore=(segment.get('id'),))
    if not ripple and edge == 'start':
        _check_track_overlap(track, new_start, new_start + new_duration,
                             ignore=(segment.get('id'),))
    shift = 0
    if ripple:
        if edge == 'end':
            shift = new_duration - duration
            _shift_segments(track, old_end, shift, ignore=(segment.get('id'),))
        else:
            shift = new_duration - duration
            _shift_segments(track, old_end, shift, ignore=(segment.get('id'),))
    return {'edge': edge, 'delta': sec(delta_us), 'start': sec(new_start),
            'duration': sec(new_duration), 'shift': sec(shift)}


def clip_trim(path, clipId, edge, delta, ripple=False):
    project = _project(path)
    with project.edit('clip_trim'):
        content = project.content
        track, segment = _find_segment(content, clipId)
        pairs = _paired_segments(content, segment, track)
        pair_tracks = {t.get('id') for t in
                       (_segment_track(content, pair['id']) for pair in pairs) if t}
        old_end = _segment_end(segment)
        result = _trim_segment(project, track, segment, edge, delta, bool(ripple))
        for pair in pairs:
            pair_track = _segment_track(content, pair['id'])
            if not pair_track:
                continue
            try:
                _trim_segment(project, pair_track, pair, edge, delta, bool(ripple))
            except RuntimeError:
                pass
        _shift_linked(project, track, old_end, _segment_end(segment) - old_end,
                      exclude=pair_tracks)
        _refresh_track_indices(content)
    return {**project.summary(), 'clipId': segment['id'], **result}


def clip_speed(path, clipId, speed, ripple=False):
    project = _project(path)
    speed = clamp(float(speed), 0.0625, 16.0)
    with project.edit('clip_speed'):
        content = project.content
        track, segment = _find_segment(content, clipId)
        pairs = _paired_segments(content, segment, track)
        old_end = _segment_end(segment)
        old_duration = _segment_duration(segment)
        source_duration = _source_duration(segment)
        if source_duration:
            new_duration = max(1, int(round(source_duration / speed)))
            new_duration = snap_project(content, new_duration)
            if new_duration < frame_us(content):
                new_duration = int(round(frame_us(content)))
            delta = new_duration - old_duration
            if ripple:
                _shift_segments(track, _segment_end(segment), delta,
                                ignore=(segment.get('id'),))
            else:
                overlaps = _segments_overlapping(
                    track, _segment_start(segment),
                    _segment_start(segment) + new_duration,
                    ignore=(segment.get('id'),))
                if overlaps:
                    if delta <= 0:
                        raise RuntimeError('speed change would overlap a neighbour')
                    next_start = min(_segment_start(s) for s in overlaps)
                    needed = (_segment_start(segment) + new_duration) - next_start
                    _shift_segments(track, next_start, needed,
                                    ignore=(segment.get('id'),))
            segment['target_timerange']['duration'] = new_duration
            _scale_keyframes(segment, new_duration / float(max(1, old_duration)))
        segment['speed'] = speed
        for ref in segment.get('extra_material_refs') or []:
            mat = _find_material_by_id(content, ref)
            if isinstance(mat, dict) and mat.get('type') == 'speed':
                mat['speed'] = speed
                mat['curve_speed'] = None
        for pair in pairs:
            pair['speed'] = speed
            if _source_duration(pair):
                old_pair_duration = _segment_duration(pair)
                pair['target_timerange']['duration'] = segment['target_timerange']['duration']
                _scale_keyframes(pair, _segment_duration(pair) / float(max(1, old_pair_duration)))
        _shift_linked(project, track, old_end, _segment_end(segment) - old_end)
        _refresh_track_indices(content)
    return {**project.summary(), 'clipId': segment['id'],
            'speed': speed,
            'duration': sec(_segment_duration(segment))}


def _material_dimensions(project, material):
    width = int(material.get('width') or 0)
    height = int(material.get('height') or 0)
    if not width or not height:
        path = material.get('path')
        if path and os.path.isfile(path):
            probe = probe_media(path)
            width = int(probe.get('width') or 0)
            height = int(probe.get('height') or 0)
            material['width'] = width
            material['height'] = height
    return width, height


def clip_frame(path, clipId, framing, at=0.0):
    project = _project(path)
    framing = str(framing or '').lower()
    if framing not in ('fit', 'fill'):
        raise RuntimeError("framing must be 'fit' or 'fill'")
    with project.edit('clip_frame'):
        content = project.content
        track, segment = _find_segment(content, clipId)
        if _track_kind(track) in ('audio', 'effect'):
            raise RuntimeError(f'{_track_kind(track)} clips have no picture to frame')
        material = _segment_material(content, segment)
        if not material:
            raise RuntimeError('clip has no visual material')
        width, height = _material_dimensions(project, material)
        if not width or not height:
            raise RuntimeError('cannot read the media dimensions')
        canvas = project.canvas
        sx = canvas['width'] / float(width)
        sy = canvas['height'] / float(height)
        scale = min(sx, sy) if framing == 'fit' else max(sx, sy)
        clip = segment.setdefault('clip', _clip_defaults())
        clip['scale'] = {'x': scale, 'y': scale}
        clip['transform'] = {'x': 0.0, 'y': 0.0}
        segment['uniform_scale'] = {'on': True, 'value': scale}
        if any(k.get('property_type') in ('UNIFORM_SCALE', 'KFTypeScaleX', 'KFTypeScaleY')
               for k in segment.get('common_keyframes') or []):
            _keyframe_set(segment, 'scale', clamp(float(at), 0, 1), scale, None)
    return {**project.summary(), 'clipId': segment['id'],
            'framing': framing, 'scale': scale}


def clip_transform(path, clipId, offsetX=None, offsetY=None, rotation=None,
                   scale=None, stretchX=None, stretchY=None):
    project = _project(path)
    with project.edit('clip_transform'):
        track, segment = _find_segment(project.content, clipId)
        if _track_kind(track) in ('audio', 'effect'):
            raise RuntimeError(f'{_track_kind(track)} clips have no picture to transform')
        clip = segment.setdefault('clip', _clip_defaults())
        clip.setdefault('scale', {'x': 1.0, 'y': 1.0})
        clip.setdefault('transform', {'x': 0.0, 'y': 0.0})
        if offsetX is not None or offsetY is not None:
            tx = clip['transform'].get('x', 0.0) if offsetX is None else clamp(float(offsetX), -3, 3) * 2.0
            ty = clip['transform'].get('y', 0.0) if offsetY is None else -clamp(float(offsetY), -3, 3) * 2.0
            clip['transform'] = {'x': tx, 'y': ty}
        if rotation is not None:
            value = float(rotation)
            while value <= -180:
                value += 360
            while value > 180:
                value -= 360
            clip['rotation'] = value
        if scale is not None:
            value = clamp(float(scale), 0.05, 8.0)
            clip['scale'] = {'x': value, 'y': value}
            segment['uniform_scale'] = {'on': True, 'value': value}
        if stretchX is not None or stretchY is not None:
            sx = clip['scale'].get('x', 1.0) if stretchX is None else \
                clip['scale'].get('x', 1.0) * clamp(float(stretchX), 0.1, 10.0)
            sy = clip['scale'].get('y', 1.0) if stretchY is None else \
                clip['scale'].get('y', 1.0) * clamp(float(stretchY), 0.1, 10.0)
            clip['scale'] = {'x': sx, 'y': sy}
            segment['uniform_scale'] = {'on': False, 'value': 1.0}
    return {**project.summary(), 'clipId': segment['id'],
            'transform': deepcopy(segment.get('clip'))}


def _clone_material_for_segment(project, segment):
    content = project.content
    material = _segment_material(content, segment)
    if not material:
        return None
    material_id = material.get('id')
    shared = []
    for track in content.get('tracks') or []:
        for other in track.get('segments') or []:
            if other.get('material_id') == material_id:
                shared.append(other)
    if len(shared) <= 1:
        return material
    for key, arr in _materials(content).items():
        if isinstance(arr, list) and any(m is material for m in arr):
            clone = deepcopy(material)
            clone['id'] = cc.uid()
            arr.append(clone)
            segment['material_id'] = clone['id']
            return clone
    return material


def _get_fade(project, segment):
    content = project.content
    for ref in segment.get('extra_material_refs') or []:
        mat = _find_material_by_id(content, ref)
        if isinstance(mat, dict) and mat.get('type') == 'audio_fade':
            return sec(mat.get('fade_in_duration')), sec(mat.get('fade_out_duration'))
    return 0.0, 0.0


def _set_fade(project, segment, fade_in, fade_out):
    content = project.content
    refs = segment.setdefault('extra_material_refs', [])
    for ref in list(refs):
        mat = _find_material_by_id(content, ref)
        if isinstance(mat, dict) and mat.get('type') == 'audio_fade':
            refs.remove(ref)
            _remove_material(content, ref)
            break
    fade_in = max(0.0, float(fade_in or 0))
    fade_out = max(0.0, float(fade_out or 0))
    if not fade_in and not fade_out:
        return
    mat = new_audio_fade(fade_in, fade_out)
    _add_material(content, 'audio_fades', mat)
    refs.append(mat['id'])


def _set_crop(project, segment, crop):
    material = _clone_material_for_segment(project, segment)
    if not material:
        raise RuntimeError('clip has no material to crop')
    if crop is None:
        material['crop'] = {'upper_left_x': 0.0, 'upper_left_y': 0.0,
                            'upper_right_x': 1.0, 'upper_right_y': 0.0,
                            'lower_left_x': 0.0, 'lower_left_y': 1.0,
                            'lower_right_x': 1.0, 'lower_right_y': 1.0}
        material['crop_scale'] = 1.0
        return
    left = clamp(float(crop.get('left') or 0), 0, 1)
    right = clamp(float(crop.get('right') or 0), 0, 1)
    top = clamp(float(crop.get('top') or 0), 0, 1)
    bottom = clamp(float(crop.get('bottom') or 0), 0, 1)
    if left + right >= 1 or top + bottom >= 1:
        raise RuntimeError('crop leaves no picture')
    material['crop'] = {
        'upper_left_x': left, 'upper_left_y': top,
        'upper_right_x': 1.0 - right, 'upper_right_y': top,
        'lower_left_x': left, 'lower_left_y': 1.0 - bottom,
        'lower_right_x': 1.0 - right, 'lower_right_y': 1.0 - bottom,
    }
    material['crop_ratio'] = 'free'
    material['crop_scale'] = 1.0


def _set_masks(project, segment, masks):
    content = project.content
    refs = segment.setdefault('extra_material_refs', [])
    for ref in list(refs):
        mat = _find_material_by_id(content, ref)
        if isinstance(mat, dict) and (mat.get('type') == 'mask' or
                                      'mask' in str(mat.get('type', ''))):
            refs.remove(ref)
            _remove_material(content, ref)
    segment['mcp_masks'] = []
    segment['mcp_extra_masks'] = []
    created = []
    for index, spec in enumerate(masks or []):
        if index > 0:
            # CapCut has one mask per clip. Keep the rest as editor metadata
            # instead of attaching materials CapCut would ignore or misread.
            segment['mcp_extra_masks'].append(deepcopy(spec))
            continue
        mat = new_mask_material(spec)
        _add_material(content, 'common_mask', mat)
        refs.append(mat['id'])
        created.append(mat['id'])
        segment['mcp_masks'].append({'id': mat['id'], **deepcopy(spec)})
    segment['enable_video_mask'] = bool(created)
    return created


def _effect_refs(content, segment):
    out = []
    for ref in segment.get('extra_material_refs') or []:
        mat = _find_material_by_id(content, ref)
        if isinstance(mat, dict) and mat.get('type') in (
                'video_effect', 'face_effect', 'filter', 'text_effect', 'text_shape'):
            out.append((ref, mat))
    return out


def _replace_effect_chain(project, segment, chain, filter_chain=False):
    content = project.content
    refs = segment.setdefault('extra_material_refs', [])
    for ref, mat in _effect_refs(content, segment):
        if (filter_chain and mat.get('type') != 'filter') or \
                (not filter_chain and mat.get('type') == 'filter'):
            continue
        refs.remove(ref)
        _remove_material(content, ref)
    created = []
    for entry in chain or []:
        ident = entry.get('id') or entry.get('effectId') or entry.get('effect_id')
        if not ident:
            continue
        mat = (new_filter_material if filter_chain else new_effect_material)(
            ident, entry.get('params'), 'segment', segment.get('id'))
        if entry.get('enabled') is False:
            mat['mcp_enabled'] = False
        _add_material(content, 'video_effects', mat)
        refs.append(mat['id'])
        created.append(mat['id'])
    return created


def _transition_refs(content, segment):
    out = []
    for ref in segment.get('extra_material_refs') or []:
        mat = _find_material_by_id(content, ref)
        if isinstance(mat, dict) and mat.get('type') == 'transition':
            out.append((ref, mat))
    return out


def _set_transition(project, segment, track, transition):
    content = project.content
    segments = track.get('segments') or []
    owner = segment
    index = next((i for i, s in enumerate(segments) if s.get('id') == segment.get('id')), -1)
    if index > 0:
        owner = segments[index - 1]
    owner_refs = owner.setdefault('extra_material_refs', [])
    for ref, _mat in _transition_refs(content, owner):
        owner_refs.remove(ref)
        _remove_material(content, ref)
    if transition is None:
        return None
    ident = transition.get('id')
    if not ident:
        raise RuntimeError('transitionIn needs an id')
    mat = new_transition_material(ident, transition.get('duration'))
    _add_material(content, 'transitions', mat)
    # CapCut attaches a transition to the first clip of the cut.
    owner_refs.append(mat['id'])
    return mat['id']


def _set_motion(project, segment, track_kind, motion):
    content = project.content
    refs = segment.setdefault('extra_material_refs', [])
    for ref in list(refs):
        mat = _find_material_by_id(content, ref)
        if isinstance(mat, dict) and mat.get('type') == 'sticker_animation':
            refs.remove(ref)
            _remove_material(content, ref)
            break
    if not motion:
        return None
    animations = []
    for slot, key in (('intro', 'in'), ('outro', 'out'), ('loop', 'loop')):
        spec = motion.get(key)
        if not spec:
            continue
        kind = spec.get('kind') or spec.get('id') or spec.get('slug')
        if not kind:
            continue
        package = cat.find_animation(kind, slot, track_kind)
        if not package:
            raise RuntimeError(f'animation not found: {kind!r}')
        animations.append((slot, package, spec.get('seconds')))
    if not animations:
        return None
    mat = new_animation_material(animations, track_kind, _segment_duration(segment))
    _add_material(content, 'material_animations', mat)
    refs.append(mat['id'])
    return mat['id']


def _audio_effect_refs(content, segment):
    out = []
    for ref in segment.get('extra_material_refs') or []:
        mat = _find_material_by_id(content, ref)
        if isinstance(mat, dict) and mat.get('type') == 'audio_effect':
            out.append((ref, mat))
    return out


def _replace_audio_effects(project, segment, chain):
    content = project.content
    refs = segment.setdefault('extra_material_refs', [])
    for ref, _mat in _audio_effect_refs(content, segment):
        refs.remove(ref)
        _remove_material(content, ref)
    created = []
    for entry in chain or []:
        ident = entry.get('id') or entry.get('effectId')
        if not ident:
            continue
        found = cat.find_effect(ident)
        mat = _base_material('audio_effects', {
            'type': 'audio_effect', 'name': str(ident), 'effect_id': str(ident),
            'resource_id': str(ident), 'value': 1.0, 'adjust_params': [],
            'apply_target_type': 0, 'platform': 'all', 'source_platform': 1,
            'category_id': '', 'category_name': '', 'time_range': None,
        })
        mat['id'] = cc.uid()
        mat['type'] = 'audio_effect'
        if found:
            mat['name'] = found.get('name') or str(ident)
            mat['effect_id'] = found.get('effect_id') or str(ident)
            mat['resource_id'] = found.get('resource_id') or mat['effect_id']
        params = entry.get('params') or {}
        mat['adjust_params'] = [{'name': k, 'value': float(v), 'default_value': float(v)}
                                for k, v in params.items()
                                if isinstance(v, (int, float))]
        mat['mcp_params'] = params
        _add_material(content, 'audio_effects', mat)
        refs.append(mat['id'])
        created.append(mat['id'])
    return created


def clip_update(path, clipId, patch):
    project = _project(path)
    patch = patch or {}
    with project.edit('clip_update'):
        content = project.content
        track, segment = _find_segment(content, clipId)
        kind = _track_kind(track)
        material = _segment_material(content, segment)
        if kind in ('audio', 'effect'):
            visual_only = [key for key in ('opacity', 'flipH', 'flipV', 'crop', 'masks',
                                           'videoEffects', 'filters', 'transitionIn',
                                           'motion', 'text', 'background', 'blend',
                                           'shape')
                           if key in patch]
            if visual_only:
                raise RuntimeError(f'{kind} clips cannot take picture fields: ' +
                                   ', '.join(visual_only))
        if 'volume' in patch and patch.get('volume') is not None:
            value = max(0.0, float(patch['volume']))
            segment['volume'] = value
            if value > 0:
                segment['last_nonzero_volume'] = value
        if patch.get('muted') is not None:
            if patch.get('muted'):
                segment['last_nonzero_volume'] = float(segment.get('volume') or 1.0)
                segment['volume'] = 0.0
            else:
                segment['volume'] = float(segment.get('last_nonzero_volume') or 1.0)
        if 'fadeIn' in patch or 'fadeOut' in patch:
            fade_in, fade_out = _get_fade(project, segment)
            if 'fadeIn' in patch:
                fade_in = 0.0 if patch.get('fadeIn') is None else float(patch['fadeIn'])
            if 'fadeOut' in patch:
                fade_out = 0.0 if patch.get('fadeOut') is None else float(patch['fadeOut'])
            _set_fade(project, segment, max(0.0, fade_in), max(0.0, fade_out))
        if 'opacity' in patch and patch.get('opacity') is not None:
            value = clamp(float(patch['opacity']), 0, 1)
            clip = segment.setdefault('clip', _clip_defaults())
            clip['alpha'] = value
        if patch.get('flipH') is not None:
            clip = segment.setdefault('clip', _clip_defaults())
            clip.setdefault('flip', {})['horizontal'] = bool(patch['flipH'])
        if patch.get('flipV') is not None:
            clip = segment.setdefault('clip', _clip_defaults())
            clip.setdefault('flip', {})['vertical'] = bool(patch['flipV'])
        if patch.get('crop') is not None or 'crop' in patch:
            _set_crop(project, segment, patch.get('crop'))
        if patch.get('background') is not None or 'background' in patch:
            if patch.get('background') is None:
                _set_segment_canvas(project, segment, {'kind': 'none'})
            else:
                _set_segment_canvas(project, segment, patch['background'])
        if patch.get('masks') is not None or 'masks' in patch:
            _set_masks(project, segment, patch.get('masks') or [])
        if patch.get('videoEffects') is not None or 'videoEffects' in patch:
            _replace_effect_chain(project, segment, patch.get('videoEffects') or [], False)
        if patch.get('filters') is not None or 'filters' in patch:
            _replace_effect_chain(project, segment, patch.get('filters') or [], True)
        if patch.get('audioEffects') is not None or 'audioEffects' in patch:
            _replace_audio_effects(project, segment, patch.get('audioEffects') or [])
        if patch.get('transitionIn') is not None or 'transitionIn' in patch:
            _set_transition(project, segment, track, patch.get('transitionIn'))
        if patch.get('motion') is not None or 'motion' in patch:
            _set_motion(project, segment, kind, patch.get('motion'))
        if 'text' in patch and material and kind == 'text':
            if patch.get('text') is None:
                content_text = (_text_content(material).get('text') or '')
                material['mcp_style'] = _default_text_style(content_text)
                material['mcp_runs'] = []
                _build_text_styles(material)
            else:
                base = _style_from_material(material)
                merged = _normalize_text_style(patch['text'], base)
                material['mcp_style'] = merged
                material['mcp_runs'] = list(merged.get('runs') or [])
                _build_text_styles(material)
        if patch.get('audioStream') is not None or 'audioStream' in patch:
            segment['mcp_audio_stream'] = patch.get('audioStream')
        if patch.get('blend') is not None:
            _set_blend(project, segment, patch.get('blend'))
        if patch.get('shape') is not None:
            segment['mcp_shape'] = deepcopy(patch['shape'])
        if patch.get('preservePitch') is not None:
            segment['is_tone_modify'] = not bool(patch['preservePitch'])
        if patch.get('name') is not None:
            segment['mcp_name'] = str(patch['name'])
            if material is not None and kind != 'text':
                material['material_name'] = str(patch['name'])
        _refresh_track_indices(content)
    return {**project.summary(), 'clipId': segment['id'],
            'clip': _segment_summary(content, track, segment)}


def clip_masks(path, clipId, masks):
    project = _project(path)
    with project.edit('clip_masks'):
        track, segment = _find_segment(project.content, clipId)
        if _track_kind(track) in ('audio', 'effect'):
            raise RuntimeError(f'{_track_kind(track)} clips have no picture to mask')
        created = _set_masks(project, segment, masks or [])
    return {**project.summary(), 'clipId': segment['id'],
            'createdIds': created, 'maskCount': len(created),
            'limited': bool(len(masks or []) > 1),
            'note': 'CapCut has one mask per clip; extra masks are kept as metadata.'}


def clip_stabilize(path, clipId, stabilize=None):
    project = _project(path)
    with project.edit('clip_stabilize'):
        track, segment = _find_segment(project.content, clipId)
        if _track_kind(track) != 'video':
            raise RuntimeError('stabilization only applies to video clips')
        material = _segment_material(project.content, segment)
        if not material:
            raise RuntimeError('clip has no material')
        if stabilize is None:
            material['mcp_stabilize'] = None
            stable = material.get('stable')
            if not isinstance(stable, dict):
                stable = {}
                material['stable'] = stable
            stable['stable_level'] = 0
            stable['matrix_path'] = ''
            stable['time_range'] = {'start': 0, 'duration': 0}
            return {**project.summary(), 'clipId': segment['id'],
                    'stabilize': None, 'native': False}
        smoothness = clamp(float(stabilize.get('smoothness', 0.5)), 0, 1)
        tripod = bool(stabilize.get('tripod'))
        level = 3 if tripod else (2 if smoothness >= 0.66 else 1 if smoothness >= 0.2 else 1)
        material['mcp_stabilize'] = {
            'smoothness': smoothness,
            'maxZoom': max(1.0, float(stabilize.get('maxZoom', 1.2))),
            'tripod': tripod,
            'keepRotation': bool(stabilize.get('keepRotation')),
        }
        stable = material.setdefault('stable', {})
        stable['stable_level'] = level
        stable['matrix_path'] = ''
        stable['time_range'] = {'start': _segment_start(segment),
                                 'duration': _segment_duration(segment)}
    return {**project.summary(), 'clipId': segment['id'],
            'stabilize': deepcopy(material.get('mcp_stabilize')),
            'native': 'stabilization preview runs in CapCut when the clip is rendered'}


# ---------------------------------------------------------------- keyframes

def _keyframe_property(name):
    name = str(name or '')
    if name not in PROPERTY_MAP:
        raise RuntimeError(f'unsupported keyframe property: {name!r} '
                           f'(use one of {", ".join(PROPERTY_MAP)})')
    return PROPERTY_MAP[name][0]


def _keyframe_entry(segment, property_type):
    kfs = segment.setdefault('common_keyframes', [])
    for entry in kfs:
        if entry.get('property_type') == property_type:
            return entry
    entry = {'id': cc.uid(), 'material_id': '', 'property_type': property_type,
             'keyframe_list': []}
    kfs.append(entry)
    return entry


def _keyframe_set(segment, property_name, at, value, ease=None):
    property_type = _keyframe_property(property_name)
    duration = max(1, _segment_duration(segment))
    at = clamp(float(at), 0.0, 1.0)
    offset = int(round(at * duration))
    if property_name == 'offsetX':
        value = float(value) * 2.0
    elif property_name == 'offsetY':
        value = -float(value) * 2.0
    entry = _keyframe_entry(segment, property_type)
    keys = entry.setdefault('keyframe_list', [])
    frame = max(1, int(round(frame_us({'fps': 30}))))
    for index, key in enumerate(list(keys)):
        if abs(int(key.get('time_offset') or 0) - offset) <= frame // 2:
            keys.pop(index)
            break
    key = {
        'id': cc.uid(),
        'curveType': 'Line',
        'time_offset': offset,
        'left_control': {'x': 0.0, 'y': 0.0},
        'right_control': {'x': 0.0, 'y': 0.0},
        'values': [float(value)],
        'string_value': '',
        'graphID': '',
    }
    if ease and len(ease) == 4:
        key['curveType'] = 'FreeCurveInOut'
        key['left_control'] = {'x': float(ease[0]), 'y': float(ease[1])}
        key['right_control'] = {'x': float(ease[2]), 'y': float(ease[3])}
    keys.append(key)
    keys.sort(key=lambda k: int(k.get('time_offset') or 0))
    if property_name in ('stretchX', 'stretchY'):
        segment['uniform_scale'] = {'on': False, 'value': 1.0}
    return {'property': property_name, 'propertyType': property_type,
            'at': at, 'timeOffset': offset, 'value': float(value)}


def eval_keyframes(segment, property_type, at_us):
    """Linear value of a segment property at a timeline instant, or None."""
    keys = []
    for entry in segment.get('common_keyframes') or []:
        if entry.get('property_type') == property_type:
            keys = sorted(entry.get('keyframe_list') or [],
                          key=lambda k: int(k.get('time_offset') or 0))
            break
    if not keys:
        return None
    start = _segment_start(segment)
    offset = max(0, int(at_us) - start)
    if offset <= int(keys[0].get('time_offset') or 0):
        values = keys[0].get('values') or [0]
        return float(values[0])
    if offset >= int(keys[-1].get('time_offset') or 0):
        values = keys[-1].get('values') or [0]
        return float(values[0])
    for a, b in zip(keys, keys[1:]):
        ta = int(a.get('time_offset') or 0)
        tb = int(b.get('time_offset') or 0)
        if ta <= offset <= tb and tb > ta:
            va = float((a.get('values') or [0])[0])
            vb = float((b.get('values') or [0])[0])
            t = (offset - ta) / float(tb - ta)
            return va + (vb - va) * t
    values = keys[-1].get('values') or [0]
    return float(values[0])


def keyframe_set(path, clipId, property, at, value, ease=None):
    project = _project(path)
    with project.edit('keyframe_set'):
        track, segment = _find_segment(project.content, clipId)
        if _track_kind(track) in ('audio', 'effect') and property not in ('volume',):
            raise RuntimeError(f'{_track_kind(track)} clips only keyframe volume')
        result = _keyframe_set(segment, property, at, value, ease)
    return {**project.summary(), 'clipId': segment['id'], **result}


def keyframe_clear(path, clipId, property, at):
    project = _project(path)
    with project.edit('keyframe_clear'):
        _track, segment = _find_segment(project.content, clipId)
        property_type = _keyframe_property(property)
        duration = max(1, _segment_duration(segment))
        offset = int(round(clamp(float(at), 0, 1) * duration))
        removed = False
        for entry in list(segment.get('common_keyframes') or []):
            if entry.get('property_type') != property_type:
                continue
            keys = entry.setdefault('keyframe_list', [])
            before = len(keys)
            entry['keyframe_list'] = [k for k in keys
                                      if int(k.get('time_offset') or 0) != offset]
            removed = len(entry['keyframe_list']) < before
            if not entry['keyframe_list']:
                segment['common_keyframes'].remove(entry)
    return {**project.summary(), 'clipId': segment['id'],
            'property': property, 'removed': removed}


def mask_key_set(path, clipId, maskId, number, at, value, ease=None):
    project = _project(path)
    with project.edit('mask_key_set'):
        _track, segment = _find_segment(project.content, clipId)
        number = str(number)
        if number not in MASK_NUMBERS:
            raise RuntimeError(f'mask number must be one of {", ".join(MASK_NUMBERS)}')
        mask = None
        for ref in segment.get('extra_material_refs') or []:
            mat = _find_material_by_id(project.content, ref)
            if isinstance(mat, dict) and mat.get('type') == 'mask':
                if str(mat.get('id')) == str(maskId) or str(mat.get('id')).startswith(str(maskId)):
                    mask = mat
                    break
        if mask is None:
            return {**project.summary(), 'clipId': segment['id'],
                    'maskId': maskId, 'changed': False,
                    'reason': 'mask not found on this clip'}
        keys = mask.setdefault('mcp_keys', {})
        items = keys.setdefault(number, [])
        at = clamp(float(at), 0, 1)
        items[:] = [k for k in items if abs(float(k.get('at') or 0) - at) > 1e-6]
        items.append({'at': at, 'value': float(value),
                      'ease': list(ease) if ease else [0, 0, 1, 1]})
        items.sort(key=lambda k: float(k.get('at') or 0))
        mask.setdefault('mcp_mask', {})['keys'] = deepcopy(keys)
    return {**project.summary(), 'clipId': segment['id'],
            'maskId': mask['id'], 'number': number, 'at': at,
            'value': float(value), 'changed': True}


def effect_key_set(path, clipId, entry, key, at, value, ease=None):
    project = _project(path)
    with project.edit('effect_key_set'):
        _track, segment = _find_segment(project.content, clipId)
        effects = [mat for _ref, mat in _effect_refs(project.content, segment)]
        if entry < 0 or entry >= len(effects):
            return {**project.summary(), 'clipId': segment['id'],
                    'changed': False, 'reason': 'effect index out of range'}
        mat = effects[entry]
        keys = mat.setdefault('mcp_keys', {})
        items = keys.setdefault(str(key), [])
        at = clamp(float(at), 0, 1)
        items[:] = [k for k in items if abs(float(k.get('at') or 0) - at) > 1e-6]
        items.append({'at': at, 'value': float(value),
                      'ease': list(ease) if ease else [0, 0, 1, 1]})
        items.sort(key=lambda k: float(k.get('at') or 0))
        for param in mat.get('adjust_params') or []:
            if str(param.get('name')) == str(key):
                param['value'] = float(value)
                break
        else:
            mat.setdefault('adjust_params', []).append(
                {'name': str(key), 'value': float(value), 'default_value': float(value)})
    return {**project.summary(), 'clipId': segment['id'],
            'effectId': mat.get('id'), 'key': key, 'at': at,
            'value': float(value), 'changed': True}


# ---------------------------------------------------------------- text / captions / titles

def _text_track(project, prefer_largest=True):
    tracks = [t for t in project.content.get('tracks') or [] if _track_kind(t) == 'text']
    if not tracks:
        return None
    if prefer_largest:
        return max(tracks, key=lambda t: len(t.get('segments') or []))
    return tracks[0]


def _make_text_segment(project, track, text, start, duration, style=None,
                       offset_y=None):
    material = new_text_material(text, style)
    _add_material(project.content, 'texts', material)
    seg, _speed = _make_segment(project, material, 'text',
                                snap_project(project.content, usec(start)),
                                target_duration=max(1, snap_project(project.content, usec(duration))))
    if offset_y is not None:
        clip = seg.setdefault('clip', _clip_defaults())
        clip['transform'] = {'x': 0.0, 'y': -float(offset_y) * 2.0}
    track.setdefault('segments', []).append(seg)
    track['segments'] = sorted(track['segments'], key=lambda s: _segment_start(s))
    return seg


def _deoverlap_cues(cues, gap=0.02):
    items = sorted(({'start': float(c['start']), 'end': float(c['end']),
                     'text': c.get('text', '')} for c in cues),
                   key=lambda c: c['start'])
    out = []
    for index, cue in enumerate(items):
        if index + 1 < len(items):
            cue['end'] = min(cue['end'], items[index + 1]['start'] - gap)
        if cue['end'] - cue['start'] >= 0.08:
            out.append(cue)
    return out


def captions_add(path, captions, offsetY=0.35, style=None):
    project = _project(path)
    if not captions:
        raise RuntimeError('captions must not be empty')
    with project.edit('captions_add'):
        content = project.content
        track = _text_track(project, prefer_largest=True)
        if track is None:
            track = new_track('text')
            content.setdefault('tracks', []).append(track)
        cues = _deoverlap_cues(captions)
        style_source = track
        if any(_segments_overlapping(track, usec(c['start']), usec(c['end']))
               for c in cues):
            track = new_track('text')
            content.setdefault('tracks', []).append(track)
        created = []
        if style:
            base_style = _normalize_text_style(style, None)
        elif style_source.get('segments'):
            mat = _segment_material(content, style_source['segments'][0]) or {}
            base_style = _style_from_material(mat) if mat else _default_text_style()
        else:
            base_style = _default_text_style()
        for cue in cues:
            style_for_cue = dict(base_style or {})
            style_for_cue['content'] = cue['text']
            style_for_cue['runs'] = []
            # do not inherit a single caption's content as the shared style
            seg = _make_text_segment(project, track, cue['text'],
                                     cue['start'], max(0.08, cue['end'] - cue['start']),
                                     style_for_cue, float(offsetY))
            created.append(seg['id'])
        _refresh_track_indices(content)
    return {**project.summary(), 'trackId': track['id'],
            'createdId': created[-1] if created else None,
            'createdIds': created, 'count': len(created)}


def _find_title_track(project, start, end, above=False):
    content = project.content
    text_tracks = [t for t in content.get('tracks') or [] if _track_kind(t) == 'text']
    candidates = list(reversed(text_tracks)) if above else text_tracks
    for track in candidates:
        if not _segments_overlapping(track, start, end):
            return track
    track = new_track('text')
    track['name'] = 'titles'
    if above:
        content.setdefault('tracks', []).append(track)
    else:
        content.setdefault('tracks', []).append(track)
    return track


def title_add(path, start, style=None, duration=None, offsetY=None,
              trackId=None, above=False):
    project = _project(path)
    style = style or {}
    text = style.get('content') or 'Your text'
    duration = float(duration) if duration is not None else 4.0
    start_us = snap_project(project.content, usec(start))
    dur_us = max(1, snap_project(project.content, usec(duration)))
    with project.edit('title_add'):
        content = project.content
        if trackId:
            track = _track_by_id(content, trackId)
            if _track_kind(track) != 'text':
                raise RuntimeError('titles need a text track')
        else:
            track = _find_title_track(project, start_us, start_us + dur_us, bool(above))
        seg = _make_text_segment(project, track, text, sec(start_us), sec(dur_us),
                                 style, offsetY)
        _refresh_track_indices(content)
    return {**project.summary(), 'createdId': seg['id'], 'clipId': seg['id'],
            'trackId': track['id'], 'start': sec(start_us),
            'duration': sec(dur_us), 'text': text}


def _title_material(project, clipId):
    _track, segment = _find_segment(project.content, clipId)
    material = _segment_material(project.content, segment)
    if not material or not (material.get('content') or material.get('font_size') is not None):
        raise RuntimeError('clip is not a text clip')
    return segment, material


def title_style_words(path, clipId, start, end, patch):
    project = _project(path)
    with project.edit('title_style_words'):
        segment, material = _title_material(project, clipId)
        style = _style_from_material(material)
        text = material.get('mcp_original_text') or style.get('content') or ''
        start = max(0, min(len(text), int(start)))
        end = max(start, min(len(text), int(end)))
        if end <= start:
            raise RuntimeError('start/end select no characters')
        runs = [r for r in style.get('runs') or []
                if not (int(r.get('start') or 0) < end and int(r.get('end') or 0) > start)]
        run = {'start': start, 'end': end}
        for key in ('color', 'fontScale', 'fontWeight', 'italic', 'strikethrough',
                    'strokeColor', 'underline'):
            if patch.get(key) is not None:
                run[key] = patch[key]
        runs.append(run)
        runs.sort(key=lambda r: int(r.get('start') or 0))
        style['runs'] = runs
        material['mcp_style'] = style
        material['mcp_runs'] = runs
        _build_text_styles(material)
    return {**project.summary(), 'clipId': segment['id'],
            'start': start, 'end': end, 'runs': len(runs)}


def title_clear_word_styles(path, clipId, start=None, end=None):
    project = _project(path)
    with project.edit('title_clear_word_styles'):
        segment, material = _title_material(project, clipId)
        style = _style_from_material(material)
        text = material.get('mcp_original_text') or style.get('content') or ''
        start = 0 if start is None else max(0, min(len(text), int(start)))
        end = len(text) if end is None else max(start, min(len(text), int(end)))
        runs = []
        removed = 0
        for run in style.get('runs') or []:
            rs, re_ = int(run.get('start') or 0), int(run.get('end') or 0)
            if re_ <= start or rs >= end:
                runs.append(run)
            elif rs < start or re_ > end:
                # keep the part of the run outside the cleared span
                if rs < start:
                    runs.append({**run, 'end': start})
                if re_ > end:
                    runs.append({**run, 'start': end})
                removed += 1
            else:
                removed += 1
        style['runs'] = runs
        material['mcp_style'] = style
        material['mcp_runs'] = runs
        _build_text_styles(material)
    return {**project.summary(), 'clipId': segment['id'],
            'start': start, 'end': end, 'removed': removed, 'runs': len(runs)}


# ---------------------------------------------------------------- effects / edit bus

def effect_layer_add(path, effectId, name, start, duration=None, sound=False,
                     trackId=None):
    project = _project(path)
    if sound:
        with project.edit('effect_layer_add'):
            content = project.content
            created = []
            for track in content.get('tracks') or []:
                if _track_kind(track) != 'audio':
                    continue
                for segment in track.get('segments') or []:
                    if _segment_start(segment) < usec(start) + usec(duration or 5) and \
                            _segment_end(segment) > usec(start):
                        chain = [{'id': effectId, 'params': {}}]
                        created += _replace_audio_effects(project, segment, chain)
            return {**project.summary(), 'sound': True,
                    'createdIds': created, 'count': len(created)}
    with project.edit('effect_layer_add'):
        content = project.content
        if trackId:
            track = _track_by_id(content, trackId)
            if _track_kind(track) != 'effect':
                raise RuntimeError('effects need an effect track')
        else:
            track = next((t for t in reversed(content.get('tracks') or [])
                          if _track_kind(t) == 'effect'), None)
            if track is None:
                track = new_effect_track()
                content.setdefault('tracks', []).append(track)
        material = new_effect_material(effectId, {}, 'global')
        material['name'] = name or material.get('name') or effectId
        _add_material(content, 'video_effects', material)
        seg, _speed = _make_segment(project, material, 'effect',
                                    snap_project(content, usec(start)),
                                    target_duration=max(1, snap_project(
                                        content, usec(duration if duration is not None else 5))))
        track.setdefault('segments', []).append(seg)
        track['segments'] = sorted(track['segments'], key=lambda s: _segment_start(s))
        _refresh_track_indices(content)
    return {**project.summary(), 'createdId': seg['id'], 'clipId': seg['id'],
            'trackId': track['id'], 'effectId': material.get('effect_id'),
            'name': material.get('name')}


def effects_list(kind=None):
    packages = cat.list_packages(kind)
    result = {'ok': True, 'kind': kind, 'count': len(packages), 'effects': packages}
    if str(kind or '').lower() == 'filter' and not packages:
        result['note'] = ('The CapCut namespace has no standalone filter packages in '
                          'this catalogue; use scene effects or per-clip '
                          'videoEffects/filters with an effect id.')
    return result


# ---------------------------------------------------------------- edit bus

def _apply_command(project, command):
    command = command or {}
    op = command.get('op') or command.get('command') or command.get('type')
    if not op:
        raise RuntimeError('edit command needs an op')
    path = project.folder
    key = str(op)
    normalized = re.sub(r'[^a-z0-9]', '', key.lower())
    if normalized == 'batch':
        created = None
        for child in command.get('commands') or []:
            result = _apply_command(project, child)
            if isinstance(result, dict) and result.get('createdId'):
                created = result['createdId']
        return {'createdId': created}
    mapping = {
        'addclip': lambda c: clip_add(path, c.get('mediaId') or c.get('media_id'),
                                      c.get('trackId') or c.get('track_id'),
                                      c.get('start', 0), c.get('ripple', False)),
        'addclips': lambda c: clips_add_sequence(path, c.get('mediaIds') or c.get('media_ids'),
                                                 c.get('start', 0),
                                                 c.get('trackId') or c.get('track_id'),
                                                 c.get('ripple', False)),
        'addcaptions': lambda c: captions_add(path, c.get('captions') or [],
                                              c.get('offsetY', 0.35), c.get('style')),
        'addtitle': lambda c: title_add(path, c.get('start', 0), c.get('style'),
                                        c.get('duration'), c.get('offsetY'),
                                        c.get('trackId') or c.get('track_id'),
                                        c.get('above', False)),
        'stylewords': lambda c: title_style_words(
            path, c.get('clipId') or c.get('clip_id'), c.get('start', 0),
            c.get('end', 0), c.get('patch') or {}),
        'clearwordstyles': lambda c: title_clear_word_styles(
            path, c.get('clipId') or c.get('clip_id'), c.get('start'), c.get('end')),
        'trimclip': lambda c: clip_trim(path, c.get('clipId') or c.get('clip_id'),
                                        c.get('edge', 'end'), c.get('delta', 0),
                                        c.get('ripple', False)),
        'setclipspeed': lambda c: clip_speed(path, c.get('clipId') or c.get('clip_id'),
                                             c.get('speed', 1),
                                             c.get('ripple', False)),
        'setclipspeed2': lambda c: clip_speed(path, c.get('clipId') or c.get('clip_id'),
                                              c.get('speed', 1), c.get('ripple', False)),
        'setclipframe': lambda c: clip_frame(
            path, c.get('clipId') or c.get('clip_id'), c.get('framing', 'fit'),
            c.get('at', 0)),
        'stabilizeclip': lambda c: clip_stabilize(
            path, c.get('clipId') or c.get('clip_id'), c.get('stabilize')),
        'setcliptransform': lambda c: clip_transform(
            path, c.get('clipId') or c.get('clip_id'), c.get('offsetX'), c.get('offsetY'),
            c.get('rotation'), c.get('scale'), c.get('stretchX'), c.get('stretchY')),
        'updateclip': lambda c: clip_update(path, c.get('clipId') or c.get('clip_id'),
                                            c.get('patch') or {}),
        'setmasks': lambda c: clip_masks(path, c.get('clipId') or c.get('clip_id'),
                                         c.get('masks') or []),
        'splitclips': lambda c: clips_split(path, c.get('clipIds') or c.get('clip_ids'),
                                            c.get('time', 0)),
        'removeclips': lambda c: clips_remove(path, c.get('clipIds') or c.get('clip_ids'),
                                              c.get('ripple', False)),
        'moveclips': lambda c: clips_move(path, c.get('moves') or []),
        'addtrack': lambda c: track_add(path, c.get('bottom', False), c.get('kind', 'video')),
        'settimelinebackground': lambda c: timeline_background(
            path, c.get('timelineId') or c.get('timeline_id'),
            c.get('background') or {'kind': 'none'}),
        'timelinelinkage': lambda c: timeline_linkage(
            path, c.get('timelineId') or c.get('timeline_id'), c.get('on', True)),
        'setlinkage': lambda c: timeline_linkage(
            path, c.get('timelineId') or c.get('timeline_id'), c.get('on', True)),
        'addeffect': lambda c: effect_layer_add(
            path, c.get('effectId') or c.get('effect_id'), c.get('name') or '',
            c.get('start', 0), c.get('duration'), c.get('sound', False),
            c.get('trackId') or c.get('track_id')),
        'setkeyframe': lambda c: keyframe_set(
            path, c.get('clipId') or c.get('clip_id'), c.get('property'),
            c.get('at', 0), c.get('value', 0), c.get('ease')),
        'clearkeyframe': lambda c: keyframe_clear(
            path, c.get('clipId') or c.get('clip_id'), c.get('property'), c.get('at', 0)),
        'setmaskkey': lambda c: mask_key_set(
            path, c.get('clipId') or c.get('clip_id'), c.get('maskId') or c.get('mask_id'),
            c.get('number'), c.get('at', 0), c.get('value', 0), c.get('ease')),
        'seteffectkey': lambda c: effect_key_set(
            path, c.get('clipId') or c.get('clip_id'), c.get('entry', 0),
            c.get('key'), c.get('at', 0), c.get('value', 0), c.get('ease')),
        'addmedia': lambda c: media_import(path, c.get('file')),
        'importmedia': lambda c: media_import(path, c.get('file')),
        'setprojectvideo': lambda c: project_set_video(path, c.get('video') or {}),
        'saveproject': lambda c: project_save(path, c.get('name')),
    }
    fn = mapping.get(normalized)
    if fn is None:
        # common camelCase spellings
        aliases = {
            'setclipmasks': 'setmasks', 'addclipssequence': 'addclips',
            'clipadd': 'addclip', 'clipsaddsequence': 'addclips',
            'clipsmove': 'moveclips', 'clipsremove': 'removeclips',
            'clipssplit': 'splitclips', 'cliptrim': 'trimclip',
            'clipspeed': 'setclipspeed', 'clippframe': 'setclipframe',
            'clipframe': 'setclipframe', 'cliptransform': 'setcliptransform',
            'clipupdate': 'updateclip', 'clipmasks': 'setmasks',
            'clipstabilize': 'stabilizeclip', 'captionsadd': 'addcaptions',
            'titleadd': 'addtitle', 'titlestylewords': 'stylewords',
            'titleclearwordstyles': 'clearwordstyles',
            'keyframeset': 'setkeyframe', 'keyframeclear': 'clearkeyframe',
            'maskkeyset': 'setmaskkey', 'effectkeyset': 'seteffectkey',
            'effectlayeradd': 'addeffect', 'trackadd': 'addtrack',
            'timelinebackground': 'settimelinebackground',
            'timelinelinkage': 'timelinelinkage',
            'mediaimport': 'addmedia', 'projectsetvideo': 'setprojectvideo',
            'projectsave': 'saveproject',
            'settimelinebackgroundcolor': 'settimelinebackground',
        }
        fn = mapping.get(aliases.get(normalized, ''))
    if fn is None:
        raise RuntimeError(f'unknown edit command: {op!r}')
    return fn(command)


def edit_apply(path, command):
    project = _project(path)
    with project.edit('edit_apply'):
        result = _apply_command(project, command)
    return {**project.summary(), 'result': result}


def edit_undo(path):
    project = _project(path)
    return {**project.undo(), 'undone': True}


def edit_redo(path):
    project = _project(path)
    return {**project.redo(), 'redone': True}


# ---------------------------------------------------------------- templates

def _template_root():
    return _expand(os.environ.get('CAPCUT_TEMPLATES') or os.path.join(HERE, 'templates'))


def _template_manifest(folder):
    manifest_path = os.path.join(folder, 'mcp_template.json')
    if os.path.isfile(manifest_path):
        try:
            manifest = cc.read_json(manifest_path)
        except Exception:
            manifest = {}
    else:
        manifest = {}
    cp = cc.content_path(folder)
    slots = list(manifest.get('slots') or [])
    if not slots and cp:
        try:
            content = cc.read_json(cp)
            for mat in (content.get('materials') or {}).get('videos') or []:
                if mat.get('mcp_slot'):
                    slots.append({'id': mat.get('mcp_slot'), 'name': mat.get('mcp_slot'),
                                  'kind': 'image' if mat.get('type') == 'photo' else 'video'})
        except Exception:
            pass
    return {
        'path': folder,
        'name': manifest.get('name') or os.path.basename(folder),
        'description': manifest.get('description') or '',
        'slots': slots,
    }


def template_list():
    roots = [_template_root(), cc.drafts_root()]
    found = []
    seen = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder) or folder in seen:
                continue
            if not os.path.isfile(os.path.join(folder, 'mcp_template.json')) and \
                    not os.path.isfile(os.path.join(folder, 'draft_content.json')):
                continue
            if not os.path.isfile(os.path.join(folder, 'draft_content.json')):
                continue
            if os.path.isfile(os.path.join(folder, 'mcp_template.json')) or \
                    'template' in name.lower():
                seen.add(folder)
                found.append(_template_manifest(folder))
    return {'ok': True, 'templates': found, 'root': _template_root()}


def template_instantiate(template, location, name, fills):
    _require_capcut_closed()
    template_folder = resolve_folder(template)
    manifest = _template_manifest(template_folder)
    fills = fills or []
    dest = _new_project_folder(location or cc.drafts_root(), name)
    shutil.copytree(template_folder, dest)
    shutil.rmtree(os.path.join(dest, cc.BACKUP_DIR), ignore_errors=True)
    if os.path.isfile(os.path.join(dest, BIN_NAME)):
        os.remove(os.path.join(dest, BIN_NAME))
    if os.path.isfile(os.path.join(dest, 'draft_cover.jpg')):
        try:
            os.remove(os.path.join(dest, 'draft_cover.jpg'))
        except OSError:
            pass
    try:
        cp = cc.content_path(dest)
        content = cc.read_json(cp)
        filled = []
        by_id = {str(s.get('id')): s for s in manifest.get('slots') or []}
        by_name = {str(s.get('name')).lower(): s for s in manifest.get('slots') or [] if s.get('name')}
        for fill in fills:
            slot = fill.get('slot') or fill.get('id') or fill.get('name')
            file = fill.get('file') or fill.get('path')
            if not slot or not file:
                continue
            spec = by_id.get(str(slot)) or by_name.get(str(slot).lower())
            probe = probe_media(file)
            target = None
            for key in ('videos', 'audios', 'images'):
                for mat in (content.get('materials') or {}).get(key) or []:
                    if mat.get('mcp_slot') == slot or mat.get('id') == slot or \
                            str(slot).lower() in (str(mat.get('material_name') or '').lower(),
                                                  str(mat.get('name') or '').lower()):
                        target = mat
                        break
                if target:
                    break
            if target is None and spec:
                # no explicit placeholder: fill the first material of the slot's kind
                kind_key = 'audios' if (spec.get('kind') == 'audio') else 'videos'
                arr = (content.get('materials') or {}).get(kind_key) or []
                target = arr[0] if arr else None
            if target is None:
                continue
            target['path'] = probe['path']
            target['material_name'] = os.path.basename(probe['path'])
            target['name'] = os.path.basename(probe['path'])
            target['duration'] = usec(probe['duration'] or 10_800)
            target['width'] = probe.get('width') or target.get('width') or 0
            target['height'] = probe.get('height') or target.get('height') or 0
            target['has_audio'] = probe.get('has_audio')
            target['mcp_slot'] = slot
            fill_id = cc.uid()
            target['local_material_id'] = fill_id
            filled.append({'slot': slot, 'materialId': target['id'],
                           'file': probe['path']})
        filled_ids = {item['materialId'] for item in filled}
        for track in content.get('tracks') or []:
            for segment in track.get('segments') or []:
                if segment.get('material_id') not in filled_ids:
                    continue
                mat = _find_material_by_id(content, segment['material_id'])
                if not mat:
                    continue
                source = segment.get('source_timerange') or {}
                available = int(mat.get('duration') or 0)
                if source and available and int(source.get('start') or 0) + int(source.get('duration') or 0) > available:
                    speed = float(segment.get('speed') or 1.0)
                    source['duration'] = max(1, available - int(source.get('start') or 0))
                    segment.setdefault('target_timerange', {})['duration'] = max(
                        1, int(round(source['duration'] / speed)))
        content['name'] = name
        _recompute_duration(content)
        cc.write_json(cp, content)
        meta_path = os.path.join(dest, 'draft_meta_info.json')
        meta = cc.read_json(meta_path) if os.path.isfile(meta_path) else {}
        meta['draft_name'] = name
        meta['tm_duration'] = content.get('duration') or 0
        meta['tm_draft_create'] = cc.now_us()
        meta['tm_draft_modified'] = cc.now_us()
        meta['draft_fold_path'] = cc.posix(dest)
        meta['draft_root_path'] = cc.posix(cc.drafts_root())
        meta['draft_json_file'] = cc.posix(cp)
        meta.setdefault('draft_cover', '')
        for group in meta.get('draft_materials') or []:
            if isinstance(group, dict) and isinstance(group.get('value'), list):
                group['value'] = []
        cc.write_json(meta_path, meta)
        donor = _schema_donor(cc.drafts_root())
        cc._register_root(cc.drafts_root(), donor, name, dest,
                          content.get('duration') or 0,
                          meta.get('draft_id') or content.get('id') or donor['name'])
        project = Project(dest)
        _PROJECTS[dest] = project
        for item in filled:
            mat = _find_material_by_id(project.content, item['materialId']) or {}
            media = {
                'id': mat.get('local_material_id') or cc.uid(),
                'path': mat.get('path'),
                'kind': ('audio' if str(mat.get('type', '')).startswith(
                    ('extract', 'music', 'record', 'sound')) else
                    'image' if mat.get('type') == 'photo' else 'video'),
                'name': mat.get('material_name') or os.path.basename(mat.get('path') or ''),
                'duration': float(mat.get('duration') or 0) / 1e6,
                'width': mat.get('width'),
                'height': mat.get('height'),
                'has_audio': bool(mat.get('has_audio')),
            }
            project.bin.setdefault('items', []).append(media)
            _register_media_entry(project, media)
        project._save_bin()
        project.save()
        return {**project.summary(), 'template': manifest.get('name'),
                'filled': filled, 'slots': manifest.get('slots') or []}
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


# ---------------------------------------------------------------- version

def capcut_version():
    catalogue = cat.catalogue()
    counts = {k: len(v) for k, v in catalogue.items() if isinstance(v, list)}
    return {
        'ok': True,
        'api': '1.0',
        'build': 'capcut-mcp-editor',
        'app': cc.capcut_version(),
        'mcp': {'mode': 'filesystem', 'writes': 'draft JSON',
                'capcutMustBeClosed': True,
                'draftsRoot': cc.drafts_root()},
        'capabilities': [
            'project create/open/save/close/list/get', 'timeline read',
            'timeline background/linkage', 'track add', 'media import/probe',
            'clip add/sequence/move/remove/split/trim/speed/frame/transform/update',
            'masks', 'keyframes', 'mask/effect param keys', 'captions', 'titles',
            'title word styles', 'effects/transitions', 'effect layers',
            'undo/redo/edit command bus', 'templates', 'preview frame',
            'ffmpeg export', 'stabilize metadata',
        ],
        'catalogue': counts,
        'notes': [
            'CapCut cannot be open while drafts are written.',
            'preview_frame and export_start use ffmpeg and approximate effects, '
            'masks and keyframes; CapCut renders them exactly when it exports.',
        ],
    }

