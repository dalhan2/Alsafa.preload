"""CapCut MCP server - build and edit CapCut desktop drafts.

Tools (exposed by OpenCode as capcut_<name>):
  Build: doctor, list_drafts, read_draft, make_clip_draft,
         make_multi_clip_draft, import_subs, delete_draft
  Edit:  capcut_version, project_*, timeline_*, track_add, media_*,
         clip_*, clips_*, captions_add, title_*, keyframe_*, mask_key_set,
         effect_key_set, effect_layer_add, effects_list, preview_frame,
         export_*, edit_apply, edit_undo, edit_redo, template_*

Transport: stdio. Run: <venv-python> server.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capcutlib as cc  # noqa: E402
import editor as ed  # noqa: E402
import render  # noqa: E402
from mcp.server.mcpserver import Image, MCPServer  # noqa: E402

mcp = MCPServer(
    'capcut',
    instructions=(
        'Build and edit CapCut desktop drafts. CapCut must be CLOSED while the '
        'server writes drafts. A template project named "template" (one video clip '
        '+ one text clip) is used to clone the installed build schema; the editor '
        'tools also use any existing multi-clip project as a schema donor. Times '
        'accept HH:MM:SS, MM:SS or seconds unless a tool says otherwise. Use '
        'project_create/media_import/clip_add to build a timeline, captions_add and '
        'title_add for text, clip_update for transforms/effects/transitions, and '
        'preview_frame/export_start to review. CapCut remains the exact renderer.'
    ),
)


# ---------------------------------------------------------------- build tools

@mcp.tool()
def doctor() -> dict:
    """Check CapCut install, drafts folder, template readiness and running state."""
    result = cc.doctor()
    result['editor'] = ed.capcut_version()
    return result


@mcp.tool()
def list_drafts() -> dict:
    """List CapCut projects found in the drafts folder."""
    return {'ok': True, 'drafts': cc.list_drafts()}


@mcp.tool()
def read_draft(name: str) -> dict:
    """Read a draft's timeline: tracks, media (with missing-file flags), captions."""
    return cc.read_draft(name)


@mcp.tool()
def make_clip_draft(name: str, video: str, start: str, end: str,
                    subs: str | None = None, canvas: str = '16:9',
                    title: str | None = None, subs_offset: float = 0.0) -> dict:
    """Create a CapCut draft from one trimmed video range.

    name:       draft name shown in CapCut
    video:      absolute path to the source video
    start/end:  clip range in the source (HH:MM:SS, MM:SS or seconds)
    subs:       optional .srt/.ass subtitle file (timed relative to the clip)
    canvas:     '16:9' (1920x1080) or '9:16' (1080x1920, video fit to width)
    title:      optional text shown for the first 3 seconds
    subs_offset: seconds to subtract from subtitle times if they are stream-absolute
    """
    return cc.make_clip_draft(
        name=name, video=video,
        start=cc.parse_time(start), end=cc.parse_time(end),
        subs=subs, canvas=canvas, title=title, subs_offset=subs_offset)


@mcp.tool()
def make_multi_clip_draft(name: str, stream_dir: str, plan: str | None = None,
                          video: str | None = None, canvas: str = '16:9',
                          clip_dir: str | None = None) -> dict:
    """Create one CapCut draft from a stream's clips_plan.json.

    name:       draft name shown in CapCut
    stream_dir: folder containing clips_plan.json and the source video
    plan:       optional path to clips_plan.json (default: <stream_dir>\\clips_plan.json)
    video:      optional source video (default: largest .mkv/.mp4 in stream_dir)
    canvas:     '16:9' (1920x1080) or '9:16' (1080x1920, video fit to width)
    clip_dir:   optional folder of pre-cut clips; one material per file is used
                instead of slicing one long source
    """
    return cc.make_multi_clip_draft(
        name=name, stream_dir=stream_dir, plan=plan, video=video, canvas=canvas,
        clip_dir=clip_dir)


