# CapCut MCP (bundled draft builder + agentic editor)

Local MCP server for **CapCut desktop**. It builds drafts from clip picks and
exposes the editing surface an agent needs to finish a cut: projects, media,
timeline lanes, clip moves/splits/trims, speed, transforms, crop, masks,
keyframes, captions/titles, effects/transitions, undo/redo, preview frames and
an ffmpeg render.

This is the updated `capcut-mcp`, now shipped **inside Alsafa.preload**. The
preprocess builder (`scripts/make_multi_clip_draft.py`) and the MCP server share
the same `capcutlib.py`, so both paths produce identical drafts. Run
`server.py` from this repository; the workflow scripts and the editor are one
codebase.

CapCut has no public API. Like every working community tool, this server writes
CapCut's local draft files (`draft_content.json`, `draft_meta_info.json`,
`root_meta_info.json`) in:

```
%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft
```

**CapCut must be closed while the server writes.** Its autosave overwrites
agent edits.

## The one-time template

CapCut 9.x rejects drafts built from stale schemas. The server clones the exact
JSON shapes of projects **your** CapCut build made itself, so:

1. Open CapCut → **New project**, name it **`template`**.
2. Add **any video clip** (a few seconds is enough).
3. Optional: add **one text clip** and style it the way you want captions.
   If the template has no text clip, the server borrows a text style from any
   other real draft (and falls back to Arial).
4. Save, go back to the project list, and **close CapCut fully**.

Then make **one multi-clip donor**: a project CapCut itself created with at
least two video clips + their audio, saved and closed. `make_multi_clip_draft`
clones that project because a one-clip template cannot express per-clip speed,
canvas, sound and audio-companion materials. Set `CAPCUT_DONOR` to a folder name
to choose one explicitly; otherwise the server picks the richest donor.

`doctor` confirms `template_ready` and `captions_ready`. The template and donor
are never modified. Any existing multi-clip project is also used as a richer
schema donor (tracks, audio companions, effects, animations).

## Register with OpenCode

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
changing `server.py` so the new tool list is picked up.

## Quick start

```python
# create a 1920x1080 project in the CapCut drafts root
project_create(location="", name="my_edit",
               video={"width": 1920, "height": 1080,
                      "rateNum": 30, "rateDen": 1})

# import and place media
media_import(path="<project>", file=r"J:\path\clip.mp4")
clip_add(path="<project>", mediaId="<id>", trackId="", start=0)

# captions and title
captions_add(path="<project>", captions=[
    {"start": 0.2, "end": 1.4, "text": "First line"},
    {"start": 1.5, "end": 2.8, "text": "Second line"},
], offsetY=0.35)
title_add(path="<project>", start=0.1, duration=2.0,
          style={"content": "MY TITLE", "fontSize": 0.11,
                 "color": "#ffcc00", "strokeWidth": 0.01})

# polish, save and review
clip_trim(path="<project>", clipId="<id>", edge="end", delta=-0.3, ripple=True)
clip_update(path="<project>", clipId="<id>",
            patch={"opacity": 0.9, "fadeIn": 0.2,
                   "transitionIn": {"id": "dissolve", "duration": 0.4}})
project_save(path="<project>")
preview_frame(path="<project>", time=1.0)      # returns the PNG
export_start(path="<project>", output=r"J:\out.mp4")
```

The same work can be one atomic step:

```python
edit_apply(path="<project>", command={"op": "batch", "commands": [
    {"op": "addClip", "mediaId": "<id>", "trackId": "<track>", "start": 0},
    {"op": "addCaptions", "captions": [{"start": 0, "end": 1, "text": "Hi"}]},
]})
edit_undo(path="<project>")
edit_redo(path="<project>")
```

## Tools

### Build (original pipeline)

| Tool | Purpose |
|---|---|
| `doctor` | CapCut version, drafts folder, template/caption readiness, editor capabilities |
| `list_drafts` | List projects in the drafts folder |
| `read_draft` | Compact timeline, media and captions for a draft |
| `make_clip_draft` | Trimmed video + optional subs/title → CapCut draft |
| `make_multi_clip_draft` | `clips_plan.json` → one draft with many segments |
| `import_subs` | Add/replace a caption track on an existing draft |
| `delete_draft` | Move a draft to `.mcp_trash` (`confirm=true`) |

### Projects and timelines

| Tool | Purpose |
|---|---|
| `capcut_version` | API/build, CapCut version, catalogue counts and notes |
| `project_create` | New project from the installed build's schema |
| `project_open` / `project_get` | Load/inspect a project (full JSON + summary) |
| `project_save` / `project_close` | Validate and write / close (save optional) |
| `project_list` | Projects with frame, duration and open state |
| `project_set_video` | Canvas size, frame rate, background colour |
| `timeline_read` | Frame, tracks top-to-bottom, clips, media bin, captions |
| `timeline_background` | `none` / `color` / `blur` / `image` behind pictures |
| `timeline_linkage` | Magnetic timeline on/off |
| `track_add` | Add a video/audio/text/effect lane |

### Media and clips

| Tool | Purpose |
|---|---|
| `media_import` / `media_probe` | Add to the bin / probe without changes |
| `clip_add` | Place a bin item at `start` on a lane |
| `clips_add_sequence` | Lay several items end to end in one step |
| `clips_move` / `clips_remove` | Move or delete (with optional ripple) |
| `clips_split` | Cut clips at a timeline instant |
| `clip_trim` | Move one edge by signed delta seconds |
| `clip_speed` | 0.0625–16×, source stretch preserved |
| `clip_frame` | Fit or fill the frame |
| `clip_transform` | Offset, rotation, scale, stretch |
| `clip_update` | Volume, fades, opacity, flips, crop, blend, effects, filters, transition, text, masks, background, motion, mute, audio stream |
| `clip_masks` | Shape masks (circle/rectangle/linear/mirror/heart/star) |
| `clip_stabilize` | Stabilization settings (CapCut applies the engine) |

