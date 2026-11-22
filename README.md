# Alsafa.preload

Tooling for the OpenCode **`preprocess`** skill: download a YouTube stream/VOD, analyse live-chat hype peaks, build a `transcript v2.md`, and generate a clip plan for CapCut.

This repository contains **code only**. Downloaded streams, captions, chat logs, transcripts, clip plans and CapCut drafts are generated locally and are intentionally gitignored.

## Contents

- `SKILL.md` — the OpenCode skill definition ( portable paths via environment variables ).
- `scripts/` — yt-dlp/ffmpeg-adjacent Python tools invoked by the skill.
- `capcutlib.py` + `subs.py` — vendored copies of the CapCut draft library and its one local dependency, used by `make_multi_clip_draft.py`.
- `install-skill.ps1` — copies `SKILL.md` into the local OpenCode skills folder (copy-on-install).
- `tests/` — offline unit tests with generated fixtures; no network access required.

## Prerequisites

- Python 3.10+ and `pip`.
- `yt-dlp` (`pip install -r requirements.txt`).
- `ffmpeg` / `ffprobe` on `PATH`.
- Optional, only for step 3: CapCut desktop, fully closed, with a draft named `template`.

## Install

```powershell
git clone https://github.com/<owner>/Alsafa.preload.git
cd Alsafa.preload
python -m pip install -r requirements.txt
```

Set two user environment variables (persisted for future sessions):

```powershell
[Environment]::SetEnvironmentVariable('PREPROCESS_ROOT', 'J:\Faygo', 'User')
[Environment]::SetEnvironmentVariable('PREPROCESS_REPO', (Get-Location).Path, 'User')
```

`PREPROCESS_ROOT` is where `streams\` and `capcut\` live. `PREPROCESS_REPO` is this checkout. Open a new terminal after setting them.

Install or refresh the skill (copy-on-install):

```powershell
.\install-skill.ps1
```

This copies `SKILL.md` to `$env:USERPROFILE\.config\opencode\skills\preprocess\SKILL.md`. The local copy is a generated artifact — edit the repo copy, then re-run the installer. Use `-SkillDir` to install somewhere else.

## Workflow

Give OpenCode a YouTube link and ask it to **preprocess** the stream. The skill runs:

1. `yt-dlp` → best H.264 1080p60 video + best audio, merged to MKV, plus auto-subs (`json3`), live chat and VOD comments, into `$env:PREPROCESS_ROOT\streams\<streamer>_<id>\`.
2. `build_transcript2.py`, `analyze_live_chat.py`, `vod_highlight_validator.py` and `parse_timestamped_comments.py` → analysis digests. You then write `transcript v2.md` in the stream folder.
3. `plan_clips_v2.py` → `clips_plan.json`; cut the physical clips with `ffmpeg -c copy` into a new folder; optionally `make_multi_clip_draft.py` builds a CapCut draft.

See `SKILL.md` for exact commands and the required shape of `transcript v2.md`.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Tests use temporary synthetic JSON3/live-chat/Markdown fixtures and `plan_clips_v2.py --duration`, so they need neither network access nor `ffmpeg`.

## Keeping the skill in sync

After changing `SKILL.md` or any script:

```powershell
.\install-skill.ps1
git add . && git commit -m "update preprocess tooling"
git push
```

## License

MIT — see `LICENSE`. The toolchain is provided as-is; you are responsible for complying with YouTube's terms and for the copyright/privacy status of any content you download or analyse.