@mcp.tool()
def import_subs(name: str, subs: str, subs_offset: float = 0.0,
                replace: bool = True) -> dict:
    """Add or replace the caption track of an existing draft (.srt/.ass)."""
    return cc.import_subs_into_draft(
        name=name, subs=subs, subs_offset=subs_offset, replace=replace)


@mcp.tool()
def delete_draft(name: str, confirm: bool = False) -> dict:
    """Move a draft to .mcp_trash (safe delete). Requires confirm=True."""
    return cc.delete_draft(name=name, confirm=confirm)


# ---------------------------------------------------------------- projects

@mcp.tool()
def capcut_version() -> dict:
    """What this server can do, the CapCut build it sees, and the catalogue counts."""
    return ed.capcut_version()


@mcp.tool()
def project_list() -> dict:
    """List CapCut projects with frame size, duration and open state."""
    return ed.project_list()


@mcp.tool()
def project_open(path: str) -> dict:
    """Open a CapCut project folder (or its name) for reading and editing."""
    return ed.project_open(path)


@mcp.tool()
def project_get(path: str) -> dict:
    """The project's full draft JSON, metadata, media bin and timeline summary."""
    return ed.project_get(path)


@mcp.tool()
def project_create(location: str, name: str, video: dict | None = None) -> dict:
    """Create a project folder from the installed CapCut schema.

    location: directory to make the folder in (empty means the CapCut drafts root)
    name:     project name
    video:    optional {width, height, rateNum, rateDen, canvas, colorSpace}
    """
    return ed.project_create(location, name, video)


@mcp.tool()
def project_save(path: str, name: str | None = None) -> dict:
    """Write an open project to disk (validating the draft first)."""
    return ed.project_save(path, name)


@mcp.tool()
def project_close(path: str, save: bool = True) -> dict:
    """Close an open project, saving unsaved edits by default."""
    return ed.project_close(path, save)


@mcp.tool()
def project_set_video(path: str, video: dict) -> dict:
    """Set the canvas size and frame rate: {width, height, rateNum, rateDen, canvas}."""
    return ed.project_set_video(path, video)


# ---------------------------------------------------------------- timeline

@mcp.tool()
def timeline_read(path: str) -> dict:
    """Read the timeline: frame, tracks (top to bottom), clips, captions, media."""
    return ed.timeline_read(path)


@mcp.tool()
def timeline_background(path: str, background: dict, timelineId: str = '') -> dict:
    """Set the background behind pictures: {kind: none|color|blur|image, ...}."""
    return ed.timeline_background(path, timelineId, background)


@mcp.tool()
def timeline_linkage(path: str, on: bool, timelineId: str = '') -> dict:
    """Turn magnetic-timeline linkage on or off."""
    return ed.timeline_linkage(path, timelineId, on)


@mcp.tool()
def track_add(path: str, bottom: bool = False, kind: str = 'video') -> dict:
    """Add a lane (video/audio/text/effect); bottom puts it under every other lane."""
    return ed.track_add(path, bottom, kind)


# ---------------------------------------------------------------- media

@mcp.tool()
def media_import(path: str, file: str) -> dict:
    """Add a local video/audio/image file to a project's bin. Returns createdId."""
    return ed.media_import(path, file)


@mcp.tool()
def media_probe(path: str) -> dict:
    """Probe a media file without changing any project."""
    return ed.media_probe(path)


# ---------------------------------------------------------------- clips

@mcp.tool()
def clip_add(path: str, mediaId: str, trackId: str, start: float,
             ripple: bool = False) -> dict:
    """Place a bin item on a lane at start seconds. Returns createdId."""
    return ed.clip_add(path, mediaId, trackId, start, ripple)


@mcp.tool()
def clips_add_sequence(path: str, mediaIds: list[str], start: float,
                       trackId: str | None = None, ripple: bool = False) -> dict:
    """Lay several bin items end to end in one undoable step."""
    return ed.clips_add_sequence(path, mediaIds, start, trackId, ripple)


