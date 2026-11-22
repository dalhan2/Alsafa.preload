"""Build ONE CapCut draft per stream from its clips_plan.json.

Same core as the MCP's make_clip_draft, extended to many segments:
clones the `template` draft schema, one video material (the stream master),
one segment per plan clip laid back-to-back, no captions/titles/effects.

Usage: make_multi_clip_draft.py <stream_dir> --name <draft name> [--plan path]
"""
import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Prefer an explicit override, otherwise use the vendored copy at the repo root.
_repo = Path(os.environ.get('PREPROCESS_REPO') or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(_repo))
import capcutlib as cc  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('stream', type=Path)
    ap.add_argument('--name', required=True)
    ap.add_argument('--plan', default=None)
    a = ap.parse_args()

    if cc.capcut_running():
        sys.exit('CapCut is running - close it fully first')
    root = cc.drafts_root()
    tpl = cc.find_template(root)
    if not tpl:
        sys.exit('no template draft found')

    plan_path = Path(a.plan) if a.plan else a.stream / 'clips_plan.json'
    clips = json.loads(plan_path.read_text(encoding='utf-8-sig'))
    videos = [p for p in a.stream.iterdir() if p.suffix.lower() in ('.mkv', '.mp4')]
    video = max(videos, key=lambda p: p.stat().st_size)
    probe = cc.ffprobe_media(str(video))

    content = copy.deepcopy(tpl['content'])
    vtrack = cc.find_track(content, 'video')
    ttrack = cc.find_track(content, 'text')
    shape = copy.deepcopy(vtrack['segments'][0])
    vmat_src = cc.find_material(content, shape['material_id'])
    if not vmat_src:
        sys.exit('template has no video material')

    vmat = copy.deepcopy(vmat_src)
    vmat['id'] = cc.uid()
    vmat['path'] = cc.posix(os.path.abspath(video))
    vmat['material_name'] = video.name
    vmat['duration'] = int(round(probe['duration'] * 1e6))
    vmat['width'] = probe.get('width') or 1920
    vmat['height'] = probe.get('height') or 1080
    if 'has_audio' in vmat:
        vmat['has_audio'] = probe['has_audio']
    content.setdefault('materials', {})['videos'] = [vmat]

    segs, acc = [], 0
    for c in clips:
        dur = int(round(c['duration'] * 1e6))
        src = int(round(c['start'] * 1e6))
        seg = copy.deepcopy(shape)
        seg['id'] = cc.uid()
        seg['material_id'] = vmat['id']
        seg['target_timerange'] = {'start': acc, 'duration': dur}
        seg['source_timerange'] = {'start': src, 'duration': dur}
        seg['speed'] = 1.0
        seg['volume'] = 1.0
        seg['extra_material_refs'] = []
        for k in ('common_keyframes', 'keyframe_refs'):
            seg.pop(k, None)
        segs.append(seg)
        acc += dur
    vtrack['segments'] = segs
    ttrack['segments'] = []
    content['tracks'] = [vtrack, ttrack]
    content['duration'] = acc
    content['id'] = cc.uid()
    cc.set_canvas(content, 1920, 1080)
    cc._prune_unused(content)
    errors = cc.validate(content, str(video))
    if errors:
        sys.exit('validation failed: ' + '; '.join(errors[:8]))

    dest = os.path.join(root, time.strftime('%Y%m%d_%H%M%S') + '_' + a.name)
    shutil.copytree(tpl['folder'], dest)
    shutil.rmtree(os.path.join(dest, cc.BACKUP_DIR), ignore_errors=True)
    cc.write_json(cc.content_path(dest), content)

    draft_id = cc.uid()
    meta_path = os.path.join(dest, 'draft_meta_info.json')
    meta = cc.read_json(meta_path) if os.path.isfile(meta_path) else {}
    meta['draft_name'] = a.name
    meta['draft_id'] = draft_id
    meta['tm_duration'] = acc
    meta['tm_draft_create'] = cc.now_us()
    meta['tm_draft_modified'] = cc.now_us()
    meta['draft_fold_path'] = cc.posix(dest)
    meta['draft_root_path'] = cc.posix(root)
    meta['draft_is_invisible'] = False
    cc._register_media(meta, str(video), probe)
    mid = clips[0]['start'] + clips[0]['duration'] / 2
    cover = os.path.join(dest, 'draft_cover.jpg')
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', f'{mid:.3f}',
                    '-i', str(video), '-frames:v', '1', '-vf', 'scale=480:-2',
                    cover], capture_output=True, timeout=300)
    if os.path.isfile(cover):
        meta['draft_cover'] = cc.posix(cover)
    cc.write_json(meta_path, meta)

    cc._register_root(root, tpl, a.name, dest, acc, draft_id)
    print(json.dumps({'ok': True, 'draft': a.name, 'segments': len(segs),
                      'duration_sec': round(acc / 1e6, 1),
                      'video': str(video), 'folder': dest}))


if __name__ == '__main__':
    main()
