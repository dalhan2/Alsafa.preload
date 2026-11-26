"""render.py - ffmpeg preview frames and exports for CapCut drafts.

preview_frame composes a frame with Pillow (video layers, transforms, masks,
captions/titles) so an agent can look at the edit without opening CapCut.
export_start renders the timeline with ffmpeg. CapCut itself remains the
authority for effects, masks and keyframes; the export is an approximation that
is good for review and for simple caption/trim edits.
"""
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from copy import deepcopy

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capcutlib as cc  # noqa: E402
import editor as ed  # noqa: E402

JOBS = {}
_LOCK = threading.RLock()


class _ProjectView:
    """A snapshot of the in-memory project for a background export."""

    def __init__(self, project):
        self.folder = project.folder
        self.content = deepcopy(project.content)
        self.meta = deepcopy(project.meta)
        self.bin = deepcopy(project.bin)

    @property
    def canvas(self):
        c = self.content.get('canvas_config') or {}
        return {'width': int(c.get('width') or 1920),
                'height': int(c.get('height') or 1080),
                'ratio': c.get('ratio') or '16:9'}

    @property
    def fps(self):
        return ed.project_fps(self.content)


# ---------------------------------------------------------------- time / colour

def _sec(us):
    return float(us or 0) / 1_000_000.0


def _usec(seconds):
    return int(round(float(seconds or 0) * 1_000_000.0))


def _hex_rgb(value, default=(0, 0, 0)):
    parsed = ed.parse_hex_color(value) if value else None
    if not parsed:
        return default
    return tuple(int(round(c * 255)) for c in parsed[:3])


def _alpha_byte(value):
    return int(round(max(0.0, min(1.0, float(value))) * 255))


def _ff_path(path):
    text = str(path).replace('\\', '/')
    return text.replace(':', '\\:')


def _font(path, size, bold=False):
    candidates = [path] if path else []
    if path:
        base, ext = os.path.splitext(path)
        suffix = '_bold' if bold else ''
        candidates.insert(0, base + suffix + ext)
    candidates += [r'C:\Windows\Fonts\arialbd.ttf' if bold else r'C:\Windows\Fonts\arial.ttf',
                   r'C:\Windows\Fonts\arial.ttf']
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, max(6, int(size)))
            except Exception:
                continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- segment helpers

def _segment_at(content, segment, at_us):
    start = int((segment.get('target_timerange') or {}).get('start') or 0)
    duration = int((segment.get('target_timerange') or {}).get('duration') or 0)
    return start <= at_us < start + duration


def _layer_segments(content, at_us):
    """Bottom-to-top visible layers at a timeline instant."""
    out = []
    for z, track in enumerate(content.get('tracks') or []):
        kind = ed._track_kind(track)
        if kind not in ('video', 'image'):
            continue
        for segment in track.get('segments') or []:
            if not segment.get('visible', True):
                continue
            if _segment_at(content, segment, at_us):
                out.append((z, track, segment))
    return out


def _text_segments(content, at_us):
    out = []
    for z, track in enumerate(content.get('tracks') or []):
        if ed._track_kind(track) != 'text':
            continue
        for segment in track.get('segments') or []:
            if not segment.get('visible', True):
                continue
            if _segment_at(content, segment, at_us):
                out.append((z, track, segment))
    return out


def _material(content, segment):
    return ed._find_material_by_id(content, segment.get('material_id')) or {}


# ---------------------------------------------------------------- frame extraction

def _extract_layer(path, source_us, tempdir, name):
    path = str(path or '')
    if not os.path.isfile(path):
        return None
    out = os.path.join(tempdir, f'{name}.png')
    ext = os.path.splitext(path)[1].lower()
    if ext in ed._IMAGE_EXT:
        try:
            img = Image.open(path).convert('RGBA')
            return img
        except Exception:
            return None
    cmd = ['ffmpeg', '-y', '-v', 'error',
           '-ss', f'{max(0.0, source_us / 1e6):.6f}', '-i', path,
           '-frames:v', '1', '-f', 'image2', out]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:
        return None
    if os.path.isfile(out):
        try:
            return Image.open(out).convert('RGBA')
        except Exception:
            return None
    return None


def _crop_points(material):
    crop = material.get('crop') or {}
    left = float(crop.get('upper_left_x') or 0.0)
    right = float(crop.get('upper_right_x') or 1.0)
    top = float(crop.get('upper_left_y') or 0.0)
    bottom = float(crop.get('lower_left_y') or 1.0)
    return left, top, right, bottom