@mcp.tool()
def clip_trim(path: str, clipId: str, edge: str, delta: float,
              ripple: bool = False) -> dict:
    """Move a clip edge by delta seconds (edge: start|end)."""
    return ed.clip_trim(path, clipId, edge, delta, ripple)


@mcp.tool()
def clip_speed(path: str, clipId: str, speed: float, ripple: bool = False) -> dict:
    """Retime a clip (0.0625..16); the same source stretch is kept."""
    return ed.clip_speed(path, clipId, speed, ripple)


@mcp.tool()
def clips_move(path: str, moves: list[dict]) -> dict:
    """Move clips in one step: [{clipId, start, trackId}, ...]."""
    return ed.clips_move(path, moves)


@mcp.tool()
def clips_remove(path: str, clipIds: list[str], ripple: bool = False) -> dict:
    """Delete clips; ripple closes the gaps they leave."""
    return ed.clips_remove(path, clipIds, ripple)


@mcp.tool()
def clips_split(path: str, clipIds: list[str], time: float) -> dict:
    """Cut each named clip in two at a timeline time (seconds)."""
    return ed.clips_split(path, clipIds, time)


@mcp.tool()
def clip_frame(path: str, clipId: str, framing: str, at: float = 0.0) -> dict:
    """Size a picture to the frame: framing fit|fill."""
    return ed.clip_frame(path, clipId, framing, at)


@mcp.tool()
def clip_transform(path: str, clipId: str, offsetX: float | None = None,
                   offsetY: float | None = None, rotation: float | None = None,
                   scale: float | None = None, stretchX: float | None = None,
                   stretchY: float | None = None) -> dict:
    """Set a picture's placement: offset fractions, rotation, scale, stretch."""
    return ed.clip_transform(path, clipId, offsetX, offsetY, rotation, scale,
                             stretchX, stretchY)


@mcp.tool()
def clip_update(path: str, clipId: str, patch: dict) -> dict:
    """Patch a clip: volume, fades, opacity, flips, crop, effects, filters,
    transitionIn, text, masks, background, motion, muted, blend, audioStream."""
    return ed.clip_update(path, clipId, patch)


@mcp.tool()
def clip_masks(path: str, clipId: str, masks: list[dict]) -> dict:
    """Set a clip's masks (shape, mode, x/y, rotation, scale, feather, invert)."""
    return ed.clip_masks(path, clipId, masks)


@mcp.tool()
def clip_stabilize(path: str, clipId: str, stabilize: dict | None = None) -> dict:
    """Set stabilization metadata (smoothness, maxZoom, tripod, keepRotation)."""
    return ed.clip_stabilize(path, clipId, stabilize)


# ---------------------------------------------------------------- text

@mcp.tool()
def captions_add(path: str, captions: list[dict], offsetY: float = 0.35,
                 style: dict | None = None) -> dict:
    """Place a run of captions: [{start, end, text}, ...] in one step."""
    return ed.captions_add(path, captions, offsetY, style)


@mcp.tool()
def title_add(path: str, start: float, style: dict | None = None,
              duration: float | None = None, offsetY: float | None = None,
              trackId: str | None = None, above: bool = False) -> dict:
    """Place a title (text over the picture) at start seconds."""
    return ed.title_add(path, start, style, duration, offsetY, trackId, above)


@mcp.tool()
def title_style_words(path: str, clipId: str, start: int, end: int,
                      patch: dict) -> dict:
    """Give characters start..end a look of their own (color, weight, etc.)."""
    return ed.title_style_words(path, clipId, start, end, patch)


@mcp.tool()
def title_clear_word_styles(path: str, clipId: str, start: int | None = None,
                            end: int | None = None) -> dict:
    """Remove per-word styling between characters start and end."""
    return ed.title_clear_word_styles(path, clipId, start, end)


# ---------------------------------------------------------------- keyframes/effects

