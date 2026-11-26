#!/usr/bin/env python3
"""Whisper ASR to a lines JSON: [{"start", "end", "text"}] (seconds).

One ASR step for both sides of the game-dialogue filter:
  * no-commentary gameplay walkthrough -> game_script.json (one-time per game)
  * stream VOD                        -> vod_lines.json (per stream)

Accepts a local audio file or a http(s) URL; URLs are fetched audio-only with
yt-dlp (format 139/ba/w - ~50 kbps is plenty, whisper resamples to 16 kHz
mono anyway). --start/--end pull only that window of a long walkthrough.
--json3 also writes YouTube-json3 shape so build_transcript2.py runs on
whisper output unchanged.

Optional dependency (not in requirements.txt): python -m pip install faster-whisper

Usage:
  whisper_lines.py <audio-or-url> <out_lines.json> [--lang en] [--model large-v3]
                   [--start H:MM:SS] [--end H:MM:SS] [--json3 out.json3]
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def fetch_audio(url, start, end):
    out = Path(tempfile.mkdtemp(prefix="whisper_lines_"))
    cmd = [sys.executable, "-m", "yt_dlp", "-f", "139/ba/w", "--no-playlist",
           "-o", str(out / "audio.%(ext)s")]
    if start or end:
        window = f"*{start or '0:00:00'}-{end or '24:00:00'}"
        cmd += ["--download-sections", window, "--force-keyframes-at-cuts"]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True)
    files = [p for p in out.iterdir() if p.suffix != ".part"]
    if r.returncode != 0 or not files:
        sys.exit("yt-dlp failed:\n" + (r.stderr or r.stdout)[-500:])
    return out, max(files, key=lambda p: p.stat().st_size)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("out", type=Path)
    ap.add_argument("--lang", default=None,
                    help="pin the language (ja, en, id, ...); default: auto-detect")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--start", default=None, help="HH:MM:SS window start (URL only)")
    ap.add_argument("--end", default=None, help="HH:MM:SS window end (URL only)")
    ap.add_argument("--json3", type=Path, default=None,
                    help="also write YouTube-json3 shape for build_transcript2.py")
    a = ap.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper is required: python -m pip install faster-whisper")

    tmpdir = None
    src = a.audio
    if src.startswith("http://") or src.startswith("https://"):
        tmpdir, f = fetch_audio(src, a.start, a.end)
        src = str(f)

    model = WhisperModel(a.model)
    segs, info = model.transcribe(src, language=a.lang, vad_filter=True)
    lines = [{"start": round(s.start, 3), "end": round(s.end, 3),
              "text": s.text.strip()} for s in segs]
    lines = [l for l in lines if l["text"]]
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)

    a.out.write_text(json.dumps(lines, indent=1, ensure_ascii=False),
                     encoding="utf-8")
    if a.json3:
        events = [{"tStartMs": int(l["start"] * 1000),
                   "dDurMs": int((l["end"] - l["start"]) * 1000),
                   "segs": [{"utf8": l["text"], "tOffsetMs": 0}]}
                  for l in lines]
        a.json3.write_text(json.dumps({"events": events}, ensure_ascii=False),
                           encoding="utf-8")
    print(json.dumps({"lines": len(lines), "lang": info.language,
                      "duration": round(lines[-1]["end"], 1) if lines else 0,
                      "out": str(a.out)}))


if __name__ == "__main__":
    main()
