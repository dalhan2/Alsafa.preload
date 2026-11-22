"""Extract timestamped comments from a yt-dlp .info.json (with --write-comments).

Outputs a readable digest: video metadata + every comment containing a
timestamp (h:mm:ss / mm:ss), sorted by timestamp with like counts, so a human
can curate a Timestamps markdown from it.

Usage:
  python parse_timestamped_comments.py cjson_<id>.info.json out_digest.txt
"""
import json
import re
import sys
from datetime import timedelta

if len(sys.argv) != 3:
    sys.exit('usage: parse_timestamped_comments.py <info.json> <out_digest.txt>')
src, out = sys.argv[1], sys.argv[2]

with open(src, encoding='utf-8') as f:
    info = json.load(f)

title = info.get('title', '?')
channel = info.get('channel') or info.get('uploader', '?')
upload = (info.get('upload_date') or '?')
if upload != '?' and len(upload) == 8:
    upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}]"
dur = int(info.get('duration') or 0)
length = str(timedelta(seconds=dur)) if dur else '?'
url = info.get('webpage_url') or f"https://www.youtube.com/watch?v={info.get('id')}"

TS = re.compile(r'(?<!\d)(?:(\d{1,2}):)?(\d{1,3}):(\d{2})(?!\d)')


def secs_of(m):
    h = int(m.group(1) or 0)
    return h * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def walk(comments, depth=0):
    for c in comments or []:
        yield c, depth
        for sub in walk(c.get('comments'), depth + 1):
            yield sub


rows = []
all_comments = info.get('comments') or []
n_total = len(all_comments)
for c, depth in walk(all_comments):
    text = (c.get('text') or '').replace('\n', ' ').strip()
    if not text or depth > 0:            # top-level comments only
        continue
    matches = list(TS.finditer(text))
    if not matches:
        continue
    likes = c.get('like_count') or 0
    author = c.get('author') or '?'
    # one row per distinct timestamp in the comment
    seen = set()
    for m in matches:
        t = secs_of(m)
        if t in seen or t > dur + 300:
            continue
        seen.add(t)
        rows.append((t, likes, text[:300], author))

rows.sort(key=lambda r: (r[0], -r[1]))


def fmt(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


with open(out, 'w', encoding='utf-8') as f:
    f.write(f"TITLE: {title}\n")
    f.write(f"CHANNEL: {channel}\n")
    f.write(f"URL: {url}\n")
    f.write(f"UPLOADED: {upload}\n")
    f.write(f"LENGTH: {length}\n")
    f.write(f"COMMENTS TOTAL: {n_total}   TIMESTAMPED: {len(rows)}\n\n")
    prev_t = -999
    for t, likes, text, author in rows:
        gap = '' if t - prev_t >= 1 else '  <-- same moment'
        f.write(f"[{fmt(t)}] ({likes} likes) {text}{gap}\n")
        prev_t = t
print(f"{out}: {len(rows)} timestamped comments of {n_total} total")
