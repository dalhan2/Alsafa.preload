"""End-to-end smoke test: spawn the server over stdio, list tools, call doctor."""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.environ.get('CAPCUT_PYTHON') or sys.executable


async def main():
    params = StdioServerParameters(command=PY, args=[os.path.join(HERE, 'server.py')],
                                   cwd=HERE)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print('TOOLS:', [t.name for t in tools.tools])
            result = await session.call_tool('doctor', {})
            print('DOCTOR is_error:', result.is_error)
            print('DOCTOR content:', result.content)
            if result.structured_content:
                print('DOCTOR structured:', result.structured_content)


if __name__ == '__main__':
    asyncio.run(main())
