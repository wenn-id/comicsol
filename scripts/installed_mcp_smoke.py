"""MCP protocol smoke against an installed ``comic-sol`` executable."""

from __future__ import annotations

import argparse
import asyncio
import json

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


async def smoke(command: str, arguments: list[str]) -> None:
    """Run the installed MCP server smoke test."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server = StdioServerParameters(command=command, args=arguments)
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


def parse_server_entry(command: str, arguments_json: str) -> tuple[str, list[str]]:
    """Parse one structured MCP command entry without invoking a shell."""
    arguments = json.loads(arguments_json)
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise ValueError("MCP arguments must be a JSON string array")
    return command, arguments


def main() -> int:
    """Execute the installed MCP smoke-test command."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--args-json", required=True)
    parsed = parser.parse_args()
    command, arguments = parse_server_entry(parsed.command, parsed.args_json)
    asyncio.run(smoke(command, arguments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
