#!/usr/bin/env python3
"""Build a clip plan from a stream's "transcript v2.md".

Reads, in the same file:
  * timestamp tables (Early / Mid / Late / Crowd-sourced ...): Time | Moment
  * the "Verdict - where to cut" table (Tier | Time | [Chunk] | Moment)
  * the "Script-only finds" time list (when present)

Rules (same convention everywhere):
  * moments < 15 s apart merge into one group
  * explicit ranges are used as-is, snapped outward to a silence cut point
  * single moments start at the latest cut point just before, end at the first
    cut point leaving >= 10 s; without cut points, t-5 .. t+5
  * remaining overlapping clips merge
  * rows marked excluded / skipped (tier "-") are dropped

Usage: plan_clips_v2.py <stream_dir> [--out clips_plan.json] [--duration SEC]
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TIME_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")


def sec(t):
    p = [int(x) for x in t.split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]


def fmt(s):
    s = int(round(s))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def slug(text, n=42):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:n].strip("_")


def norm_cell(cell):
    out = []
    for part in cell.replace("**", "").split("/"):
        times = TIME_RE.findall(part)
        if len(times) >= 2:
            out.append((sec(times[0]), sec(times[1])))
        elif times:
            out.append((sec(times[0]), sec(times[0])))
    return out


def table_rows(lines, heading_match):
    rows, active = [], False
    for line in lines:
        if line.startswith("## ") or line.startswith("### "):
            if active and not heading_match(line):
                break
            active = heading_match(line)
            continue
        if active and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and not re.fullmatch(r"[-: ]+", cells[0]):
                rows.append(cells)
    return rows


def probe_dur(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stream", type=Path)
    ap.add_argument("--out", default=None)
    ap.add_argument("--duration", type=float, default=None,
                    help="override ffprobe duration (useful for offline tests)")
    a = ap.parse_args()
    sd = a.stream

    transcript = sd / "transcript v2.md"
    lines = transcript.read_text(encoding="utf-8").splitlines()
    cuts_file = next(iter(sorted(sd.glob("cutpoints_*.txt"))), None)
    cuts = []
    if cuts_file:
        cuts = sorted(sec(m.group(1)) for m in
                      (re.match(r"\s*CUT at (\d{2}:\d{2}:\d{2})", ln) for ln in
                       cuts_file.read_text(encoding="utf-8").splitlines()) if m)
    videos = [p for p in sd.iterdir() if p.suffix.lower() in (".mkv", ".mp4")]
    video = max(videos, key=lambda p: p.stat().st_size) if videos else None
    if a.duration is not None:
        dur = a.duration
    elif video is not None:
        dur = probe_dur(video)
    else:
        sys.exit("no video found in stream dir; pass --duration")

    groups = []

    # 1. two-column timestamp tables (Early/Mid/Late/Crowd-sourced)
    for cells in table_rows(lines, lambda ln: ln.lower().startswith("## ")):
        if len(cells) != 2 or cells[0].lower() == "time":
            continue
        if re.search(r"exclu|skip", cells[1], re.I):
            continue
        for a_, b_ in norm_cell(cells[0]):
            groups.append({"a": a_, "b": b_, "labels": [cells[1]], "explicit": b_ > a_})

    # 2. verdict table
    for cells in table_rows(lines, lambda ln: ln.lower().startswith("### verdict")):
        if len(cells) < 3 or cells[1].lower() == "time":
            continue
        if cells[0].strip() in ("—", "-", "–"):
            continue
        times = norm_cell(cells[1])
        parts = [p.strip() for p in cells[-1].split(" / ")]
        labels = parts if len(parts) == len(times) else [cells[-1]] * len(times)
        for (a_, b_), lab in zip(times, labels):
            groups.append({"a": a_, "b": b_, "labels": [lab], "explicit": b_ > a_})

    # 3. script-only finds (read until the next heading, not just 3 lines)
    for i, line in enumerate(lines):
        if "Script-only finds" in line:
            blob = []
            for ln in lines[i + 1:]:
                if ln.startswith("#"):
                    break
                blob.append(ln)
            for t in TIME_RE.findall(" ".join(blob)):
                groups.append({"a": sec(t), "b": sec(t),
                               "labels": [f"script-only find {fmt(sec(t))}"],
                               "explicit": False})
            break

    # dedupe identical spans
    uniq = {}
    for g in groups:
        key = (g["a"], g["b"])
        if key in uniq:
            if g["labels"][0] not in uniq[key]["labels"]:
                uniq[key]["labels"] += g["labels"]
        else:
            uniq[key] = g
    groups = sorted(uniq.values(), key=lambda g: g["a"])

    # merge < 15 s apart
    merged = []
    for g in groups:
        if merged and g["a"] - merged[-1]["b"] <= 15:
            m = merged[-1]
            m["b"] = max(m["b"], g["b"])
            m["explicit"] = m["explicit"] or g["explicit"]
            m["labels"] += g["labels"]
        else:
            merged.append(dict(g))

    def latest_cut(t, window):
        cand = [c for c in cuts if c <= t and t - c <= window]
        return cand[-1] if cand else None

    clips = []
    for g in merged:
        a_, b_ = g["a"], g["b"]
        if g["explicit"]:
            start = latest_cut(a_, 8) if cuts else a_
            start = start if start is not None else a_
            end = next((c for c in cuts if c >= b_ and c - b_ <= 8), b_)
            if end - start < 10:
                end = next((c for c in cuts if c >= start + 10), start + 10)
        else:
            start = latest_cut(a_ - 1, 45) if cuts else None
            if start is None:
                start = max(0.0, a_ - 5)
            end = next((c for c in cuts if c >= b_ + 2 and c - start >= 10), None)
            if end is None:
                end = start + 10
        end = min(end, dur)
        if end - start < 10:
            start = max(0.0, end - 10)
        clips.append({"a": start, "b": end, "labels": g["labels"],
                      "moments": [g["a"], g["b"]]})

    out = []
    for c in clips:
        if out and c["a"] <= out[-1]["b"]:
            out[-1]["b"] = max(out[-1]["b"], c["b"])
            out[-1]["labels"] += c["labels"]
            out[-1]["moments"][1] = c["moments"][1]
        else:
            out.append(c)

    plan = []
    for i, c in enumerate(out, 1):
        plan.append({"idx": i, "start": round(c["a"], 3), "end": round(c["b"], 3),
                     "duration": round(c["b"] - c["a"], 3),
                     "labels": c["labels"], "moments": c["moments"],
                     "slug": slug(c["labels"][0]) or "clip"})

    dest = Path(a.out) if a.out else sd / "clips_plan.json"
    dest.write_text(json.dumps(plan, indent=1), encoding="utf-8")
    info = {"stream": sd.name, "video": video.name if video else None, "duration": dur,
            "cutpoints": cuts_file.name if cuts_file else None,
            "clips": len(plan), "total_sec": round(sum(p["duration"] for p in plan), 1),
            "plan": str(dest)}
    print(json.dumps(info))


if __name__ == "__main__":
    main()
