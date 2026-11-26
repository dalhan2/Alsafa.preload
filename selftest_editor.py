"""Self-test for the agentic editor layer.

Run: .venv\\Scripts\\python.exe selftest_editor.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix='capcut_mcp_editor_')
os.environ['CAPCUT_DRAFTS_ROOT'] = TMP
os.environ.pop('CAPCUT_DONOR', None)
os.environ.pop('CAPCUT_TEMPLATE', None)

import capcutlib as cc  # noqa: E402
import editor as ed  # noqa: E402
import render  # noqa: E402

cc.capcut_running = lambda: False


def copy_donor():
    real = os.path.join(os.environ['LOCALAPPDATA'], 'CapCut', 'User Data',
                        'Projects', 'com.lveditor.draft')
    candidates = []
    if os.path.isdir(real):
        for name in os.listdir(real):
            cp = os.path.join(real, name, 'draft_content.json')
            if os.path.isfile(cp):
                candidates.append((os.path.getsize(cp), os.path.join(real, name)))
    if not candidates:
        raise RuntimeError('no real CapCut draft available to seed the test')
    candidates.sort()
    # a mid-size project has text/audio and still copies quickly
    src = candidates[len(candidates) // 2][1]
    dst = os.path.join(TMP, 'donor')
    shutil.copytree(src, dst)
    shutil.rmtree(os.path.join(dst, cc.BACKUP_DIR), ignore_errors=True)
    return dst


def make_video(path):
    subprocess.run([
        'ffmpeg', '-y', '-v', 'error',
        '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30',
        '-f', 'lavfi', '-i', 'sine=frequency=440',
        '-t', '3', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', path], check=True, timeout=180)


def main():
    ok = False
    try:
        print('temp:', TMP)
        copy_donor()
        video = os.path.join(TMP, 'test.mp4')
        make_video(video)

        created = ed.project_create(TMP, 'agent test', {
            'width': 1280, 'height': 720, 'rateNum': 30, 'rateDen': 1})
        path = created['path']
        assert created['created'], created

        media = ed.media_import(path, video)
        media_id = media['createdId']

        added = ed.clip_add(path, media_id, '', 0.0)
        clip_id = added['createdId']
        assert clip_id, added
        added_tl = ed.timeline_read(path)
        video_seg = next(s for t in added_tl['tracks'] if t['kind'] == 'video'
                         for s in t['segments'])
        assert video_seg['sourceStart'] == 0.0, video_seg
        assert video_seg['sourceDuration'] > 0, video_seg

        caps = ed.captions_add(path, [
            {'start': 0.1, 'end': 1.0, 'text': 'Hello editor'},
            {'start': 1.1, 'end': 2.0, 'text': 'Second caption'},
        ], 0.35, {'color': '#ffffff', 'fontSize': 0.09})
        assert caps['count'] == 2, caps

        title = ed.title_add(path, 0.2, {'content': 'Agent Title', 'color': '#ffcc00'},
                             duration=1.0, offsetY=-0.4)
        assert title['createdId'], title

        tl = ed.timeline_read(path)
        assert tl['tracks'] and tl['captions'], tl

        split = ed.clips_split(path, [clip_id], 1.5)
        assert split['splitCount'] == 1, split

        trimmed = ed.clip_trim(path, clip_id, 'end', -0.2)
        assert trimmed['clipId'] == clip_id, trimmed

        sped = ed.clip_speed(path, clip_id, 1.5)
        assert sped['speed'] == 1.5, sped

        ed.clip_transform(path, clip_id, offsetX=0.1, offsetY=-0.05,
                          rotation=2.0, scale=1.1)
        ed.clip_update(path, clip_id, {
            'opacity': 0.95,
            'crop': {'left': 0.02, 'right': 0.02, 'top': 0.02, 'bottom': 0.02},
            'fadeIn': 0.2,
            'fadeOut': 0.2,
            'transitionIn': {'id': 'dissolve', 'duration': 0.3},
        })
        masks = ed.clip_masks(path, clip_id, [{
            'source': {'kind': 'shape', 'shape': 'circle', 'width': 0.7, 'height': 0.7},
            'x': 0.0, 'y': 0.0, 'feather': 0.0, 'invert': False,
        }])
        assert masks['maskCount'] == 1, masks
        mask_id = masks['createdIds'][0]
        ed.mask_key_set(path, clip_id, mask_id, 'scale', 0.5, 1.1)

        ed.keyframe_set(path, clip_id, 'scale', 0.0, 1.1)
        ed.keyframe_set(path, clip_id, 'offsetX', 1.0, 0.2)

        fx = ed.effect_layer_add(path, 'blur', 'Soft Blur', 0.3, 0.8)
        assert fx['createdId'], fx

        ed.title_style_words(path, title['createdId'], 0, 5,
                             {'color': '#ff0000', 'fontWeight': 900})
        ed.title_clear_word_styles(path, title['createdId'], 0, 5)

        saved = ed.project_save(path)
        assert saved['ok'], saved

        # reopen from disk and check validation
        ed._PROJECTS.clear()
        reopened = ed.project_get(path)
        content = reopened['content']
        assert not cc.validate(content), cc.validate(content)
        assert ed.timeline_read(path)['duration'] > 0

        # undo/redo on a fresh edit
        ed.clip_transform(path, clip_id, scale=1.2)
        assert ed.edit_undo(path)['ok']
        assert ed.edit_redo(path)['ok']

        # preview frame + export
        frame = render.preview_frame(path, 0.7, os.path.join(TMP, 'frame.png'),
                                     width=640)
        assert os.path.isfile(frame['path']), frame
        assert any(layer.get('kind') == 'video' for layer in frame['layers']), frame
        assert any(layer.get('kind') == 'text' for layer in frame['layers']), frame
        print('preview:', frame['path'])

        job = render.export_start(path, os.path.join(TMP, 'out.mp4'),
                                  width=640, height=360, crf=28, preset='ultrafast')
        print('export job:', job['job'])
        status = render.export_wait(job['job'], timeoutSeconds=120)
        print('export status:', {k: status.get(k) for k in ('state', 'stage', 'progress', 'error')})
        assert status['state'] == 'done', status
        out_path = os.path.join(TMP, 'out.mp4')
        assert os.path.getsize(out_path) > 1000
        probe = cc.ffprobe_media(out_path)
        assert probe['duration'] > 0.5, probe
        assert probe['width'] == 640 and probe['height'] == 360, probe

        listing = ed.project_list()
        assert any(p['path'] == path for p in listing['projects']), listing

        ok = True
        print('PASS: project create, media, clips, captions, title, split, trim, '
              'speed, transform, crop, transition, mask, keyframes, effect, '
              'undo/redo, save/reopen, preview, export')
    finally:
        if ok:
            shutil.rmtree(TMP, ignore_errors=True)
        else:
            print('FAILED - temp kept for inspection:', TMP)


if __name__ == '__main__':
    main()
