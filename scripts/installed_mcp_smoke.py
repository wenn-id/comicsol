"""MCP protocol smoke against an installed ``comic-sol`` executable."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


EXPECTED_TOOLS = {
    "comic_init",
    "comic_status",
    "comic_validate",
    "comic_resume_plan",
    "comic_transition",
    "comic_record_attempt",
    "comic_promote_attempt",
    "comic_override_panel",
    "comic_record_stage",
    "comic_letter",
    "comic_compose",
    "comic_export",
    "comic_render_report",
    "comic_finalize",
    "comic_invalidate",
    "comic_resume",
    "comic_doctor",
}


async def smoke(executable: Path, output_root: Path) -> None:
    server = StdioServerParameters(
        command=str(executable),
        args=["mcp", "--root", str(output_root.resolve())],
    )
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            if names != EXPECTED_TOOLS:
                missing = sorted(EXPECTED_TOOLS - names)
                extra = sorted(names - EXPECTED_TOOLS)
                raise RuntimeError(f"MCP tool mismatch: missing={missing}, extra={extra}")
            doctor = await session.call_tool("comic_doctor", {})
            if getattr(doctor, "isError", getattr(doctor, "is_error", False)):
                raise RuntimeError("installed MCP doctor failed")
    print(f"mcp-smoke-ok: {len(EXPECTED_TOOLS)} tools")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    arguments = parser.parse_args()
    asyncio.run(smoke(arguments.executable, arguments.output_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
