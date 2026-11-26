"""End-to-end smoke test for the editor tools over MCP stdio."""
import asyncio
import json
import os
import shutil
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.environ.get('CAPCUT_PYTHON') or sys.executable
TMP = tempfile.mkdtemp(prefix='capcut_mcp_mcp_')


def seed():
    real = os.path.join(os.environ['LOCALAPPDATA'], 'CapCut', 'User Data',
                        'Projects', 'com.lveditor.draft')
    src = os.environ.get('CAPCUT_SMOKE_SEED')
    if src:
        src = src if os.path.isdir(src) else os.path.join(real, src)
    else:
        candidates = [os.path.join(real, n) for n in os.listdir(real)] \
            if os.path.isdir(real) else []
        candidates = [p for p in candidates
                      if os.path.isfile(os.path.join(p, 'draft_content.json'))]
        src = max(candidates, key=os.path.getsize) if candidates else ''
    if not src or not os.path.isdir(src):
        raise RuntimeError('no CapCut draft available to seed the smoke test')
    shutil.copytree(src, os.path.join(TMP, 'seed'))
    shutil.rmtree(os.path.join(TMP, 'seed', '.mcp_backups'), ignore_errors=True)


def sample_video():
    explicit = os.environ.get('CAPCUT_SMOKE_VIDEO')
    if explicit and os.path.isfile(explicit):
        return explicit
    root = os.path.join(os.path.dirname(HERE), 'capcut')
    for base, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith('.mp4'):
                return os.path.join(base, name)
    raise RuntimeError('set CAPCUT_SMOKE_VIDEO to a short .mp4 to run this test')


async def main():
    seed()
    params = StdioServerParameters(
        command=PY, args=[os.path.join(HERE, 'server.py')], cwd=HERE,
        env={**os.environ, 'CAPCUT_DRAFTS_ROOT': TMP,
             'CAPCUT_MCP_ALLOW_RUNNING': '1'})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            print('tool count:', len(names))
            assert 'project_create' in names and 'preview_frame' in names

            async def call(name, args):
                result = await session.call_tool(name, args)
                print(name, 'is_error:', result.is_error)
                if result.is_error:
                    print(result.content)
                    raise SystemExit(1)
                if result.structured_content:
                    return result.structured_content
                if result.content and result.content[0].type == 'text':
                    try:
                        return json.loads(result.content[0].text)
                    except Exception:
                        return result.content[0].text
                return result.content

            created = await call('project_create', {
                'location': TMP, 'name': 'mcp editor test',
                'video': {'width': 1280, 'height': 720, 'rateNum': 30, 'rateDen': 1}})
            path = created['path']
            media = await call('media_import', {'path': path,
                                               'file': sample_video()})
            clip = await call('clip_add', {
                'path': path, 'mediaId': media['createdId'], 'trackId': '',
                'start': 0.0})
            await call('captions_add', {
                'path': path, 'captions': [
                    {'start': 0.2, 'end': 1.2, 'text': 'MCP caption one'},
                    {'start': 1.3, 'end': 2.2, 'text': 'MCP caption two'}]})
            await call('title_add', {
                'path': path, 'start': 0.1,
                'style': {'content': 'MCP title', 'color': '#ffcc00'},
                'duration': 1.0})
            await call('clips_split', {'path': path, 'clipIds': [clip['createdId']],
                                       'time': 1.5})
            await call('clip_update', {'path': path, 'clipId': clip['createdId'],
                                       'patch': {'opacity': 0.9}})
            await call('effect_layer_add', {'path': path, 'effectId': 'blur',
                                            'name': 'MCP blur', 'start': 0.2,
                                            'duration': 0.5})
            timeline = await call('timeline_read', {'path': path})
            print('timeline tracks:', len(timeline['tracks']),
                  'captions:', len(timeline['captions']))
            saved = await call('project_save', {'path': path})
            print('saved duration:', saved['duration'])

            # preview returns an Image when output is omitted
            preview = await session.call_tool('preview_frame', {'path': path, 'time': 0.6})
            print('preview content types:', [c.type for c in preview.content])
            assert any(c.type == 'image' for c in preview.content)

            job = await call('export_start', {
                'path': path, 'output': os.path.join(TMP, 'mcp_out.mp4'),
                'width': 480, 'height': 270, 'crf': 30, 'preset': 'ultrafast'})
            status = await call('export_wait', {'job': job['job'],
                                                'timeoutSeconds': 120})
            print('export:', status['state'], status.get('error'))
            assert status['state'] == 'done'
            await call('edit_undo', {'path': path})
            await call('edit_redo', {'path': path})
            listing = await call('project_list', {})
            print('projects:', len(listing['projects']))
            print('PASS: MCP editor smoke test')
            shutil.rmtree(TMP, ignore_errors=True)


if __name__ == '__main__':
    asyncio.run(main())
