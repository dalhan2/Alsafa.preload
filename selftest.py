"""Self-test for capcutlib using a synthetic template + real ffmpeg test video.

Run:  .venv\\Scripts\\python.exe selftest.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix='capcut_mcp_selftest_')
os.environ['CAPCUT_DRAFTS_ROOT'] = TMP

import capcutlib as cc   # noqa: E402
import subs as subslib   # noqa: E402

# Every write below goes to TMP, so the real CapCut process guard is irrelevant.
cc.capcut_running = lambda: False


def make_donor():
    """A synthetic multi-clip project, like one CapCut built from several clips.

    make_multi_clip_draft() clones a project with a real per-clip material
    graph (speed/placeholder/canvas/sound/vocal refs + a parallel audio track);
    a one-clip template cannot serve as the donor.
    """
    folder = os.path.join(TMP, '20260101_capcut_donor')
    os.makedirs(folder, exist_ok=True)
    n = 4
    videos, audios = [], []
    speeds, phs, canv, snds, cols, vocs, beats = [], [], [], [], [], [], []
    vsegs, asegs = [], []
    acc = 0
    for i in range(n):
        vid, aid = f'v-{i}', f'a-{i}'
        videos.append({'id': vid, 'path': 'C:/donor/donor.mp4',
                       'material_name': 'donor.mp4', 'duration': 5_000_000,
                       'width': 1920, 'height': 1080, 'has_audio': True,
                       'type': 'video'})
        audios.append({'id': aid, 'path': 'C:/donor/donor.mp4',
                       'duration': 5_000_000, 'type': 'audio'})
        sp, ph, cv, sd, co, vo, bt = (f'sp-{i}', f'ph-{i}', f'cv-{i}', f'sd-{i}',
                                      f'co-{i}', f'vo-{i}', f'bt-{i}')
        speeds.append({'id': sp, 'type': 'speed', 'mode': 0, 'speed': 1.0})
        phs.append({'id': ph, 'type': 'placeholder_info', 'meta_type': 'none'})
        canv.append({'id': cv, 'type': 'canvas_color', 'color': ''})
        snds.append({'id': sd, 'type': 'none', 'audio_channel_mapping': 0})
        cols.append({'id': co, 'type': 'color', 'color': ''})
        vocs.append({'id': vo, 'type': 'vocal_separation', 'choice': 0})
        beats.append({'id': bt, 'type': 'beats', 'enable_ai_beats': False})
        dur = 1_000_000
        vsegs.append({'id': f'vs-{i}', 'material_id': vid,
                      'target_timerange': {'start': acc, 'duration': dur},
                      'source_timerange': {'start': i * dur, 'duration': dur},
                      'render_timerange': {'start': 0, 'duration': 0},
                      'speed': 1.0, 'volume': 1.0, 'visible': True,
                      'common_keyframes': [], 'keyframe_refs': [],
                      'clip': {'scale': {'x': 1.0, 'y': 1.0}, 'rotation': 0.0,
                               'transform': {'x': 0.0, 'y': 0.0}},
                      'extra_material_refs': [sp, ph, cv, sd, co, vo]})
        asegs.append({'id': f'as-{i}', 'material_id': aid,
                      'target_timerange': {'start': acc, 'duration': dur},
                      'source_timerange': {'start': i * dur, 'duration': dur},
                      'render_timerange': {'start': 0, 'duration': 0},
                      'speed': 1.0, 'volume': 1.0, 'visible': True,
                      'common_keyframes': [], 'keyframe_refs': [],
                      'clip': None,
                      'extra_material_refs': [sp, ph, bt, sd, vo]})
        acc += dur
    content = {
        'id': 'DONOR-CONTENT-ID',
        'duration': acc,
        'canvas_config': {'width': 1920, 'height': 1080, 'ratio': '16:9'},
        'materials': {'videos': videos, 'audios': audios, 'texts': [],
                      'speeds': speeds, 'placeholder_infos': phs,
                      'canvases': canv, 'sound_channel_mappings': snds,
                      'material_colors': cols, 'vocal_separations': vocs,
                      'beats': beats},
        'tracks': [{'id': 'tr-v', 'type': 'video', 'segments': vsegs},
                   {'id': 'tr-a', 'type': 'audio', 'segments': asegs}],
    }
    _write(os.path.join(folder, 'draft_content.json'), content)
    _write(os.path.join(folder, 'draft_meta_info.json'), {
        'draft_name': 'capcut donor', 'draft_id': 'DONOR-META-ID',
        'tm_duration': acc, 'draft_fold_path': cc.posix(folder),
        'draft_root_path': cc.posix(TMP),
        'draft_materials': [{'type': 0, 'value': [{
            'metetype': 'video', 'file_Path': 'C:/donor/donor.mp4',
            'material_name': 'donor.mp4', 'duration': 5_000_000,
            'width': 1920, 'height': 1080}]}]})
    _write(os.path.join(folder, 'timeline_layout.json'), {
        'dockItems': [{'dockIndex': 0, 'ratio': 1,
                       'timelineIds': ['DONOR-CONTENT-ID'],
                       'timelineNames': ['Timeline 01']}],
        'layoutOrientation': 1})
    _write(os.path.join(TMP, 'root_meta_info.json'), {
        'all_draft_store': [
            {'draft_name': 'capcut template',
             'draft_fold_path': cc.posix(os.path.join(TMP, '20260101_capcut_template')),
             'draft_id': 'template-meta', 'tm_duration': 5_000_000},
            {'draft_name': 'capcut donor',
             'draft_fold_path': cc.posix(folder),
             'draft_id': 'DONOR-META-ID', 'tm_duration': acc}],
        'draft_ids': 2, 'root_path': cc.posix(TMP)})
    return folder


def make_template():
    folder = os.path.join(TMP, '20260101_capcut_template')
    os.makedirs(folder, exist_ok=True)
    content = {
        'id': 'template-content',
        'duration': 5_000_000,
        'canvas_config': {'width': 1920, 'height': 1080, 'ratio': '16:9'},
        'materials': {
            'videos': [{
                'id': 'v-tpl', 'path': 'C:/template/template.mp4',
                'material_name': 'template.mp4', 'duration': 5_000_000,
                'width': 1920, 'height': 1080, 'has_audio': True, 'type': 'video'}],
            'audios': [],
            'texts': [{
                'id': 't-tpl', 'type': 'text', 'material_name': 'text',
                'content': json.dumps({
                    'text': 'TEMPLATE',
                    'styles': [{'range': [0, 8], 'size': 9.0,
                                'fill': {'content': {'solid': {'color': [1, 1, 1]}}}}]})}],
            'speeds': [{'id': 'speed-tpl', 'type': 'speed', 'speed': 1.0}],
        },
        'tracks': [
            {'id': 'tr-v', 'type': 'video', 'segments': [{
                'id': 's-v', 'material_id': 'v-tpl',
                'target_timerange': {'start': 0, 'duration': 5_000_000},
                'source_timerange': {'start': 0, 'duration': 5_000_000},
                'speed': 1.0, 'volume': 1.0,
                'clip': {'alpha': 1.0, 'rotation': 0.0,
                         'scale': {'x': 1.0, 'y': 1.0},
                         'transform': {'x': 0.0, 'y': 0.0}},
                'extra_material_refs': ['speed-tpl']}]},
            {'id': 'tr-t', 'type': 'text', 'segments': [{
                'id': 's-t', 'material_id': 't-tpl',
                'target_timerange': {'start': 0, 'duration': 2_000_000},
                'source_timerange': {'start': 0, 'duration': 2_000_000},
                'clip': {'alpha': 1.0, 'rotation': 0.0,
                         'scale': {'x': 1.0, 'y': 1.0},
                         'transform': {'x': 0.0, 'y': -0.55}}}]},
        ],
    }
    _write(os.path.join(folder, 'draft_content.json'), content)
    meta = {
        'draft_name': 'capcut template', 'draft_id': 'template-meta',
        'tm_duration': 5_000_000,
        'draft_fold_path': cc.posix(folder), 'draft_root_path': cc.posix(TMP),
        'draft_materials': [{'type': 0, 'value': [{
            'metetype': 'video', 'file_Path': 'C:/template/template.mp4',
            'material_name': 'template.mp4', 'duration': 5_000_000,
            'width': 1920, 'height': 1080}]}],
    }
    _write(os.path.join(folder, 'draft_meta_info.json'), meta)
    return folder


def _write(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)


def main():
    ok = False
    try:
        print('temp:', TMP)
        make_template()
        make_donor()

        video = os.path.join(TMP, 'test.mp4')
        subprocess.run([
            'ffmpeg', '-y', '-v', 'error',
            '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30',
            '-f', 'lavfi', '-i', 'sine=frequency=440',
            '-t', '3', '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac',
            '-shortest', video], check=True, timeout=180)

        srt = os.path.join(TMP, 'test.srt')
        with open(srt, 'w', encoding='utf-8') as f:
            f.write('1\n00:00:00,200 --> 00:00:01,000\nHello caption\n\n'
                    '2\n00:00:01,200 --> 00:00:02,400\nSecond caption\n')

        # doctor sees the template
        doc = cc.doctor()
        assert doc['template_ready'], doc
        assert doc['captions_ready'], doc

        res = cc.make_clip_draft('test clip EN', video, 0.5, 2.5,
                                 subs=srt, title='Test Title')
        assert res['ok'], res
        assert res['caption_segments'] == 2, res

        content = cc.read_json(cc.content_path(res['folder']))
        assert content['duration'] == 2_000_000, content['duration']
        vseg = cc.find_track(content, 'video')['segments']
        assert len(vseg) == 1
        assert vseg[0]['source_timerange']['start'] == 500_000
        assert vseg[0]['target_timerange']['duration'] == 2_000_000
        text_tracks = [t for t in content['tracks'] if cc.track_type(t) == 'text']
        total_text = sum(len(t.get('segments') or []) for t in text_tracks)
        assert total_text == 3, total_text  # caption track (2) + title track (1)
        assert not cc.validate(content, video), cc.validate(content, video)

        rd = cc.read_draft('test clip EN')
        assert len(rd['captions']) == 3, rd['captions']
        assert any(c['text'] == 'Test Title' for c in rd['captions'])

        # vertical canvas
        res2 = cc.make_clip_draft('test vertical', video, 0.0, 2.0,
                                  subs=srt, canvas='9:16')
        c2 = cc.read_json(cc.content_path(res2['folder']))
        assert c2['canvas_config']['width'] == 1080
        assert c2['canvas_config']['height'] == 1920
        vclip = cc.find_track(c2, 'video')['segments'][0]['clip']
        assert abs(vclip['scale']['x'] - 1080 / 1280) < 1e-6, vclip

        # replace captions on an existing draft
        imp = cc.import_subs_into_draft('test vertical', srt)
        assert imp['captions'] == 2, imp
        rd2 = cc.read_draft('test vertical')
        assert len(rd2['captions']) == 2, rd2['captions']

        # ASS parser
        ass = os.path.join(TMP, 'x.ass')
        with open(ass, 'w', encoding='utf-8') as f:
            f.write('[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, '
                    'MarginR, MarginV, Effect, Text\n'
                    'Dialogue: 0,0:00:00.50,0:00:01.50,Default,,0,0,0,,'
                    r'{\b1}Hello\Nworld{\r}' + '\n')
        cues = subslib.parse_subs(ass)
        assert cues[0]['text'] == 'Hello\nworld', cues
        assert cues[0]['start'] == 0.5 and cues[0]['end'] == 1.5, cues

        # multi-clip compilation from clips_plan.json
        stream = os.path.join(TMP, 'stream')
        os.makedirs(stream, exist_ok=True)
        shutil.copy2(video, os.path.join(stream, 'test.mp4'))
        _write(os.path.join(stream, 'clips_plan.json'), [
            {'start': 0.2, 'end': 1.0, 'duration': 0.8},
            {'start': 1.2, 'end': 2.2, 'duration': 1.0},
            {'start': 2.4, 'end': 2.9, 'duration': 0.5},
        ])
        mres = cc.make_multi_clip_draft('test multi', stream)
        assert mres['ok'] and mres['segments'] == 3, mres
        assert mres['duration_sec'] == 2.3, mres
        mc = cc.read_json(cc.content_path(mres['folder']))
        msegs = cc.find_track(mc, 'video')['segments']
        assert len(msegs) == 3, msegs
        assert [s['target_timerange']['start'] for s in msegs] == [
            0, 800_000, 1_800_000], msegs
        assert [s['source_timerange']['start'] for s in msegs] == [
            200_000, 1_200_000, 2_400_000], msegs
        assert not cc.validate(mc, os.path.join(stream, 'test.mp4'))

        # captions can be imported into a video-only multi-clip draft
        impm = cc.import_subs_into_draft('test multi', srt)
        assert impm['captions'] == 2, impm
        rdm = cc.read_draft('test multi')
        assert len(rdm['captions']) == 2, rdm['captions']

        # vertical multi-clip canvas
        mres2 = cc.make_multi_clip_draft('test multi vertical', stream,
                                         canvas='9:16')
        mc2 = cc.read_json(cc.content_path(mres2['folder']))
        assert mc2['canvas_config']['width'] == 1080
        assert mc2['canvas_config']['height'] == 1920
        mvclip = cc.find_track(mc2, 'video')['segments'][0]['clip']
        assert abs(mvclip['scale']['x'] - 1080 / 1280) < 1e-6, mvclip

        # safe delete
        dele = cc.delete_draft('test vertical', confirm=True)
        assert os.path.isdir(dele['moved_to'])

        ok = True
        print('PASS: single/multi drafts, captions, vertical canvas, import, '
              'parsers, delete')
    finally:
        if ok:
            shutil.rmtree(TMP, ignore_errors=True)
        else:
            print('FAILED - temp kept for inspection:', TMP)


if __name__ == '__main__':
    main()