### Text, keyframes, effects

| Tool | Purpose |
|---|---|
| `captions_add` | One undoable run of captions, shared style + vertical offset |
| `title_add` | Title with style, offset, duration and lane choice |
| `title_style_words` / `title_clear_word_styles` | Per-character style runs |
| `keyframe_set` / `keyframe_clear` | Scale/position/rotation/stretch/opacity/volume |
| `mask_key_set` | Animate mask geometry (editor metadata + preview) |
| `effect_key_set` | Animate an effect parameter |
| `effect_layer_add` | Global effect layer (or audio effect with `sound=true`) |
| `effects_list` | CapCut effect/transition/animation/mask catalogue |

### Review, export and history

| Tool | Purpose |
|---|---|
| `preview_frame` | Composited frame at `time` (Pillow); image or PNG path |
| `export_start` / `export_status` / `export_wait` / `export_cancel` | ffmpeg render + job control |
| `edit_apply` | Any typed edit, or a batch, as one undoable step |
| `edit_undo` / `edit_redo` | History (in memory for the open project) |
| `template_list` / `template_instantiate` | Project templates with fill slots |

## Effects catalogue

`catalogue.json` is a snapshot of the CapCut namespace from
[`capcut-cli`](https://github.com/renezander030/capcut-cli)'s `enums.json`:
116 transitions, 345 scene effects, 95 character effects, 15 audio effects,
9 masks, plus image/text intro/outro/loop animations. `effects_list(kind=...)`
returns them; `effect_layer_add`, `clip_update.effects/filters` and
`clip_update.transitionIn` resolve their `effect_id`/`resource_id` from it.
CapCut still downloads/resolves the actual package from its own cache.

## Safety

- **CapCut must be closed** while writing. Every mutating tool checks.
- Every write is preceded by a backup in `.mcp_backups/` (last 40 kept per file).
- Drafts are validated first: unique IDs, no same-track overlaps, material refs
  resolve, media exists. A failed build deletes its folder.
- Writes are atomic (`tmp` + `os.replace`).
- Deletes move the folder to `.mcp_trash/`, never hard-delete.
- A project is only written to disk on `project_save` / `project_close`.
- `edit_apply` batches and every typed tool are one undo step; undo snapshots
  are compressed in memory for the open project.
- New projects keep the donor's `draft_content.id`, `meta.draft_id` and
  `timeline_layout` ids; regenerating them breaks CapCut's timeline binding.

## Limits

- **CapCut is the exact renderer.** `preview_frame` and `export_start` use
  ffmpeg. Video layers, transforms, crop, captions, titles, opacity, fades and
  speed are rendered; effects, masks and keyframes are approximated (masks and
  motion keyframes are previewed, but not applied by the ffmpeg exporter).
- **CapCut has one mask per clip.** `clip_masks` applies the first and keeps
  extras as editor metadata (`limited: true`).
- **Mask/effect parameter keyframes** are stored as editor metadata and are
  previewed, but the CapCut desktop build may not animate them on its own.
- **Stabilization** writes the native `stable` flags plus editor metadata;
  CapCut's stabilization engine runs when it previews/exports.
- **Blend modes** map to CapCut's mix-mode resources; unknown names are stored
  but may not resolve.
- **Shape clips** (`clip_update.shape`) are stored as editor metadata; use a
  CapCut shape/sticker if you need a real shape material.
- **Audio stream choice** (`clip_update.audioStream`) is stored for the editor
  and exporter but does not change CapCut's stream picker.
- **Templates** are CapCut draft folders plus optional `mcp_template.json`
  with `slots`. The server also treats drafts whose names contain "template"
  as templates.
- Draft format is unofficial; a CapCut update can break it. Template cloning +
  backups minimize the damage.

## Development

```powershell
J:\Faygo\Alsafa.preload\.venv\Scripts\python.exe J:\Faygo\Alsafa.preload\selftest.py
J:\Faygo\Alsafa.preload\.venv\Scripts\python.exe J:\Faygo\Alsafa.preload\selftest_editor.py
J:\Faygo\Alsafa.preload\.venv\Scripts\python.exe J:\Faygo\Alsafa.preload\smoketest_client.py
J:\Faygo\Alsafa.preload\.venv\Scripts\python.exe J:\Faygo\Alsafa.preload\smoketest_editor_client.py
```

The smoke clients spawn the real server over stdio. They need a local CapCut
draft and a sample clip; override with `CAPCUT_SMOKE_SEED`,
`CAPCUT_SMOKE_VIDEO` and `CAPCUT_PYTHON` if the defaults do not fit.

`CAPCUT_MCP_ALLOW_RUNNING=1` bypasses the "CapCut is running" guard; use it
only with a temporary `CAPCUT_DRAFTS_ROOT` (the smoke client does).

`selftest.py` covers the original builder. `selftest_editor.py` creates a
synthetic project from a real schema donor, then exercises media import, clips,
captions, titles, split, trim, speed, transform, crop, transition, masks,
keyframes, effects, undo/redo, save/reopen, preview and export. The smoke tests
spawn the real MCP server over stdio.

Rebuild the catalogue with:

```powershell
python scripts\build_catalogue.py path\to\enums.json
```
