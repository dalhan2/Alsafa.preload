"""Subtitle parsers for the CapCut MCP server (SRT + ASS)."""
import html
import os
import re


def _parse_srt_time(t):
    t = t.strip().replace(',', '.')
    h, m, s = t.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def _parse_ass_time(t):
    # h:mm:ss.cc
    h, m, s = t.strip().split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_srt(text):
    cues = []
    text = text.replace('\ufeff', '')
    blocks = re.split(r'\r?\n\r?\n+', text.strip())
    for block in blocks:
        lines = block.splitlines()
        for i, line in enumerate(lines):
            m = re.match(
                r'\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})',
                line)
            if not m:
                continue
            start = _parse_srt_time(m.group(1))
            end = _parse_srt_time(m.group(2))
            body = '\n'.join(lines[i + 1:]).strip()
            body = re.sub(r'<[^>]+>', '', body)
            body = html.unescape(body)
            if body and end > start:
                cues.append({'start': start, 'end': end, 'text': body})
            break
    return cues


def parse_ass(text):
    cues = []
    for line in text.splitlines():
        if not line.lower().startswith('dialogue:'):
            continue
        # Format: Dialogue: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
        parts = line.split(',', 9)
        if len(parts) < 10:
            continue
        try:
            start = _parse_ass_time(parts[1])
            end = _parse_ass_time(parts[2])
        except ValueError:
            continue
        body = parts[9]
        body = re.sub(r'\{[^}]*\}', '', body)          # strip override tags
        body = body.replace(r'\N', '\n').replace(r'\n', '\n').replace(r'\h', ' ')
        body = body.strip()
        if body and end > start:
            cues.append({'start': start, 'end': end, 'text': body})
    return cues


def parse_subs(path):
    """Parse .srt or .ass into [{start, end, text}] (seconds)."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f'subtitle file not found: {path}')
    with open(path, encoding='utf-8-sig', errors='replace') as f:
        text = f.read()
    ext = os.path.splitext(path)[1].lower()
    if ext == '.ass' or ext == '.ssa':
        cues = parse_ass(text)
        if not cues:  # some exporters write SRT syntax into .ass
            cues = parse_srt(text)
        return cues
    return parse_srt(text)
