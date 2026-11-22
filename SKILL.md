# Skill: Preprocess

Run this whole workflow when the user supplies a YouTube stream/VOD link and asks to **preprocess** it.

## Configuration

Two environment variables must be set before running this skill (see the repo README):

- `PREPROCESS_ROOT` — the working root where `streams\` and `capcut\` live (e.g. `J:\Faygo`).
- `PREPROCESS_REPO` — the checkout of this repo (e.g. `J:\Faygo\Alsafa.preload`).

In the commands below, `$Root` and `$Repo` refer to those variables:

```powershell
$Root = $env:PREPROCESS_ROOT
$Repo = $env:PREPROCESS_REPO
```

Do not ask for clarification on routine choices; use the conventions below.

## 0. Setup

Extract the 11-character video ID from the link. Create a dedicated stream folder:

```powershell
$StreamDir = Join-Path $Root "streams\<streamer>_<video_id>"
New-Item -ItemType Directory -Force -Path $StreamDir | Out-Null
```

`<streamer>` is a short lowercase name (e.g. `saba`, `kiara`, `raora`, `nimi`, `bijou`, `miko`). Keep the video ID in every generated filename — several streams share the same title.

Never create chunks by re-downloading. If a segment is needed later, cut it from the downloaded original with `ffmpeg -c copy`.

## 1. Download the stream with yt-dlp

Download the best H.264 1080p60 video plus best audio, merged to MKV, into the stream folder. Use a filename that includes `%(id)s`:

```powershell
python -m yt_dlp -f "(299/bv*[height=1080][fps>=55][vcodec^=avc1])+(140/ba)" --merge-output-format mkv -o "$StreamDir\%(title)s [%(id)s].%(ext)s" "<URL>"
```

If the combined format fails with `HTTP Error 401`, download video and audio separately (video with `--concurrent-fragments 4`, audio single-threaded) and mux with `ffmpeg -i video -i audio -map 0:v -map 1:a -c copy`. Verify the result with ffprobe: H.264, 1920x1080, 60 fps, plus an audio stream.

While downloading, also fetch the two analysis inputs into the same folder:

```powershell
python -m yt_dlp --write-auto-subs --sub-format json3 --skip-download -o "$StreamDir\%(id)s.%(ext)s" "<URL>"
python -m yt_dlp --write-subs --sub-langs live_chat --skip-download -o "$StreamDir\%(id)s.%(ext)s" "<URL>"
```

These produce `cc_<id>.en.json3` and `<id>.live_chat.json` (rename to `<id>.live_chat.live_chat.json` if the older doubled convention is needed). Also fetch VOD comments for the crowd-sourced timestamp table (writes `cjson_<id>.info.json` — `--dump-single-json` prints to stdout and never creates the file):

```powershell
python -m yt_dlp --skip-download --write-info-json --write-comments -o "$StreamDir\cjson_%(id)s.%(ext)s" "<URL>"
```

A 429 on captions/comments is usually transient — retry.

Turn the comment dump into a readable digest with:

```powershell
python "$Repo\scripts\parse_timestamped_comments.py" "$StreamDir\cjson_<id>.info.json" "$StreamDir\timestamped_comments_<id>.txt"
```

## 2. Analyze peak chat and create transcript v2.md

Build the timestamped transcript and natural cut points:

```powershell
python "$Repo\scripts\build_transcript2.py" "$StreamDir\cc_<id>.en.json3" "$StreamDir\transcript_<id>_en.txt" "$StreamDir\cutpoints_<id>_en.txt" "$StreamDir\major_breaks_<id>_en.txt" en
```

Analyze the live-chat replay for hype peaks:

```powershell
python "$Repo\scripts\analyze_live_chat.py" "$StreamDir\<id>.live_chat.live_chat.json" "$StreamDir\chat_peaks_digest.txt"
```

Cross-check with the VOD Highlight Analyzer port. Get the duration from the downloaded master first:

```powershell
$Video = Get-ChildItem $StreamDir -Include *.mkv,*.mp4 -Recurse | Sort-Object Length -Descending | Select-Object -First 1
$dur = & ffprobe -v error -show_entries format=duration -of csv=p=0 $Video.FullName
python "$Repo\scripts\vod_highlight_validator.py" "$StreamDir\<id>.live_chat.live_chat.json" $dur "$StreamDir\vod_analyzer_validation.txt"
```

Ignore intro/outro spam and greeting floods. Cross-check chat peaks against VOD timestamp comments and liked comments.

Now write the file **`transcript v2.md`** (exact name — the planning script looks for this filename, with a space) in the stream folder. It must contain:

- Header: title, channel, URL, length, and a note describing the v2 method.
- `## Early stream / intro`, `## Mid stream`, `## Late stream` tables with columns `Time | Moment`. Bold the most-liked moments.
- A `## Chat-rate analysis` section documenting the peak-finding method, threshold, stream average, and a top-peaks table.
- A `### Verdict — where to cut` table with columns `Tier | Time | Moment`. Use explicit ranges (`H:MM:SS–H:MM:SS`) for confirmed clips. Mark rejected moments with tier `—` so the planner skips them.
- Optional `### Script-only finds` section with a list of extra timestamps.
- A short bottom-line summary naming the top cut targets.