def _mask_value(mat, number, fraction, default):
    keys = (mat.get('mcp_keys') or {}).get(number) or []
    if not keys:
        return default
    keys = sorted(keys, key=lambda k: float(k.get('at') or 0))
    if fraction <= float(keys[0].get('at') or 0):
        return float(keys[0].get('value') if keys[0].get('value') is not None else default)
    if fraction >= float(keys[-1].get('at') or 0):
        return float(keys[-1].get('value') if keys[-1].get('value') is not None else default)
    for a, b in zip(keys, keys[1:]):
        ta, tb = float(a.get('at') or 0), float(b.get('at') or 0)
        if ta <= fraction <= tb and tb > ta:
            va = float(a.get('value') if a.get('value') is not None else default)
            vb = float(b.get('value') if b.get('value') is not None else default)
            return va + (vb - va) * ((fraction - ta) / (tb - ta))
    return default


def _apply_mask(layer, segment, content, at_us):
    masks = segment.get('mcp_masks') or []
    if not masks:
        return layer
    # CapCut is normally one mask; use the first, which is also the top mask.
    spec = masks[0]
    mat = ed._find_material_by_id(content, spec.get('id'))
    if not mat:
        return layer
    cfg = mat.get('config') or {}
    start = int((segment.get('target_timerange') or {}).get('start') or 0)
    duration = max(1, int((segment.get('target_timerange') or {}).get('duration') or 1))
    fraction = max(0.0, min(1.0, (at_us - start) / float(duration)))
    width, height = layer.size
    mask = Image.new('L', (width, height), 0)
    draw = ImageDraw.Draw(mask)
    cx = width / 2.0 + _mask_value(mat, 'x', fraction,
                                   float(cfg.get('centerX') or 0.0) / 2.0) * width
    cy = height / 2.0 + _mask_value(mat, 'y', fraction,
                                    -float(cfg.get('centerY') or 0.0) / 2.0) * height
    scale = _mask_value(mat, 'scale', fraction, 1.0)
    expand = _mask_value(mat, 'expand', fraction, 0.0)
    w = max(1.0, (_mask_value(mat, 'width', fraction,
                              float(cfg.get('width') or 0.66)) * scale + expand) * width)
    h = max(1.0, (_mask_value(mat, 'height', fraction,
                              float(cfg.get('height') or 0.66)) * scale + expand) * height)
    shape = str(mat.get('resource_type') or spec.get('shape') or 'circle').lower()
    rotation = _mask_value(mat, 'rotation', fraction, float(cfg.get('rotation') or 0.0))
    if shape == 'circle':
        draw.ellipse((cx - h / 2, cy - h / 2, cx + h / 2, cy + h / 2), fill=255)
    elif shape == 'rectangle':
        r = _mask_value(mat, 'roundness', fraction,
                        float(cfg.get('roundCorner') or 0.0)) * min(w, h) / 2
        draw.rounded_rectangle((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2),
                               radius=max(0, r), fill=255)
    elif shape in ('heart', 'star', 'pentagram'):
        points = (_heart_points(cx, cy, h / 2) if shape == 'heart'
                  else _star_points(cx, cy, h / 2, 5))
        draw.polygon(points, fill=255)
    else:
        # linear / mirror: a half-plane rotated around the centre
        angle = rotation * math.pi / 180.0
        dx, dy = math.cos(angle), math.sin(angle)
        points = [(cx - dx * width * 2, cy - dy * width * 2),
                  (cx + dx * width * 2, cy + dy * width * 2),
                  (cx + dx * width * 2 - dy * height * 2,
                   cy + dy * width * 2 + dx * height * 2),
                  (cx - dx * width * 2 - dy * height * 2,
                   cy - dy * width * 2 + dx * height * 2)]
        draw.polygon(points, fill=255)
        if shape in ('mirror', 'filmstrip'):
            draw.polygon([(w - x, y) for x, y in points], fill=255)
    if cfg.get('invert'):
        mask = ImageChops.invert(mask)
    feather = _mask_value(mat, 'feather', fraction, float(cfg.get('feather') or 0.0))
    if feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=feather * min(width, height) / 4))
    opacity = _mask_value(mat, 'opacity', fraction, 1.0)
    if opacity < 0.999:
        mask = mask.point(lambda p: int(p * opacity))
    layer.putalpha(ImageChops.multiply(layer.getchannel('A'), mask))
    return layer


def _star_points(cx, cy, radius, points):
    out = []
    for i in range(points * 2):
        angle = -math.pi / 2 + i * math.pi / points
        r = radius if i % 2 == 0 else radius * 0.45
        out.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return out


def _heart_points(cx, cy, radius):
    out = []
    for i in range(60):
        t = i / 60.0 * math.pi * 2
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        out.append((cx + x * radius / 16.0, cy - y * radius / 16.0))
    return out


