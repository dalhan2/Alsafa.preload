"""Build ONE CapCut draft per stream from its clips_plan.json.

Thin command-line wrapper around ``capcutlib.make_multi_clip_draft`` -- the
same builder the CapCut MCP server (``server.py``) uses, so the preprocessing
skill and the editor produce identical drafts. Clones a real multi-clip donor
project, lays one segment per plan entry (or one material per file when
``--clip-dir`` is given), and keeps the donor's draft/timeline ids.

Usage:
  make_multi_clip_draft.py <stream_dir> --name <draft name> [--plan path]
      [--video path] [--canvas 16:9|9:16] [--clip-dir folder]
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Prefer an explicit override, otherwise use the checkout this script lives in.
_repo = Path(os.environ.get('PREPROCESS_REPO') or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(_repo))
import capcutlib as cc  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('stream', type=Path,
                    help='folder containing clips_plan.json and the source video')
    ap.add_argument('--name', required=True, help='draft name shown in CapCut')
    ap.add_argument('--plan', default=None,
                    help='explicit clips_plan.json (default: <stream>/clips_plan.json)')
    ap.add_argument('--video', default=None,
                    help='source video (default: largest .mkv/.mp4 in <stream>)')
    ap.add_argument('--canvas', default='16:9', choices=('16:9', '9:16'),
                    help='output canvas (default: 16:9)')
    ap.add_argument('--clip-dir', default=None,
                    help='folder of pre-cut clips; one material per file')
    a = ap.parse_args()

    if cc.capcut_running():
        sys.exit('CapCut is running - close it fully first')

    try:
        result = cc.make_multi_clip_draft(
            name=a.name,
            stream_dir=str(a.stream),
            plan=str(a.plan) if a.plan else None,
            video=str(a.video) if a.video else None,
            canvas=a.canvas,
            clip_dir=str(a.clip_dir) if a.clip_dir else None,
        )
    except RuntimeError as exc:
        sys.exit(str(exc))

    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