Times may be `M:SS` or `H:MM:SS`; ranges are supported. Every row in a `Time | Moment` table becomes a candidate clip.

### Optional: game-dialogue filter (walkthrough match)

Label each VOD transcript line as game (NPC) dialogue or streamer speech by matching it against the game's own dialogue script, then omit game lines from the final subtitles. One-time per game, build the script from a no-commentary walkthrough (audio-only pull of a 36 h walkthrough is ~750 MB; `--start/--end` cut just a mission window):

```powershell
python -m pip install faster-whisper
python "$Repo\scripts\whisper_lines.py" "https://youtu.be/<walkthrough>" "$StreamDir\game_script_<game>.json" --lang en
```

Per stream, label the VOD lines (accepts the YouTube `cc_<id>.en.json3`, or a `whisper_lines.py` JSON of the VOD audio for when auto-subs are unusable — Japanese/Indonesian streams):

```powershell
python "$Repo\scripts\label_speakers.py" "$StreamDir\cc_<id>.en.json3" "$StreamDir\game_script_<game>.json" "$StreamDir\vod_labeled_<id>.json" --drop --srt "$StreamDir\streamer_<id>.srt"
```

Matched lines are game/NPC dialogue: omit them (with `--drop`) or style them separately. Unmatched lines are the streamer: never drop on no-match — open-world lines the walkthrough never triggered must stay visible. A streamer echoing an NPC line mislabels as game; fix rare cases by hand. If game and streamer speak different languages, whisper's per-line language is usually the speaker label on its own; the walkthrough match then just confirms it. When present, use the labeled transcript while writing `transcript v2.md`.

## 3. Cut clips and put them in a new CapCut folder

Generate the clip plan from `transcript v2.md`:

```powershell
python "$Repo\scripts\plan_clips_v2.py" $StreamDir
```

This reads `transcript v2.md` plus the first `cutpoints_*.txt`, probes the largest MKV/MP4 in the folder, snaps boundaries to silence cut points, merges moments closer than 15 s, drops overlapping clips, and writes `clips_plan.json`.

Create a **new folder** for this stream's CapCut clips (e.g. `Join-Path $Root "capcut\<streamer>_<id>"`), then cut the physical clips there from the original with `ffmpeg -c copy` (stream copy — no re-encode) using the start/end values in `clips_plan.json`. Do not re-download any source material for this.

If building an actual CapCut draft, use the existing script (CapCut must be fully closed):

```powershell
python "$Repo\scripts\make_multi_clip_draft.py" $StreamDir --name "<draft name>"
```

It uses the vendored `capcutlib.py` at the repo root and clones the `template` draft, laying out one segment per plan entry.

## Done criteria

Report back with: the downloaded MKV path and size, the `transcript v2.md` path, the number of clips and total clip duration, and the CapCut folder/draft path. Confirm video streams are 1920x1080 60 fps with audio.