def _visual_layer(content, segment, at_us, tempdir, index):
    material = _material(content, segment)
    path = material.get('path')
    source = int((segment.get('source_timerange') or {}).get('start') or 0)
    start = int((segment.get('target_timerange') or {}).get('start') or 0)
    speed = float(segment.get('speed') or 1.0)
    source_us = source + int((at_us - start) * speed)
    layer = _extract_layer(path, source_us, tempdir, f'layer_{index}')
    if layer is None:
        return None
    left, top, right, bottom = _crop_points(material)
    if (left, top, right, bottom) != (0.0, 0.0, 1.0, 1.0):
        w, h = layer.size
        layer = layer.crop((int(left * w), int(top * h),
                            max(1, int(right * w)), max(1, int(bottom * h))))
    clip = segment.get('clip') or {}
    flip = clip.get('flip') or {}
    if flip.get('horizontal'):
        layer = layer.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flip.get('vertical'):
        layer = layer.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    scale = clip.get('scale') or {}
    uniform = ed.eval_keyframes(segment, 'UNIFORM_SCALE', at_us)
    kx = ed.eval_keyframes(segment, 'KFTypeScaleX', at_us)
    ky = ed.eval_keyframes(segment, 'KFTypeScaleY', at_us)
    sx = float(uniform if uniform is not None else (kx if kx is not None else scale.get('x', 1.0)))
    sy = float(uniform if uniform is not None else (ky if ky is not None else scale.get('y', 1.0)))
    if sx != 1.0 or sy != 1.0:
        layer = layer.resize((max(1, int(layer.width * sx)),
                              max(1, int(layer.height * sy))), Image.Resampling.LANCZOS)
    rotation = ed.eval_keyframes(segment, 'KFTypeRotation', at_us)
    rotation = float(rotation if rotation is not None else (clip.get('rotation') or 0.0))
    if rotation:
        layer = layer.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)
    alpha = ed.eval_keyframes(segment, 'KFTypeAlpha', at_us)
    alpha = float(alpha if alpha is not None else
                  (clip.get('alpha') if clip.get('alpha') is not None else 1.0))
    if alpha < 0.999:
        layer.putalpha(layer.getchannel('A').point(lambda p: int(p * alpha)))
    layer = _apply_mask(layer, segment, content, at_us)
    return layer


