"""Capture tool schemas and the initialize response from the pinned MCP SDK."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def request(process, method, params=None, request_id=None):
    message = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    return json.loads(process.stdout.readline())["result"] if request_id is not None else None


def main() -> None:
    with tempfile.TemporaryDirectory() as root, subprocess.Popen(
        [sys.executable, "-m", "comic_sol_product.cli", "mcp", "--root", root],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as process:
        initialize = request(process, "initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "capture", "version": "1"},
        }, 0)
        request(process, "notifications/initialized")
        tools = request(process, "tools/list", {}, 1)["tools"]
        process.stdin.close()
        if process.wait(timeout=10) != 0:
            raise RuntimeError("Python MCP runtime did not exit cleanly")
    print(json.dumps({"initialize": initialize, "tools": tools}, indent=2))


if __name__ == "__main__":
    main()
