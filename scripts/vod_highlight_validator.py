"""Faithful Python port of the 'VOD Highlight Analyzer' userscript (v4.0) peak logic.

Validates the custom P90 chat-peak analysis by running the userscript's exact
algorithm on the same live_chat.json: 150 bins, msgs/min density, local maxima
(window 2), avg*1.2 threshold, BASE_EMOTE_SENTIMENT scoring, laughter-anchor
ratio categories, NMS 90s, top-5 spikes (plus an extended top-20 for
comparison).

Usage:
  python vod_highlight_validator.py <file.live_chat.json> <duration_sec> <out.txt>
"""
import json
import sys

src, dur_s, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]

# --- constants from the userscript ---
NUM_BINS = 150
MIN_PEAK_SPACING_SEC = 90
WINDOW = 2
AVG_MULT = 1.2

SENTIMENT_ANCHORS = {
    'laughter': ['\U0001f602', '\U0001f923', 'lol', 'lmao', 'haha', 'xd',
                 'keke', 'lul', 'lulw', 'kekw'],
}

BASE_EMOTE_SENTIMENT = {
    '\U0001f602': 0.8, '\U0001f923': 0.9, 'lol': 0.5, 'lmao': 0.7, 'haha': 0.5,
    'xd': 0.5, 'keke': 0.4, 'lul': 0.8, 'lulw': 0.8, 'kekw': 0.8,
    '\U0001f525': 1.0, 'wow': 0.8, 'pog': 1.2, 'pogchamp': 1.5, 'pogu': 1.2,
    'pagman': 1.2, 'peepoarrive': 0.8, 'hypers': 1.0, 'ez': 0.6, 'w': 0.6,
    '<3': 1.0, 'ayaya': 0.8, 'heyguys': 0.6, 'seemsgood': 0.7,
    'kappapride': 0.8, 'feelsgoodman': 1.0, 'bloodtrail': 0.7, 'blessrng': 0.5,
    '\U0001f62d': -0.6, '\U0001f622': -0.7, 'biblethump': -0.8, 'sadge': -0.8,
    'peeposad': -0.8, 'feelsbadman': -0.8,
    '\U0001f621': -0.8, '\U0001f92c': -0.9, 'babyrage': -0.8, 'rage': -0.8,
    'malting': -0.7,
    'wutface': -1.2, 'notlikethis': -1.0, 'residentsleeper': -1.0,
    'coolstorybob': -0.8, 'wtf': -0.8, 'cringe': -0.9, 'fail': -0.7,
    'kappa': -0.3,
}

# --- load messages (JSON lines) ---
msgs = []  # (offset_sec, text)
with open(src, encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line or line in ('[', ']'):
            continue
        if line.endswith(','):
            line = line[:-1]
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        r = entry.get('replayChatItemAction') or {}
        t = int(r.get('videoOffsetTimeMsec') or 0) / 1000.0
        for a in r.get('actions') or []:
            item = (a.get('addChatItemAction') or {}).get('item') or {}
            rend = (item.get('liveChatTextMessageRenderer')
                    or item.get('liveChatPaidMessageRenderer'))
            if not rend:
                continue
            text = ''.join(run.get('text', '')
                           for run in (rend.get('message') or {}).get('runs') or []
                           ).strip()
            if text:
                msgs.append((t, text))

# --- bins ---
bin_size = dur_s / NUM_BINS
bins = [{'count': 0, 'sent_sum': 0.0, 'sent_n': 0, 'texts': []} for _ in range(NUM_BINS)]
for t, text in msgs:
    i = min(NUM_BINS - 1, int(t // bin_size))
    b = bins[i]
    b['count'] += 1
    b['texts'].append(text)
    sent, matches = 0.0, 0
    for tok in text.split():
        low = tok.lower()
        if low in BASE_EMOTE_SENTIMENT:
            sent += BASE_EMOTE_SENTIMENT[low]
            matches += 1
    if matches:
        b['sent_sum'] += sent / matches
        b['sent_n'] += 1

factor = 60.0 / bin_size
rate = [b['count'] * factor for b in bins]               # msgs/min
sentiment = [b['sent_sum'] / b['sent_n'] if b['sent_n'] else 0.0 for b in bins]
total = len(msgs)
avg_rate = total / (dur_s / 60.0)

# --- local maxima -> peaks (userscript rules) ---
peaks = []
for i in range(WINDOW, NUM_BINS - WINDOW):
    cur = rate[i]
    if cur <= 0:
        continue
    if all(rate[i + w] < cur for w in range(-WINDOW, WINDOW + 1) if w != 0):
        if cur > avg_rate * AVG_MULT:
            laughs = sum(
                1 for tx in bins[i]['texts']
                if any(a in tx.lower() for a in SENTIMENT_ANCHORS['laughter']))
            lr = laughs / bins[i]['count'] if bins[i]['count'] else 0
            if lr > 0.25:
                cat = 'funny'
            elif sentiment[i] > 0.4:
                cat = 'hype'
            elif sentiment[i] < -0.3:
                cat = 'fail'
            else:
                cat = 'general'
            peaks.append({'bin': i, 'time': (i + 0.5) * bin_size,
                          'rate': cur, 'sent': sentiment[i],
                          'laugh_ratio': lr, 'cat': cat})

peaks.sort(key=lambda p: -p['rate'])
selected = []
for p in peaks:                                    # NMS 90s, top 5 (default)
    if any(abs(p['time'] - s['time']) < MIN_PEAK_SPACING_SEC for s in selected):
        continue
    selected.append(p)
    if len(selected) == 5:
        break
extended = []
for p in peaks:                                    # extended top-20 for comparison
    if any(abs(p['time'] - s['time']) < MIN_PEAK_SPACING_SEC for s in extended):
        continue
    extended.append(p)
    if len(extended) == 20:
        break


def fmt(t):
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}"


with open(out, 'w', encoding='utf-8') as f:
    f.write(f"VOD Highlight Analyzer v4.0 algorithm, ported\n")
    f.write(f"messages: {total}   duration: {dur_s:.0f}s   bins: {NUM_BINS} x "
            f"{bin_size:.1f}s\n")
    f.write(f"average rate: {avg_rate:.1f} msgs/min   threshold: "
            f"{avg_rate * AVG_MULT:.1f}   peak bin rate: {max(rate):.1f}\n\n")
    f.write("== TOP 5 SPIKES (script default settings, intro NOT skipped) ==\n")
    for p in sorted(selected, key=lambda p: p['time']):
        f.write(f"[{fmt(p['time'])}]  {p['rate']:.0f} msgs/min  {p['cat']}  "
                f"(laughter {p['laugh_ratio']:.2f}, sentiment {p['sent']:+.2f})\n")
    f.write("\n== EXTENDED TOP 20 (same algorithm, NMS 90s) ==\n")
    for p in extended:
        f.write(f"[{fmt(p['time'])}]  {p['rate']:6.0f} msgs/min  {p['cat']:<7} "
                f"laugh {p['laugh_ratio']:.2f} sent {p['sent']:+.2f}\n")
print(f"{out}: done — avg {avg_rate:.1f}/min, {len(peaks)} candidate peaks")
