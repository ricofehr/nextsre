"""Host shell-exec MCP server (Operator tool surface) + command harness.

Harness rules (all enforced in-tool, before anything runs):

* binary allowlist - only vetted binaries may be invoked
* substring denylist - blocks obviously destructive patterns
* per-binary verb limits - e.g. systemctl may status/restart/reload but not
  stop/disable/mask
* hard timeout + truncated output

DEMO mode returns canned output for allowed commands so the pipeline runs
without a host to shell into. Real mode runs the command via ``subprocess``.
"""

from __future__ import annotations

import os
import shlex
import subprocess

from mcp.server.fastmcp import FastMCP

DEMO = os.environ.get("DEMO_MODE", "1") == "1"
mcp = FastMCP("shell-exec", host="0.0.0.0", port=int(os.environ.get("PORT", "8202")))

ALLOWED_BINARIES = {"kubectl", "systemctl", "journalctl", "df", "free", "ss", "dig", "uptime"}
DENY_SUBSTRINGS = ("rm -rf", "mkfs", "dd ", ":(){", "shutdown", "reboot", "> /dev", "chmod -R")
ALLOWED_SYSTEMCTL_VERBS = {"status", "restart", "reload", "is-active"}

_DEMO_OUTPUT = {
    "uptime": " 14:02:11 up 9 days,  3:21,  2 users,  load average: 0.42, 0.51, 0.55",
    "df": "Filesystem     1K-blocks      Used Available Use% Mounted on\n/dev/root       61252420  31204512  27420112  54% /",
    "free": "               total        used        free\nMem:        16384000     9120000     2100000",
    "systemctl": "Active: active (running)",
}


def _harness_check(command: str) -> list[str]:
    """Raise PermissionError if the command violates policy; else return argv."""
    if any(bad in command for bad in DENY_SUBSTRINGS):
        raise PermissionError("command matches a denied pattern")
    argv = shlex.split(command)
    if not argv:
        raise ValueError("empty command")
    if argv[0] not in ALLOWED_BINARIES:
        raise PermissionError(
            f"binary '{argv[0]}' is not on the allowlist {sorted(ALLOWED_BINARIES)}"
        )
    if argv[0] == "systemctl" and len(argv) > 1 and argv[1] not in ALLOWED_SYSTEMCTL_VERBS:
        raise PermissionError(f"systemctl verb '{argv[1]}' is not permitted")
    return argv


@mcp.tool()
def run_command(command: str, timeout_s: int = 20) -> dict:
    """Run a vetted diagnostic/remediation shell command under the harness."""
    argv = _harness_check(command)

    if not DEMO:  # pragma: no cover
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        return {
            "command": command,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-2000:],
        }

    out = _DEMO_OUTPUT.get(argv[0], f"(demo) executed: {command}")
    return {"command": command, "exit_code": 0, "stdout": out, "stderr": ""}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
