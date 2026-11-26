# Alsafa.preload

One repository for the whole CapCut clip pipeline: preprocess a YouTube
stream/VOD (download → transcript/chat analysis → clip plan → physical cuts →
CapCut draft) and finish the edit through the bundled **CapCut MCP server**.

The OpenCode **`preprocess`** skill drives the analysis half; `server.py` exposes
the editing half (projects, timeline, clips, captions/titles, keyframes,
effects, preview and export). Both use the same `capcutlib.py`, so a draft built
by the skill and a draft built by the MCP are identical.

This repository contains **code only**. Downloaded streams, captions, chat logs,
transcripts, clip plans and CapCut drafts are generated locally and are
intentionally gitignored.

## Contents

- `SKILL.md` — the OpenCode skill definition (portable paths via environment variables).
- `scripts/` — yt-dlp/ffmpeg-adjacent Python tools invoked by the skill.
  `whisper_lines.py` + `label_speakers.py` additionally label VOD transcript
  lines as game (NPC) dialogue or streamer speech by matching against a dialogue
  script built once per game from a no-commentary walkthrough
  (`pip install faster-whisper`), so final subtitles can omit game dialogue.
- `capcutlib.py` + `subs.py` — the CapCut draft library and its local dependency.
- `server.py` + `editor.py` + `render.py` + `catalogue.py/json` — the bundled
  CapCut MCP server (formerly the separate `capcut-mcp` project).
- `CAPCUT_MCP.md` — full tool reference, safety rules and limits for the MCP.
- `install-skill.ps1` — copies `SKILL.md` into the local OpenCode skills folder.
- `tests/` — offline unit tests with generated fixtures; no network access required.
- `selftest.py` / `selftest_editor.py` — end-to-end checks for the draft builder
  and editor (need `ffmpeg`, and the editor self-test needs a real CapCut draft).
- `smoketest_client.py` / `smoketest_editor_client.py` — spawn the real MCP
  server over stdio and exercise the tool surface.

## Prerequisites

- Python 3.10+ and `pip`.
- `yt-dlp` (`pip install -r requirements.txt`).
- `ffmpeg` / `ffprobe` on `PATH`.
- CapCut desktop, fully closed, with:
  - a draft named `template` (one video clip; optional text clip), and
  - one multi-clip project CapCut itself created (two or more clips with audio)
    — the schema donor used by `make_multi_clip_draft`.

## Install / refresh

```powershell
git clone https://github.com/dalhan2/Alsafa.preload.git
cd Alsafa.preload
python -m pip install -r requirements.txt
```

Set two user environment variables (persisted for future sessions):

```powershell
[Environment]::SetEnvironmentVariable('PREPROCESS_ROOT', 'J:\Faygo', 'User')
[Environment]::SetEnvironmentVariable('PREPROCESS_REPO', (Get-Location).Path, 'User')
```

`PREPROCESS_ROOT` is where `streams\` and `capcut\` live. `PREPROCESS_REPO` is
this checkout. Open a new terminal after setting them.

Install or refresh the skill (copy-on-install):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-skill.ps1 -SetEnv
```

`-SetEnv` persists `PREPROCESS_REPO` (this checkout) and `PREPROCESS_ROOT` (its
parent folder), so you can skip the manual `SetEnvironmentVariable` step above.
On machines where script execution is allowed, `.\install-skill.ps1 -SetEnv`
works directly. The `-ExecutionPolicy Bypass` form is needed on Windows machines
that block unsigned scripts, like the original development machine.

The installer copies `SKILL.md` to
`$env:USERPROFILE\.config\opencode\skills\preprocess\SKILL.md`. The local copy is
a generated artifact — edit the repo copy, then re-run the installer to sync.
Use `-SkillDir` to install somewhere else.

### Who needs the installer?

| Person | Needs the installer? |
| --- | --- |
| Using the repo through OpenCode | **Yes** — this is how OpenCode finds the skill |
| Updating `SKILL.md` in the repo | **Yes** — re-run to refresh the local copy |
| Just running the Python scripts directly | No — call `python scripts/...` yourself |

You can skip this step entirely if you only want to run the scripts manually.

## Register the CapCut MCP server

Add this server to `%USERPROFILE%\.config\opencode\opencode.json` (or the
project config). Use the Python environment where `requirements.txt` is
installed; the example assumes a `.venv` in this checkout.

```jsonc
{
  "mcp": {
    "servers": {
      "capcut": {
        "type": "local",
        "command": [
          "J:\\Faygo\\Alsafa.preload\\.venv\\Scripts\\python.exe",
          "J:\\Faygo\\Alsafa.preload\\server.py"
        ],
        "cwd": "J:\\Faygo\\Alsafa.preload"
      }
    }
  }
}
```

Config auto-reloads; tools appear as `capcut_<name>`. Restart OpenCode after
changing `server.py`. See `CAPCUT_MCP.md` for every tool.

## Workflow

Give OpenCode a YouTube link and ask it to **preprocess** the stream. The skill runs:

1. `yt-dlp` → best H.264 1080p60 video + best audio, merged to MKV, plus
   auto-subs (`json3`), live chat and VOD comments, into
   `$env:PREPROCESS_ROOT\streams\<streamer>_<id>\`.
2. `build_transcript2.py`, `analyze_live_chat.py`, `vod_highlight_validator.py`
   and `parse_timestamped_comments.py` → analysis digests. You then write
   `transcript v2.md` in the stream folder.
3. `plan_clips_v2.py` → `clips_plan.json`; cut the physical clips with
   `ffmpeg -c copy` into a new folder; `make_multi_clip_draft.py` builds a
   CapCut draft from the plan (same builder as the MCP).
4. Finish the draft through the `capcut_*` MCP tools: `project_open` /
   `timeline_read`, `captions_add`, `title_add`, `clip_update`,
   `preview_frame`, then `export_start` — or open CapCut and polish by hand.

See `SKILL.md` for exact commands and the required shape of `transcript v2.md`.

## Tests

Offline workflow tests:

```powershell
python -m unittest discover -s tests -v
```

CapCut builder/editor checks (synthetic drafts; editor self-test needs a real
CapCut draft as schema donor and a 3-second test video made by `ffmpeg`):

```powershell
python selftest.py
python selftest_editor.py
python smoketest_client.py
python smoketest_editor_client.py
```

## Keeping the skill in sync

After changing `SKILL.md` or any script:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-skill.ps1
git add . && git commit -m "update preprocess tooling"
git push
```

## License

MIT — see `LICENSE`. The toolchain is provided as-is; you are responsible for
complying with YouTube's terms and for the copyright/privacy status of any
content you download or analyse.
