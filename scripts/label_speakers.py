#!/usr/bin/env python3
"""Label VOD transcript lines as game dialogue or streamer speech.

Reads a game dialogue script (whisper_lines.py run on a no-commentary
walkthrough) and a VOD transcript (YouTube .json3 auto-subs or a
whisper_lines.py lines JSON), then matches every VOD line against the script:

  * normalized exact match first (punctuation/case/space insensitive),
    then difflib ratio >= --threshold inside length buckets (stdlib only)
  * match    -> {"speaker": "game", "game_text": <script line>}
  * no match -> {"speaker": "streamer"}  (NEVER drop on no-match: open-world
    lines the walkthrough never triggered must stay visible)

--drop writes only streamer lines; --srt writes the kept lines as an SRT for
CapCut import (subs.py parses it).

Usage:
  label_speakers.py <vod.json3|.json> <game_script.json> <out.json>
                    [--threshold 0.90] [--drop] [--srt out.srt]
"""
import argparse
import difflib
import json
import re
import sys
from pathlib import Path

# shortcut: text-match only; a streamer echoing an NPC line labels as game -
# add a waveform presence check (cross-correlate VOD vs walkthrough audio at
# the matched timestamps) when that bites.


def norm(t):
    return re.sub(r"[\W_]+", "", t.lower(), flags=re.UNICODE)


def load_vod(path):
    """Lines from a whisper_lines JSON or a YouTube json3 transcript."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and "events" in data:
        lines = []
        for ev in data["events"]:
            t = ev.get("tStartMs", 0) / 1000.0
            dur = (ev.get("dDurMs") or 0) / 1000.0
            text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).strip()
            if text:
                lines.append({"start": t, "end": t + dur, "text": text})
        return lines
    return [l for l in data if l.get("text")]


def match(vod_lines, game_lines, threshold):
    exact = {}
    buckets = {}
    for g in game_lines:
        k = norm(g["text"])
        if not k:
            continue
        exact[k] = g["text"]
        buckets.setdefault(len(k) // 4, []).append((k, g["text"]))

    out = []
    for v in vod_lines:
        k = norm(v["text"])
        g = None
        if k:
            if k in exact:
                g = exact[k]
            else:
                for b in (len(k) // 4 + d for d in (-2, -1, 0, 1, 2)):
                    for gk, gt in buckets.get(b, ()):
                        if abs(len(gk) - len(k)) > 8:
                            continue
                        sm = difflib.SequenceMatcher(None, k, gk)
                        if sm.quick_ratio() < threshold:
                            continue
                        if sm.ratio() >= threshold:
                            g = gt
                            break
                    if g:
                        break
        row = dict(v)
        if g:
            row["speaker"], row["game_text"] = "game", g
        else:
            row["speaker"] = "streamer"
        out.append(row)
    return out


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vod", help="VOD transcript: .json3 or whisper_lines .json")
    ap.add_argument("game", type=Path, help="game_script.json from whisper_lines.py")
    ap.add_argument("out", type=Path)
    ap.add_argument("--threshold", type=float, default=0.90)
    ap.add_argument("--drop", action="store_true",
                    help="write only streamer lines (omit game dialogue)")
    ap.add_argument("--srt", type=Path, default=None,
                    help="also write the kept lines as an SRT")
    a = ap.parse_args()

    vod_lines = load_vod(a.vod)
    game_lines = [l for l in json.loads(a.game.read_text(encoding="utf-8-sig"))
                  if l.get("text")]
    rows = match(vod_lines, game_lines, a.threshold)
    kept = [r for r in rows if r["speaker"] == "streamer"] if a.drop else rows

    a.out.write_text(json.dumps(kept, indent=1, ensure_ascii=False),
                     encoding="utf-8")
    if a.srt:
        cues = [f"{i}\n{srt_time(r['start'])} --> {srt_time(r['end'])}\n{r['text']}\n\n"
                for i, r in enumerate(kept, 1)]
        a.srt.write_text("".join(cues), encoding="utf-8")

    n_game = sum(1 for r in rows if r["speaker"] == "game")
    print(json.dumps({"vod_lines": len(rows), "game": n_game,
                      "streamer": len(rows) - n_game, "kept": len(kept),
                      "out": str(a.out)}))


if __name__ == "__main__":
    main()
