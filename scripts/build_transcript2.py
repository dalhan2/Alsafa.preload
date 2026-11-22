import json, re, sys

src, out_txt, out_cut, out_major, lang = sys.argv[1:6]
lang = lang.lower()
JA = lang == 'ja'
SENT_END = '。！？!?…．.' if JA else '.!?'
MAX_TOKENS = 40 if JA else 16
MAX_CHARS = 70 if JA else 0

data = json.load(open(src, encoding='utf-8'))
events = data.get('events', [])

raw = []
for ev in events:
    base = ev.get('tStartMs', 0)
    for s in (ev.get('segs') or []):
        t = s.get('utf8')
        if not t:
            continue
        raw.append((base + s.get('tOffsetMs', 0), t))
raw.sort(key=lambda x: x[0])

seen, words = set(), []
for t, w in raw:
    k = (t, w)
    if k in seen:
        continue
    seen.add(k)
    words.append((t, w))


def fmt(ms):
    if ms is None:
        return '--:--:--'
    s = int(round(ms / 1000))
    return f"{s//3600:02d}:{s%3600//60:02d}:{s%60:02d}"


lines, cur, cur_start, cur_end, prev_t = [], [], None, None, None


def flush():
    global cur, cur_start, cur_end
    if cur:
        txt = ''.join(cur) if JA else ' '.join(cur)
        if not JA:
            txt = re.sub(r'\s+([.,!?;:])', r'\1', txt)
        txt = re.sub(r'\s+', ' ', txt).strip()
        if txt:
            lines.append((cur_start, cur_end, txt))
    cur, cur_start, cur_end = [], None, None


for t, w in words:
    wt = w.strip()
    if not wt:
        continue
    if prev_t is not None and (t - prev_t) > 1600:
        flush()
    if wt.startswith('>>'):
        flush()
        wt = wt.lstrip('>').strip()
        if not wt:
            prev_t = t
            continue
    if cur_start is None:
        cur_start = t
    cur.append(wt)
    cur_end = t
    prev_t = t
    joined = ''.join(cur) if JA else ' '.join(cur)
    if wt[-1] in SENT_END or len(cur) >= MAX_TOKENS or (MAX_CHARS and len(joined) >= MAX_CHARS):
        flush()
flush()

# collapse true duplicates (same text, near-identical time)
dl = []
for st, en, txt in lines:
    if dl and abs(st - dl[-1][0]) < 400 and dl[-1][2].strip() == txt.strip():
        dl[-1] = (dl[-1][0], en, dl[-1][2])
        continue
    dl.append((st, en, txt))
lines = dl

with open(out_txt, 'w', encoding='utf-8') as f:
    for st, en, txt in lines:
        f.write(f"[{fmt(st)}] {txt}\n")

GAP = 2500
cuts = []
for i in range(len(lines) - 1):
    gap = lines[i + 1][0] - lines[i][1]
    if gap >= GAP:
        cuts.append((lines[i][1], lines[i + 1][0], gap, lines[i][2], lines[i + 1][2]))

dur = fmt(words[-1][0]) if words else '--:--:--'
with open(out_cut, 'w', encoding='utf-8') as f:
    f.write(f"Natural cut points (silence >= {GAP/1000:.1f}s) [{lang}]\n")
    f.write(f"Video duration: {dur}\n")
    f.write(f"Total lines: {len(lines)}   Cut points: {len(cuts)}\n")
    f.write("=" * 70 + "\n\n")
    for a, b, gap, before, after in cuts:
        f.write(f"CUT at {fmt((a+b)//2)}   (silent {gap/1000:.1f}s, {fmt(a)} -> {fmt(b)})\n")
        f.write(f"   ...{before[-90:]}\n   {after[:90]}...\n\n")

major = [c for c in cuts if c[2] >= 5000]
with open(out_major, 'w', encoding='utf-8') as f:
    f.write("Major breaks (silence >= 5.0s) - good scene/clip boundaries\n")
    f.write(f"Video duration: {dur}   Count: {len(major)}\n")
    f.write("=" * 70 + "\n\n")
    for a, b, gap, before, after in major:
        f.write(f"BREAK at {fmt((a+b)//2)}  ({gap/1000:.1f}s gap)\n")
        f.write(f"   before: ...{before[-80:]}\n   after : {after[:80]}...\n\n")

print(f"[{lang}] lines={len(lines)} cutpoints={len(cuts)} major={len(major)} duration={dur}")
