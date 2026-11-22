"""Offline smoke tests for the preprocess toolchain.

Fixtures are generated in temp dirs; no network, ffmpeg or CapCut required.
Run with: python -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'


def run_script(name, *args, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *[str(a) for a in args]],
        capture_output=True, text=True, check=True, env=env,
    )


class TranscriptTest(unittest.TestCase):
    def test_build_transcript2_and_cutpoints(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            src = d / 'cc_abc.en.json3'
            src.write_text(json.dumps({'events': [
                {'tStartMs': 0, 'segs': [
                    {'utf8': 'Hello', 'tOffsetMs': 0},
                    {'utf8': 'world.', 'tOffsetMs': 500},
                ]},
                {'tStartMs': 20000, 'segs': [
                    {'utf8': 'After', 'tOffsetMs': 0},
                    {'utf8': 'silence.', 'tOffsetMs': 500},
                ]},
            ]}), encoding='utf-8')

            run_script('build_transcript2.py', src,
                       d / 'transcript.txt', d / 'cutpoints.txt',
                       d / 'major.txt', 'en')

            self.assertIn('Hello world.', (d / 'transcript.txt').read_text(encoding='utf-8'))
            self.assertIn('CUT at', (d / 'cutpoints.txt').read_text(encoding='utf-8'))
            self.assertIn('BREAK at', (d / 'major.txt').read_text(encoding='utf-8'))


class ChatTest(unittest.TestCase):
    def test_analyze_live_chat_finds_spike(self):
        rows = []

        def msg(t, text):
            rows.append(json.dumps({'replayChatItemAction': {
                'videoOffsetTimeMsec': int(t * 1000),
                'actions': [{'addChatItemAction': {'item': {
                    'liveChatTextMessageRenderer': {
                        'message': {'runs': [{'text': text}]},
                    },
                }}}],
            }}))

        msg(100, 'welcome')                       # intro, skipped by the analyzer
        for t in range(610, 700, 10):
            msg(t, 'hi')                          # background: 1 msg per 10 s
        for _ in range(30):
            msg(650, 'haha')                      # hype spike

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'abc.live_chat.json'
            src.write_text('[\n' + ',\n'.join(rows) + '\n]', encoding='utf-8')
            out = Path(td) / 'digest.txt'
            run_script('analyze_live_chat.py', src, out)
            digest = out.read_text(encoding='utf-8')
            self.assertIn('haha', digest)
            self.assertIn('msgs/10s', digest)


class PlannerTest(unittest.TestCase):
    def test_plan_clips_v2_parses_tables_and_skips_rejected_rows(self):
        transcript = """# Stream

## Mid stream

| Time | Moment |
| --- | --- |
| 00:01:00 | **Big** moment |

### Verdict — where to cut

| Tier | Time | Moment |
| --- | --- | --- |
| — | 00:02:00 | rejected |
| A | 00:03:00–00:03:30 | confirmed |
"""
        cuts = """CUT at 00:00:58   (silent 2.0s, a -> b)
CUT at 00:01:12   (silent 3.0s, a -> b)
CUT at 00:02:58   (silent 2.0s, a -> b)
CUT at 00:03:32   (silent 4.0s, a -> b)
CUT at 00:03:40   (silent 2.0s, a -> b)
"""

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            stream = d / 'saba_abc'
            stream.mkdir()
            (stream / 'transcript v2.md').write_text(transcript, encoding='utf-8')
            (stream / 'cutpoints_abc.txt').write_text(cuts, encoding='utf-8')
            out = d / 'clips_plan.json'

            run_script('plan_clips_v2.py', stream, '--out', out, '--duration', '600')
            plan = json.loads(out.read_text(encoding='utf-8'))

            self.assertEqual(len(plan), 2)
            self.assertEqual(plan[0]['start'], 58)
            self.assertEqual(plan[1]['start'], 178)
            self.assertFalse(any('rejected' in ' '.join(c['labels']) for c in plan))


class VendoredLibTest(unittest.TestCase):
    def test_make_multi_clip_draft_imports_vendored_capcutlib(self):
        env = os.environ.copy()
        env.pop('PREPROCESS_REPO', None)
        code = (
            f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); "
            "import make_multi_clip_draft as m; print(m.cc.__file__)"
        )
        result = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True, text=True, check=True, env=env, cwd=str(REPO),
        )
        self.assertEqual(
            Path(result.stdout.strip()).resolve(),
            (REPO / 'capcutlib.py').resolve(),
        )


if __name__ == '__main__':
    unittest.main()