def _text_layer(content, segment, canvas, at_us):
    material = _material(content, segment)
    if not material:
        return None
    data = ed._text_content(material)
    text = str(data.get('text') or '')
    if not text.strip():
        return None
    styles = data.get('styles') or [{}]
    style = styles[0] if isinstance(styles[0], dict) else {}
    solid = (((style.get('fill') or {}).get('content') or {}).get('solid') or {})
    color = solid.get('color') or [1, 1, 1]
    color = tuple(int(round(float(c) * 255)) for c in (list(color) + [1, 1, 1])[:3])
    opacity = float(material.get('global_alpha') if material.get('global_alpha') is not None else 1.0)
    size = float(style.get('size') or 9.0) / 100.0 * canvas[1]
    path = (style.get('font') or {}).get('path') or material.get('font_path') or ''
    font = _font(path, size, bool(style.get('bold')))
    stroke = 0
    strokes = style.get('strokes') or []
    if strokes:
        # CapCut's stroke width is relative to the font size, not the frame.
        stroke = max(0, int(round(float(strokes[0].get('width') or 0.0) * size)))
    stroke_color = (0, 0, 0, _alpha_byte(opacity))
    if strokes:
        stroke_solid = ((strokes[0].get('content') or {}).get('solid') or {})
        sc = stroke_solid.get('color') or [0, 0, 0]
        stroke_color = tuple(int(round(float(c) * 255)) for c in (list(sc) + [1, 1, 1])[:3]) + \
            (_alpha_byte(opacity),)
    shadow = bool(style.get('shadows'))
    line_height = max(1, int(size * 1.2))
    draw = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=max(0, line_height - int(size)),
                                   stroke_width=stroke, align='center')
    tw, th = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])
    pad = stroke * 2 + 8
    layer = Image.new('RGBA', (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    xy = (pad, pad)
    bg_color = material.get('background_color')
    if bg_color:
        bg = ed.parse_hex_color(bg_color) or (0, 0, 0, 0)
        bg_alpha = float(material.get('background_alpha') if
                         material.get('background_alpha') is not None else bg[3])
        radius = float(material.get('background_round_radius') or 0.0) * canvas[1]
        d.rounded_rectangle((2, 2, layer.width - 2, layer.height - 2),
                            radius=max(0, radius),
                            fill=(int(bg[0] * 255), int(bg[1] * 255),
                                  int(bg[2] * 255), _alpha_byte(bg_alpha)))
    if shadow:
        d.multiline_text((xy[0] + 4, xy[1] + 4), text, font=font, fill=(0, 0, 0, _alpha_byte(0.65 * opacity)),
                         spacing=max(0, line_height - int(size)), align='center')
    d.multiline_text(xy, text, font=font, fill=color + (_alpha_byte(opacity),),
                     spacing=max(0, line_height - int(size)), align='center',
                     stroke_width=stroke, stroke_fill=stroke_color)
    clip = segment.get('clip') or {}
    transform = clip.get('transform') or {}
    tx = ed.eval_keyframes(segment, 'KFTypePositionX', at_us)
    ty = ed.eval_keyframes(segment, 'KFTypePositionY', at_us)
    tx = float(tx if tx is not None else (transform.get('x') or 0.0))
    ty = float(ty if ty is not None else (transform.get('y') or 0.0))
    cx = canvas[0] / 2.0 + tx * canvas[0] / 2.0
    cy = canvas[1] / 2.0 - ty * canvas[1] / 2.0
    rotation = ed.eval_keyframes(segment, 'KFTypeRotation', at_us)
    rotation = float(rotation if rotation is not None else (clip.get('rotation') or 0.0))
    uniform = ed.eval_keyframes(segment, 'UNIFORM_SCALE', at_us)
    if uniform:
        layer = layer.resize((max(1, int(layer.width * uniform)),
                              max(1, int(layer.height * uniform))),
                             Image.Resampling.LANCZOS)
    clip_alpha = ed.eval_keyframes(segment, 'KFTypeAlpha', at_us)
    clip_alpha = float(clip_alpha if clip_alpha is not None else
                       (clip.get('alpha') if clip.get('alpha') is not None else 1.0))
    if clip_alpha < 0.999:
        layer.putalpha(layer.getchannel('A').point(lambda p: int(p * clip_alpha)))
    if rotation:
        layer = layer.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)
    return layer, (int(cx), int(cy))


def _background(project, content, at_us, tempdir):
    width, height = project.canvas['width'], project.canvas['height']
    background = content.get('mcp_background') or {'kind': 'none'}
    color = content.get('mcp_canvas_color') or content.get(
        'canvas_config', {}).get('background') or '#000000'
    image = Image.new('RGBA', (width, height), _hex_rgb(color) + (255,))
    kind = str(background.get('kind') or 'none').lower()
    if kind == 'blur':
        layers = _layer_segments(content, at_us)
        if layers:
            _z, _track, segment = layers[0]
            layer = _visual_layer(content, segment, at_us, tempdir, 'bg')
            if layer:
                scale = max(width / layer.width, height / layer.height)
                layer = layer.resize((max(1, int(layer.width * scale)),
                                      max(1, int(layer.height * scale))),
                                     Image.Resampling.LANCZOS)
                layer = layer.filter(ImageFilter.GaussianBlur(radius=max(2, width // 40)))
                image = _cover(image, layer)
    elif kind == 'image':
        media = ed._media_spec(project, background.get('mediaId')) if background.get('mediaId') else None
        if media:
            layer = _extract_layer(media.get('path'), 0, tempdir, 'bg_image')
            if layer:
                image = _cover(image, layer)
    return image


def _cover(base, layer):
    scale = max(base.width / layer.width, base.height / layer.height)
    layer = layer.resize((max(1, int(layer.width * scale)),
                          max(1, int(layer.height * scale))),
                         Image.Resampling.LANCZOS)
    x = (base.width - layer.width) // 2
    y = (base.height - layer.height) // 2
    out = base.copy()
    out.alpha_composite(layer, (x, y))
    return out


def _paste_center(canvas, layer, center):
    x = int(center[0] - layer.width / 2)
    y = int(center[1] - layer.height / 2)
    canvas.alpha_composite(layer, (x, y))


def preview_frame(path, time, output=None, width=None, height=None):
    project = ed._project(path)
    content = project.content
    at_us = max(0, _usec(time))
    canvas_size = (project.canvas['width'], project.canvas['height'])
    tempdir = tempfile.mkdtemp(prefix='capcut_mcp_preview_')
    try:
        canvas = _background(project, content, at_us, tempdir)
        layers = []
        for index, (z, track, segment) in enumerate(_layer_segments(content, at_us)):
            layer = _visual_layer(content, segment, at_us, tempdir, index)
            if layer is None:
                continue
            clip = segment.get('clip') or {}
            transform = clip.get('transform') or {}
            cx = canvas_size[0] / 2.0 + float(transform.get('x') or 0.0) * canvas_size[0] / 2.0
            cy = canvas_size[1] / 2.0 - float(transform.get('y') or 0.0) * canvas_size[1] / 2.0
            _paste_center(canvas, layer, (cx, cy))
            layers.append({'clipId': segment.get('id'), 'kind': ed._track_kind(track),
                           'scale': float((clip.get('scale') or {}).get('x') or 1.0),
                           'rotation': float(clip.get('rotation') or 0.0)})
        for z, track, segment in _text_segments(content, at_us):
            result = _text_layer(content, segment, canvas_size, at_us)
            if result:
                layer, center = result
                _paste_center(canvas, layer, center)
                layers.append({'clipId': segment.get('id'), 'kind': 'text'})
        if width or height:
            w = int(width or round(canvas.width * (height / canvas.height)))
            h = int(height or round(canvas.height * (width / canvas.width)))
            canvas = canvas.resize((max(1, w), max(1, h)), Image.Resampling.LANCZOS)
        if not output:
            render_dir = os.path.join(project.folder, '.mcp_render')
            os.makedirs(render_dir, exist_ok=True)
            output = os.path.join(render_dir, f'preview_{int(time * 1000)}.png')
        output = os.path.abspath(output)
        os.makedirs(os.path.dirname(output), exist_ok=True)
        canvas.convert('RGB').save(output, quality=94)
        return {'ok': True, 'path': output, 'time': float(time),
                'width': canvas.width, 'height': canvas.height,
                'layers': layers,
                'note': 'effects, masks and keyframes are approximated in preview'}
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


# ---------------------------------------------------------------- export

def _ass_time(seconds):
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f'{h}:{m:02d}:{s:05.2f}'


def _ass_color(rgb, alpha=0):
    r, g, b = [max(0, min(255, int(round(c * 255)))) for c in rgb[:3]]
    return f'&H{alpha:02X}{b:02X}{g:02X}{r:02X}'


def _ass_escape(text):
    text = str(text or '').replace('\\', '\\\\').replace('{', '\\{').replace('}', '\\}')
    return text.replace('\r\n', '\\N').replace('\n', '\\N')


def _write_ass(project, content, path):
    canvas = project.canvas
    lines = [
        '[Script Info]',
        'ScriptType: v4.00+',
        f'PlayResX: {canvas["width"]}',
        f'PlayResY: {canvas["height"]}',
        'WrapStyle: 0',
        'ScaledBorderAndShadow: yes',
        'YCbCr Matrix: TV.601',
        '',
        '[V4+ Styles]',
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, '
        'BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, '
        'BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
        'Style: Default,Arial,48,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,'
        '0,0,0,0,100,100,0,0,1,0,0,5,20,20,20,1',
    ]
    events = []
    for track in content.get('tracks') or []:
        if ed._track_kind(track) != 'text':
            continue
        for segment in track.get('segments') or []:
            material = _material(content, segment)
            data = ed._text_content(material)
            text = str(data.get('text') or '')
            if not text.strip():
                continue
            styles = data.get('styles') or [{}]
            style = styles[0] if isinstance(styles[0], dict) else {}
            solid = (((style.get('fill') or {}).get('content') or {}).get('solid') or {})
            color = solid.get('color') or [1, 1, 1]
            size = float(style.get('size') or 9.0) / 100.0 * canvas['height']
            font_name = str(material.get('font_name') or style.get('font', {}).get('family') or 'Arial')
            font_name = font_name.split(',')[0].replace("'", '').strip()[:60] or 'Arial'
            stroke = 0.0
            if style.get('strokes'):
                # CapCut's stroke width is relative to the font size.
                stroke = float(style['strokes'][0].get('width') or 0.0) * size
            shadow = 2 if style.get('shadows') else 0
            align = {0: 4, 1: 5, 2: 6}.get(int(material.get('alignment') or 1), 5)
            clip = segment.get('clip') or {}
            transform = clip.get('transform') or {}
            x = canvas['width'] / 2.0 + float(transform.get('x') or 0.0) * canvas['width'] / 2.0
            y = canvas['height'] / 2.0 - float(transform.get('y') or 0.0) * canvas['height'] / 2.0
            rotation = float(clip.get('rotation') or 0.0)
            alpha = float(material.get('global_alpha') if material.get('global_alpha') is not None else 1.0)
            alpha_hex = f'{int(round((1 - max(0, min(1, alpha))) * 255)):02X}'
            start = _sec((segment.get('target_timerange') or {}).get('start'))
            end = start + _sec((segment.get('target_timerange') or {}).get('duration'))
            tags = [f'\\fn{font_name}', f'\\fs{max(6.0, size):.1f}',
                    f'\\c{_ass_color(color, 0)}',
                    f'\\alpha&H{alpha_hex}&', f'\\an{align}',
                    f'\\pos({x:.0f},{y:.0f})', f'\\frz{rotation:.2f}']
            if style.get('bold'):
                tags.append('\\b1')
            if style.get('italic'):
                tags.append('\\i1')
            if style.get('underline'):
                tags.append('\\u1')
            if stroke > 0:
                tags += [f'\\bord{stroke:.2f}', '\\3c&H000000&']
            if shadow:
                tags.append(f'\\shad{shadow}')
            override = '{' + ''.join(tags) + '}'
            events.append(f'Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,'
                          f'0,0,0,,{override}{_ass_escape(text)}')
    lines += ['', '[Events]',
              'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    lines += events
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return len(events)


def _atempo_chain(speed):
    speed = max(0.0625, min(16.0, float(speed or 1.0)))
    out = []
    while speed > 2.0:
        out.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        out.append(0.5)
        speed /= 0.5
    if abs(speed - 1.0) > 1e-6:
        out.append(speed)
    return [f'atempo={value:.6f}' for value in out]


def _fade(project, content, segment):
    for ref in segment.get('extra_material_refs') or []:
        mat = ed._find_material_by_id(content, ref)
        if isinstance(mat, dict) and mat.get('type') == 'audio_fade':
            return _sec(mat.get('fade_in_duration')), _sec(mat.get('fade_out_duration'))
    return 0.0, 0.0


def _has_pair(content, segment):
    vmat = _material(content, segment)
    vpath = cc.posix(vmat.get('path', '')).lower()
    for track in content.get('tracks') or []:
        if ed._track_kind(track) != 'audio':
            continue
        for other in track.get('segments') or []:
            amat = _material(content, other)
            if vpath and cc.posix(amat.get('path', '')).lower() != vpath:
                continue
            if abs(int((other.get('target_timerange') or {}).get('start') or 0) -
                   int((segment.get('target_timerange') or {}).get('start') or 0)) <= 1 and \
                    abs(int((other.get('target_timerange') or {}).get('duration') or 0) -
                        int((segment.get('target_timerange') or {}).get('duration') or 0)) <= 1:
                return True
    return False


def _build_export(project, output, width=None, height=None, rate_num=None,
                  rate_den=None, codec=None, crf=None, preset=None, ten_bit=None,
                  hdr=None, color_range=None):
    content = deepcopy(project.content)
    canvas = project.canvas
    W = int(width or canvas['width'])
    H = int(height or canvas['height'])
    W = W - (W % 2)
    H = H - (H % 2)
    fps = (float(rate_num) / float(rate_den)) if rate_num and rate_den else project.fps
    duration = max(0.04, _sec(content.get('duration')))
    temp = tempfile.mkdtemp(prefix='capcut_mcp_export_')
    ass_path = os.path.join(temp, 'captions.ass')
    has_text = _write_ass(project, content, ass_path) > 0
    inputs = []
    filters = []
    visual = []
    audio = []
    index = 0

    # background source
    bg = content.get('mcp_background') or {'kind': 'none'}
    bg_kind = str(bg.get('kind') or 'none').lower()
    bg_image_input = None
    if bg_kind == 'image' and bg.get('mediaId'):
        try:
            media = ed._media_spec(project, bg.get('mediaId'))
            if media and os.path.isfile(media['path']):
                inputs += ['-loop', '1', '-t', f'{duration:.6f}', '-i', media['path']]
                bg_image_input = index
                index += 1
        except Exception:
            pass
    color = content.get('mcp_canvas_color') or content.get('canvas_config', {}).get('background') or 'black'
    try:
        rgb = _hex_rgb(color)
        color_arg = '0x%02x%02x%02x' % rgb
    except Exception:
        color_arg = 'black'

    for track in content.get('tracks') or []:
        kind = ed._track_kind(track)
        if kind in ('video', 'image'):
            for segment in track.get('segments') or []:
                if not segment.get('visible', True):
                    continue
                mat = _material(content, segment)
                path = mat.get('path')
                if not path or not os.path.isfile(path):
                    continue
                start = _sec((segment.get('target_timerange') or {}).get('start'))
                dur = _sec((segment.get('target_timerange') or {}).get('duration'))
                src = _sec((segment.get('source_timerange') or {}).get('start'))
                srcd = _sec((segment.get('source_timerange') or {}).get('duration'))
                speed = float(segment.get('speed') or 1.0)
                ext = os.path.splitext(path)[1].lower()
                if ext in ed._IMAGE_EXT or mat.get('type') == 'photo':
                    inputs += ['-loop', '1', '-t', f'{max(0.04, dur):.6f}', '-i', path]
                else:
                    inputs += ['-ss', f'{max(0.0, src):.6f}', '-t', f'{max(0.04, srcd):.6f}',
                               '-i', path]
                visual.append({'index': index, 'segment': segment, 'material': mat,
                               'start': start, 'duration': dur, 'speed': speed})
                index += 1
        elif kind == 'audio':
            for segment in track.get('segments') or []:
                mat = _material(content, segment)
                path = mat.get('path')
                if not path or not os.path.isfile(path):
                    continue
                start = _sec((segment.get('target_timerange') or {}).get('start'))
                dur = _sec((segment.get('target_timerange') or {}).get('duration'))
                src = _sec((segment.get('source_timerange') or {}).get('start'))
                srcd = _sec((segment.get('source_timerange') or {}).get('duration'))
                inputs += ['-ss', f'{max(0.0, src):.6f}', '-t', f'{max(0.04, srcd):.6f}',
                           '-i', path]
                audio.append({'index': index, 'segment': segment, 'start': start,
                              'duration': dur, 'speed': float(segment.get('speed') or 1.0)})
                index += 1
    # original sound of video clips that has not been detached
    for track in content.get('tracks') or []:
        if ed._track_kind(track) != 'video':
            continue
        for segment in track.get('segments') or []:
            mat = _material(content, segment)
            if not mat.get('has_audio') or float(segment.get('volume') or 0) <= 0:
                continue
            if _has_pair(content, segment):
                continue
            path = mat.get('path')
            if not path or not os.path.isfile(path):
                continue
            start = _sec((segment.get('target_timerange') or {}).get('start'))
            dur = _sec((segment.get('target_timerange') or {}).get('duration'))
            src = _sec((segment.get('source_timerange') or {}).get('start'))
            srcd = _sec((segment.get('source_timerange') or {}).get('duration'))
            inputs += ['-ss', f'{max(0.0, src):.6f}', '-t', f'{max(0.04, srcd):.6f}',
                       '-i', path]
            audio.append({'index': index, 'segment': segment, 'start': start,
                          'duration': dur, 'speed': float(segment.get('speed') or 1.0)})
            index += 1

    # base layer
    if bg_kind == 'blur' and visual:
        first = visual[0]
        filters.append(
            f'[{first["index"]}:v]setpts=PTS-STARTPTS,scale={W}:{H}:force_original_aspect_ratio=increase,'
            f'crop={W}:{H},boxblur={max(2, W // 40)}:1[base]')
    elif bg_image_input is not None:
        filters.append(
            f'[{bg_image_input}:v]setpts=PTS-STARTPTS,scale={W}:{H}:force_original_aspect_ratio=increase,'
            f'crop={W}:{H}[base]')
    else:
        filters.append(f'color=c={color_arg}:s={W}x{H}:r={fps:.6f}:d={duration:.6f}[base]')

    current = 'base'
    for number, item in enumerate(visual):
        segment = item['segment']
        mat = item['material']
        clip = segment.get('clip') or {}
        scale = clip.get('scale') or {}
        sx = float(scale.get('x') or 1.0)
        sy = float(scale.get('y') or 1.0)
        mw = int(mat.get('width') or 0)
        mh = int(mat.get('height') or 0)
        left, top, right, bottom = _crop_points(mat)
        cw = max(1, int(round(mw * max(0.01, right - left)))) if mw else None
        ch = max(1, int(round(mh * max(0.01, bottom - top)))) if mh else None
        tw = max(2, int(round((cw or 1920) * sx / 2.0) * 2))
        th = max(2, int(round((ch or 1080) * sy / 2.0) * 2))
        chain = [f'[{item["index"]}:v]setpts=PTS-STARTPTS+{item["start"]:.6f}/TB']
        if (left, top, right, bottom) != (0.0, 0.0, 1.0, 1.0):
            chain.append(f'crop=iw*{max(0.01, right - left):.6f}:ih*{max(0.01, bottom - top):.6f}:'
                         f'iw*{left:.6f}:ih*{top:.6f}')
        if item['speed'] != 1.0:
            chain.append(f'setpts=(PTS-{item["start"]:.6f}/TB)/{item["speed"]:.6f}+{item["start"]:.6f}/TB')
        chain.append(f'scale={tw}:{th}')
        flip = clip.get('flip') or {}
        if flip.get('horizontal'):
            chain.append('hflip')
        if flip.get('vertical'):
            chain.append('vflip')
        rotation = float(clip.get('rotation') or 0.0)
        if rotation:
            chain.append(f'rotate={rotation:.6f}*PI/180:ow=rotw(iw):oh=roth(ih):c=none')
        alpha = float(clip.get('alpha') if clip.get('alpha') is not None else 1.0)
        chain.append('format=rgba')
        if alpha < 0.999:
            chain.append(f'colorchannelmixer=aa={alpha:.6f}')
        filters.append(','.join(chain) + f'[v{number}]')
        transform = clip.get('transform') or {}
        cx = W / 2.0 + float(transform.get('x') or 0.0) * W / 2.0
        cy = H / 2.0 - float(transform.get('y') or 0.0) * H / 2.0
        x = int(cx - tw / 2)
        y = int(cy - th / 2)
        filters.append(f'[{current}][v{number}]overlay=x={x}:y={y}:'
                       f'eof_action=pass:repeatlast=0:shortest=0[oc{number}]')
        current = f'oc{number}'

    if has_text:
        filters.append(f'[{current}]ass=filename=\'{_ff_path(ass_path)}\':fontsdir=\'C\\:/Windows/Fonts\'[vout]')
    else:
        filters.append(f'[{current}]null[vout]')

    if audio:
        labels = []
        for number, item in enumerate(audio):
            segment = item['segment']
            volume = float(segment.get('volume') if segment.get('volume') is not None else 1.0)
            chain = [f'[{item["index"]}:a]asetpts=PTS-STARTPTS']
            chain += _atempo_chain(item['speed'])
            if volume != 1.0:
                chain.append(f'volume={volume:.6f}')
            delay = max(0, int(round(item['start'] * 1000)))
            if delay:
                chain.append(f'adelay={delay}|{delay}')
            fade_in, fade_out = _fade(project, content, segment)
            if fade_in > 0:
                chain.append(f'afade=t=in:st={item["start"]:.6f}:d={fade_in:.6f}')
            if fade_out > 0:
                chain.append(f'afade=t=out:st={max(0.0, item["start"] + item["duration"] - fade_out):.6f}:d={fade_out:.6f}')
            filters.append(','.join(chain) + f'[a{number}]')
            labels.append(f'[a{number}]')
        filters.append(f'{"".join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0[aout]')
    else:
        filters.append(f'anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:{duration:.6f}[aout]')

    script = ';'.join(filters)
    script_path = os.path.join(temp, 'filter.txt')
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write(script)
    codec = (codec or 'h264').lower()
    vcodec = {'h264': 'libx264', 'hevc': 'libx265', 'av1': 'libsvtav1'}.get(codec, 'libx264')
    crf = int(crf if crf is not None else 20)
    cmd = ['ffmpeg', '-y', '-hide_banner', '-v', 'error', '-progress', 'pipe:1']
    cmd += inputs
    cmd += ['-filter_complex', script,
            '-map', '[vout]', '-map', '[aout]',
            '-c:v', vcodec, '-crf', str(crf)]
    if preset:
        cmd += ['-preset', str(preset)]
    if codec == 'h264':
        cmd += ['-pix_fmt', 'yuv420p', '-profile:v', 'high']
    elif codec == 'hevc':
        cmd += ['-pix_fmt', 'yuv420p' if not ten_bit else 'yuv420p10le']
    cmd += ['-r', f'{fps:.6f}', '-c:a', 'aac', '-b:a', '192k',
            '-movflags', '+faststart', '-t', f'{duration:.6f}', output]
    return cmd, temp


def _run_export(job_id, project_view, options):
    job = JOBS[job_id]
    temp = None
    try:
        project = project_view
        job['stage'] = 'preparing'
        cmd, temp = _build_export(project, **options)
        job['command'] = cmd
        job['stage'] = 'rendering'
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        job['process'] = proc
        duration = max(0.04, options.get('duration') or project.content.get('duration', 0) / 1e6)
        while True:
            line = proc.stdout.readline() if proc.stdout else ''
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            match = re.search(r'out_time_ms=(\d+)', line)
            if match and duration:
                seconds = int(match.group(1)) / 1_000_000.0
                job['progress'] = max(0.0, min(1.0, seconds / duration))
                job['stage'] = 'rendering'
        stderr = proc.stderr.read() if proc.stderr else ''
        code = proc.wait()
        if code == 0:
            job['state'] = 'done'
            job['stage'] = 'done'
            job['progress'] = 1.0
        elif job.get('cancelled'):
            job['state'] = 'cancelled'
            job['stage'] = 'cancelled'
        else:
            job['state'] = 'failed'
            job['stage'] = 'failed'
            job['error'] = (stderr or 'ffmpeg failed')[-2000:]
    except Exception as exc:
        job['state'] = 'failed'
        job['stage'] = 'failed'
        job['error'] = str(exc)
    finally:
        job['finished'] = time.time()
        if temp:
            shutil.rmtree(temp, ignore_errors=True)


def export_start(path, output, width=None, height=None, rateNum=None, rateDen=None,
                 codec=None, crf=None, preset=None, tenBit=None, hdr=None,
                 colorRange=None):
    project = ed._project(path)
    output = os.path.abspath(str(output))
    if not output.lower().endswith(('.mp4', '.mov', '.mkv')):
        raise RuntimeError('output must be .mp4, .mov or .mkv')
    os.makedirs(os.path.dirname(output), exist_ok=True)
    job_id = cc.uid()
    job = {
        'ok': True, 'job': job_id, 'state': 'running', 'stage': 'queued',
        'progress': 0.0, 'output': output, 'error': None, 'started': time.time(),
        'finished': None, 'process': None, 'command': None,
        'notes': ('CapCut is the exact renderer; this ffmpeg export approximates '
                  'effects, masks and keyframes.'),
    }
    JOBS[job_id] = job
    options = {
        'output': output, 'width': width, 'height': height,
        'rate_num': rateNum, 'rate_den': rateDen, 'codec': codec,
        'crf': crf, 'preset': preset, 'ten_bit': tenBit, 'hdr': hdr,
        'color_range': colorRange,
    }
    thread = threading.Thread(target=_run_export, args=(job_id, _ProjectView(project), options),
                              daemon=True)
    thread.start()
    return {k: v for k, v in job.items() if k not in ('process', 'command')}


def export_status(job):
    found = JOBS.get(str(job))
    if not found:
        raise RuntimeError(f'unknown export job: {job}')
    result = {k: v for k, v in found.items() if k not in ('process', 'command')}
    result['ok'] = found.get('state') not in ('failed',)
    return result


def export_wait(job, timeoutSeconds=60):
    deadline = time.time() + max(0, min(600, float(timeoutSeconds)))
    while time.time() < deadline:
        found = JOBS.get(str(job))
        if not found:
            raise RuntimeError(f'unknown export job: {job}')
        if found.get('state') in ('done', 'cancelled', 'failed'):
            return export_status(job)
        time.sleep(0.2)
    return export_status(job)


def export_cancel(job):
    found = JOBS.get(str(job))
    if not found:
        raise RuntimeError(f'unknown export job: {job}')
    found['cancelled'] = True
    proc = found.get('process')
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    return export_status(job)
