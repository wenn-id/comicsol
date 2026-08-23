"""Fail-closed runtime hardening audit for the Comic Sol OCI image.

Runs the image under the full hardening set that ``compose.yaml`` applies —
read-only root filesystem, no network, every capability dropped, a
process-count limit, ``no-new-privileges``, an init process, and only the
minimum writable mounts (the ``/data`` volume and a ``/tmp`` tmpfs) — and
asserts the effective runtime policy:

* the engine's default seccomp profile is active (never ``unconfined``) and
  the container itself runs in seccomp filter mode;
* the image and the live process use the fixed numeric ``10001:10001``
  identity and hold no effective capabilities;
* the root filesystem refuses writes and no network interface beyond the
  loopback device exists;
* the ``doctor`` and MCP stdio surfaces still work under those constraints,
  with the reported CLI version matching the canonical release version.

Every check is fail-closed: the first violation raises and exits non-zero.
"""

from __future__ import annotations

import argparse
import errno
import json
import re
import subprocess
import sys

EXPECTED_UID = 10001
EXPECTED_GID = 10001
DEFAULT_PIDS_LIMIT = 64
MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_INITIALIZE_REQUEST = (
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{'
    '"protocolVersion":"' + MCP_PROTOCOL_VERSION + '","capabilities":{},'
    '"clientInfo":{"name":"container-runtime-audit","version":"1"}}}\n'
)


def _run(
    command: list[str], *, stdin: bytes | None = None, timeout: int = 600
) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        command,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    return completed


def _probe_args(docker: str, image: str, pids_limit: int) -> list[str]:
    """docker run arguments mirroring compose.yaml's hardening block."""

    return [
        docker,
        "run",
        "--rm",
        "--init",
        "--read-only",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--pids-limit",
        str(pids_limit),
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp",
    ]


def _python_probe(
    docker: str, image: str, pids_limit: int, code: str
) -> subprocess.CompletedProcess:
    return _run(
        [*_probe_args(docker, image, pids_limit), "--entrypoint", "python", image, "-c", code]
    )


def check_engine_seccomp_default(docker: str) -> None:
    completed = _run([docker, "info", "--format", "{{.SecurityOptions}}"])
    if completed.returncode != 0:
        raise RuntimeError("docker info failed:\n" + completed.stderr.decode("utf-8", "replace"))
    # Engines render the option list with either spaces or semicolons/commas
    # between the name=.../profile=... tokens; normalize all three.
    tokens = [
        token
        for token in re.split(
            r"[\s;,]+", completed.stdout.decode("utf-8", "replace").strip("[] \n")
        )
        if token
    ]
    if "name=seccomp" not in tokens:
        raise RuntimeError(f"engine does not enforce a seccomp profile: {' '.join(tokens)!r}")
    index = tokens.index("name=seccomp")
    profile = next(
        (token for token in tokens[index + 1 : index + 3] if token.startswith("profile=")),
        None,
    )
    if profile not in ("profile=default", "profile=builtin"):
        raise RuntimeError(
            f"engine seccomp profile is not the default/builtin profile: {' '.join(tokens)!r}"
        )
    print(f"engine-seccomp: {profile} active")


def check_image_user(docker: str, image: str) -> None:
    completed = _run([docker, "image", "inspect", "--format", "{{.Config.User}}", image])
    if completed.returncode != 0:
        raise RuntimeError(
            f"docker image inspect failed:\n{completed.stderr.decode('utf-8', 'replace')}"
        )
    user = completed.stdout.decode("utf-8").strip()
    if user != f"{EXPECTED_UID}:{EXPECTED_GID}":
        raise RuntimeError(f"image Config.User is {user!r}, expected {EXPECTED_UID}:{EXPECTED_GID}")
    print(f"image-user: {EXPECTED_UID}:{EXPECTED_GID}")


def check_cli_version(docker: str, image: str, pids_limit: int, expected: str) -> None:
    completed = _run(
        [*_probe_args(docker, image, pids_limit), "--entrypoint", "comic-sol", image, "--version"]
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "comic-sol --version failed under hardening:\n"
            + completed.stderr.decode("utf-8", "replace")
        )
    reported = completed.stdout.decode("utf-8").strip()
    if reported != f"comic-sol {expected}":
        raise RuntimeError(f"image reports version {reported!r}, expected 'comic-sol {expected}'")
    print(f"cli-version: comic-sol {expected}")


def check_runtime_identity(docker: str, image: str, pids_limit: int) -> None:
    completed = _python_probe(
        docker, image, pids_limit, "import os; print(os.getuid(), os.getgid())"
    )
    if completed.returncode != 0:
        raise RuntimeError("identity probe failed:\n" + completed.stderr.decode("utf-8", "replace"))
    identity = completed.stdout.decode("utf-8").split()
    if identity != [str(EXPECTED_UID), str(EXPECTED_GID)]:
        raise RuntimeError(
            f"runtime identity is {identity!r}, expected [{EXPECTED_UID}, {EXPECTED_GID}]"
        )
    print(f"runtime-identity: uid={EXPECTED_UID} gid={EXPECTED_GID} (non-root)")


def check_proc_status_policy(docker: str, image: str, pids_limit: int) -> None:
    completed = _python_probe(
        docker,
        image,
        pids_limit,
        "print(open('/proc/self/status').read())",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "/proc/self/status probe failed:\n" + completed.stderr.decode("utf-8", "replace")
        )
    status = {}
    for line in completed.stdout.decode("utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            status[key] = value.strip()
    cap_eff = status.get("CapEff", "")
    if not re.fullmatch(r"0+", cap_eff):
        raise RuntimeError(f"process holds effective capabilities: CapEff={cap_eff}")
    if status.get("NoNewPrivs") != "1":
        raise RuntimeError(
            f"no-new-privileges is not enforced: NoNewPrivs={status.get('NoNewPrivs')!r}"
        )
    if status.get("Seccomp") != "2":
        raise RuntimeError(
            f"container is not in seccomp filter mode: Seccomp={status.get('Seccomp')!r}"
        )
    print("proc-status: CapEff=0 NoNewPrivs=1 Seccomp=2 (filter mode)")


def check_pids_limit(docker: str, image: str, pids_limit: int) -> None:
    completed = _python_probe(
        docker, image, pids_limit, "print(open('/sys/fs/cgroup/pids.max').read().strip())"
    )
    if completed.returncode != 0:
        raise RuntimeError("pids.max probe failed:\n" + completed.stderr.decode("utf-8", "replace"))
    observed = completed.stdout.decode("utf-8").strip()
    if observed != str(pids_limit):
        raise RuntimeError(f"pids limit is {observed!r}, expected {pids_limit}")
    print(f"pids-limit: {pids_limit}")


def check_read_only_rootfs(docker: str, image: str, pids_limit: int) -> None:
    completed = _python_probe(
        docker,
        image,
        pids_limit,
        "try:\n"
        "    open('/.write-probe', 'w').close()\n"
        "except OSError as error:\n"
        "    print(error.errno)\n"
        "else:\n"
        "    raise SystemExit(3)\n",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "root filesystem write probe did not fail closed:\n"
            + completed.stdout.decode("utf-8", "replace")
            + completed.stderr.decode("utf-8", "replace")
        )
    errno_reported = completed.stdout.decode("utf-8").strip()
    if errno_reported != str(errno.EROFS):
        raise RuntimeError(
            f"root filesystem write failed with errno {errno_reported}, expected EROFS ({errno.EROFS})"
        )
    print("read-only-rootfs: write probe denied with EROFS")


def check_no_network(docker: str, image: str, pids_limit: int) -> None:
    completed = _python_probe(
        docker, image, pids_limit, "import os; print(sorted(os.listdir('/sys/class/net')))"
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "network interface probe failed:\n" + completed.stderr.decode("utf-8", "replace")
        )
    interfaces = completed.stdout.decode("utf-8").strip()
    if interfaces != "['lo']":
        raise RuntimeError(
            f"network interfaces are {interfaces}, expected only the loopback device"
        )
    print("network: none (loopback device only)")


def check_doctor(docker: str, image: str, pids_limit: int) -> None:
    completed = _run(
        [
            *_probe_args(docker, image, pids_limit),
            "--entrypoint",
            "comic-sol",
            image,
            "doctor",
            "--output-root",
            "/data/doctor",
        ]
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "doctor failed under full hardening:\n"
            + completed.stdout.decode("utf-8", "replace")
            + completed.stderr.decode("utf-8", "replace")
        )
    print("doctor: ok under read-only rootfs, no network, and dropped capabilities")


def check_mcp_handshake(docker: str, image: str, pids_limit: int) -> None:
    command = [
        *_probe_args(docker, image, pids_limit),
        # stdin must reach the stdio server for the handshake to happen at all
        "-i",
        "--entrypoint",
        "comic-sol",
        image,
        "mcp",
        "--root",
        "/data",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(MCP_INITIALIZE_REQUEST.encode("utf-8"), timeout=300)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError("MCP initialize handshake timed out") from None
    if process.returncode != 0:
        raise RuntimeError(
            f"MCP server exited with {process.returncode} after the handshake:\n"
            + stderr.decode("utf-8", "replace")
        )
    response = None
    for line in stdout.decode("utf-8").splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        if message.get("id") == 1:
            response = message
            break
    if response is None or "error" in response:
        raise RuntimeError(
            "MCP initialize handshake returned no successful response:\n"
            + stdout.decode("utf-8", "replace")
        )
    server_info = response.get("result", {}).get("serverInfo", {})
    if not server_info.get("name"):
        raise RuntimeError(f"MCP response has no serverInfo name: {response}")
    print(f"mcp: initialize handshake ok (server: {server_info['name']})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="image reference to audit")
    parser.add_argument(
        "--expect-version",
        required=True,
        help="canonical release version the image must report",
    )
    parser.add_argument("--docker", default="docker", help="docker CLI to invoke")
    parser.add_argument(
        "--pids-limit",
        type=int,
        default=DEFAULT_PIDS_LIMIT,
        help=f"expected process-count limit (default: {DEFAULT_PIDS_LIMIT})",
    )
    parsed = parser.parse_args()

    check_engine_seccomp_default(parsed.docker)
    check_image_user(parsed.docker, parsed.image)
    check_cli_version(parsed.docker, parsed.image, parsed.pids_limit, parsed.expect_version)
    check_runtime_identity(parsed.docker, parsed.image, parsed.pids_limit)
    check_proc_status_policy(parsed.docker, parsed.image, parsed.pids_limit)
    check_pids_limit(parsed.docker, parsed.image, parsed.pids_limit)
    check_read_only_rootfs(parsed.docker, parsed.image, parsed.pids_limit)
    check_no_network(parsed.docker, parsed.image, parsed.pids_limit)
    check_doctor(parsed.docker, parsed.image, parsed.pids_limit)
    check_mcp_handshake(parsed.docker, parsed.image, parsed.pids_limit)
    print(
        "container-audit-ok: engine seccomp, image user, CLI version, runtime "
        "identity, capabilities, seccomp mode, pids limit, read-only rootfs, "
        "no network, doctor, and MCP verified"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
