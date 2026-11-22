"""Find chat-rate peaks in a yt-dlp .live_chat.json replay dump.

Buckets text messages into 10s bins, computes a robust baseline, flags bins above
the 90th percentile (ignoring the intro), clusters them into "hype moments",
and prints each moment with its rate, top spammed phrases and sample messages.

Usage:
  python analyze_live_chat.py <file.live_chat.json> <out_digest.txt> [--skip-intro 600]
"""
import json
import re
import sys
from collections import Counter

src, out = sys.argv[1], sys.argv[2]
skip_intro = int(sys.argv[3]) if len(sys.argv) > 3 else 600
BIN = 10  # seconds

data = []
with open(src, encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line or line in ('[', ']'):
            continue
        if line.endswith(','):
            line = line[:-1]
        try:
            data.append(json.loads(line))
        except json.JSONDecodeError:
            pass

msgs = []  # (t, text)
for entry in data:
    r = entry.get('replayChatItemAction') or {}
    t = int(r.get('videoOffsetTimeMsec') or 0) / 1000.0
    for a in r.get('actions') or []:
        item = (a.get('addChatItemAction') or {}).get('item') or {}
        rend = (item.get('liveChatTextMessageRenderer')
                or item.get('liveChatPaidMessageRenderer'))
        if not rend:
            continue
        parts = []
        for run in (rend.get('message') or {}).get('runs') or []:
            if run.get('text'):
                parts.append(run['text'])
        text = ''.join(parts).strip()
        if text:
            msgs.append((t, text))

msgs.sort()
dur = msgs[-1][0] if msgs else 0
n_bins = int(dur // BIN) + 1
bins = [[] for _ in range(n_bins)]
for t, text in msgs:
    bins[min(int(t // BIN), n_bins - 1)].append(text)

rates = [len(b) for b in bins]
analyzed = rates[int(skip_intro // BIN):]
if not analyzed:
    sys.exit('no chat after intro skip')
analyzed_sorted = sorted(analyzed)
thr = analyzed_sorted[int(len(analyzed_sorted) * 0.90)]  # P90 of post-intro rates
med = analyzed_sorted[len(analyzed_sorted) // 2]
peak_rate = max(rates)

# cluster flagged bins (allow 30s gaps inside a moment)
moments = []
cur = None
for i, rate in enumerate(rates):
    flagged = (i * BIN >= skip_intro) and rate >= thr and rate >= med * 2.5
    if flagged:
        if cur and i * BIN - cur['end'] <= 30:
            cur['end'] = (i + 1) * BIN
            cur['bins'].append(i)
        else:
            cur = {'start': i * BIN, 'end': (i + 1) * BIN, 'bins': [i]}
            moments.append(cur)
    elif cur and i * BIN - cur['end'] > 30:
        cur = None

norm = re.compile(r'[^a-zA-Z0-9 ]+')


def top_phrases(texts, k=6):
    c = Counter()
    for tx in texts:
        w = norm.sub('', tx).lower().strip()
        if 1 <= len(w) <= 30:
            c[w] += 1
    return c.most_common(k)


def fmt(t):
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


moments.sort(key=lambda m: -max(rates[i] for i in m['bins']))
with open(out, 'w', encoding='utf-8') as f:
    f.write(f"messages: {len(msgs)}   bins: {n_bins}x{BIN}s   median/10s: {med}   "
            f"P90/10s: {thr}   peak/10s: {peak_rate}\n")
    f.write(f"intro skipped: first {skip_intro}s\n\n")
    for m in moments:
        texts = [tx for i in m['bins'] for tx in bins[i]]
        peak = max(rates[i] for i in m['bins'])
        f.write(f"== {fmt(m['start'])} - {fmt(m['end'])}  "
                f"peak {peak} msgs/10s, {len(texts)} msgs total\n")
        f.write("   top: " + ", ".join(f"{p}x{n}" for p, n in top_phrases(texts)) + "\n")
        shown = 0
        seen = set()
        for i in m['bins']:
            for tx in bins[i]:
                k = norm.sub('', tx).lower()
                if k not in seen and 3 <= len(tx) <= 90:
                    f.write(f"   [{fmt(i * BIN)}] {tx}\n")
                    seen.add(k)
                    shown += 1
                    if shown >= 5:
                        break
            if shown >= 5:
                break
        f.write("\n")
print(f"{out}: {len(moments)} moments (threshold {thr} msgs/10s)")