@mcp.tool()
def keyframe_set(path: str, clipId: str, property: str, at: float, value: float,
                 ease: list[float] | None = None) -> dict:
    """Animate scale/offsetX/offsetY/rotation/stretchX/stretchY/opacity/volume."""
    return ed.keyframe_set(path, clipId, property, at, value, ease)


@mcp.tool()
def keyframe_clear(path: str, clipId: str, property: str, at: float) -> dict:
    """Remove the key on property at a fraction of the clip."""
    return ed.keyframe_clear(path, clipId, property, at)


@mcp.tool()
def mask_key_set(path: str, clipId: str, maskId: str, number: str, at: float,
                 value: float, ease: list[float] | None = None) -> dict:
    """Animate one of a clip's masks (x, y, rotation, scale, feather, ...)."""
    return ed.mask_key_set(path, clipId, maskId, number, at, value, ease)


@mcp.tool()
def effect_key_set(path: str, clipId: str, entry: int, key: str, at: float,
                   value: float, ease: list[float] | None = None) -> dict:
    """Animate one parameter of one effect in a clip's chain."""
    return ed.effect_key_set(path, clipId, entry, key, at, value, ease)


@mcp.tool()
def effect_layer_add(path: str, effectId: str, name: str, start: float,
                     duration: float | None = None, sound: bool = False,
                     trackId: str | None = None) -> dict:
    """Place an effect (or audio effect) as a layer over the timeline."""
    return ed.effect_layer_add(path, effectId, name, start, duration, sound, trackId)


@mcp.tool()
def effects_list(kind: str | None = None) -> dict:
    """The effect/transition/animation catalogue: id, name, kind and ids."""
    return ed.effects_list(kind)


# ---------------------------------------------------------------- preview/export

@mcp.tool()
def preview_frame(path: str, time: float, output: str | None = None,
                  width: int | None = None, height: int | None = None):
    """Render the composited frame at time seconds. Returns the PNG path, or the
    image itself when output is omitted."""
    result = render.preview_frame(path, time, output, width, height)
    if output is None:
        return Image(path=result['path'])
    return result


@mcp.tool()
def export_start(path: str, output: str, width: int | None = None,
                 height: int | None = None, rateNum: int | None = None,
                 rateDen: int | None = None, codec: str | None = None,
                 crf: int | None = None, preset: str | None = None,
                 tenBit: bool | None = None, hdr: bool | None = None,
                 colorRange: str | None = None) -> dict:
    """Start an ffmpeg render of the timeline to an .mp4. Returns a job id."""
    return render.export_start(path, output, width, height, rateNum, rateDen,
                               codec, crf, preset, tenBit, hdr, colorRange)


@mcp.tool()
def export_status(job: str) -> dict:
    """How an export job stands: state, stage, progress, output, error."""
    return render.export_status(job)


@mcp.tool()
def export_wait(job: str, timeoutSeconds: float = 60) -> dict:
    """Wait for an export job to end (up to 600 seconds)."""
    return render.export_wait(job, timeoutSeconds)


@mcp.tool()
def export_cancel(job: str) -> dict:
    """Stop a running export at its next frame."""
    return render.export_cancel(job)


# ---------------------------------------------------------------- edit bus / templates

@mcp.tool()
def edit_apply(path: str, command: dict) -> dict:
    """Apply any one edit command, or {"op":"batch","commands":[...]}, as one step."""
    return ed.edit_apply(path, command)


@mcp.tool()
def edit_undo(path: str) -> dict:
    """Undo the last edit."""
    return ed.edit_undo(path)


@mcp.tool()
def edit_redo(path: str) -> dict:
    """Redo the last undone edit."""
    return ed.edit_redo(path)


@mcp.tool()
def template_list() -> dict:
    """List project templates and their fill slots."""
    return ed.template_list()


@mcp.tool()
def template_instantiate(template: str, location: str, name: str,
                         fills: list[dict]) -> dict:
    """Make a project from a template: fills = [{slot, file}, ...]."""
    return ed.template_instantiate(template, location, name, fills)


if __name__ == '__main__':
    mcp.run()
