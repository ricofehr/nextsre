"""Host shell-exec MCP server (Operator tool surface) + command harness.

This harness is the weakest point of the whole privilege boundary, because an
under-constrained binary here is a way around every other guardrail in the repo:
``kubectl delete``/``apply``/``scale`` sidestep the namespace allowlist and
replica cap in ``k8s_write.py``, and ``kubectl exec -- sh -c ...`` sidesteps the
binary allowlist itself. Mutating Kubernetes actions must go through
``k8s_write.py``, where they are guarded and audited.

Rules, all enforced before anything runs:

* binary allowlist - only vetted binaries, and never path-qualified
* verb at a fixed position - for binaries that take a subcommand, ``argv[1]``
  must be the verb and must not be a flag. Scanning for "the first non-flag
  token" instead is unsafe: kubectl global flags take values, so
  ``kubectl --cache-dir get delete ns kube-system`` would present ``get`` as the
  verb while kubectl parses ``--cache-dir=get`` and runs the delete.
* denied tokens anywhere in argv - mutating subcommands, secret material, and
  the destructive flags of the binaries that take no verb (``journalctl
  --vacuum-*``, ``ss -K``). Position-independent, so it holds even if the verb
  check is ever fooled again.
* bounded timeout - ``timeout_s`` reaches us from an LLM
* a minimal environment for the child - the server's own env holds credentials

DEMO mode returns canned output for allowed commands so the pipeline runs without
a host to shell into. Real mode runs the command via ``subprocess`` - no shell,
so the argv is never re-parsed by sh.
"""

from __future__ import annotations

import os
import shlex
import subprocess

from mcp.server.fastmcp import FastMCP

from common.env import env_flag
from common.serving import bind_host, port, transport_security

mcp = FastMCP(
    "shell-exec",
    host=bind_host(),
    port=port(default=8202),
    transport_security=transport_security("shell-exec"),
)

ALLOWED_BINARIES = {"kubectl", "systemctl", "journalctl", "df", "free", "ss", "dig", "uptime"}

# Binaries that take a subcommand. argv[1] must be one of these.
# kubectl is confined to reads; `systemctl restart|reload` is deliberate - host
# service remediation is what the Operator is for - so this is a non-destructive
# allowlist, not a read-only one.
ALLOWED_VERBS = {
    "kubectl": {
        "get",
        "describe",
        "logs",
        "top",
        "version",
        "explain",
        "api-resources",
        "api-versions",
        "cluster-info",
    },
    "systemctl": {"status", "is-active", "restart", "reload"},
}

# Rejected wherever they appear, flag or positional. Defence in depth behind the
# verb check.
DENIED_TOKENS = {
    # kubectl mutations - never permitted here; k8s_write.py owns these
    "annotate",
    "apply",
    "attach",
    "auth",
    "cordon",
    "cp",
    "create",
    "delete",
    "drain",
    "edit",
    "exec",
    "label",
    "patch",
    "port-forward",
    "proxy",
    "replace",
    "rollout",
    "run",
    "scale",
    "set",
    "taint",
    "uncordon",
    # cluster secret material
    "secret",
    "secrets",
    # kubectl escape hatches: arbitrary API paths, dump's file-write target, and
    # -f/-k, which take a possibly remote manifest naming the resources to act on
    "--raw",
    "dump",
    "--output-directory",
    "-f",
    "--filename",
    "-k",
    "--kustomize",
    # kubectl identity and endpoint redirection: --as escalates through the
    # Operator's own RBAC, --server/--kubeconfig point it at an attacker's
    # cluster and hand over the bearer token
    "--as",
    "--as-group",
    "--as-uid",
    "--server",
    "--kubeconfig",
    "--token",
    "--client-key",
    "--client-certificate",
    "--insecure-skip-tls-verify",
    "--user",
    "--context",
    "--cluster",
    "--username",
    "--password",
    # systemctl mutations outside the verb allowlist, and remote/root dispatch
    "disable",
    "kill",
    "mask",
    "stop",
    "-H",
    "--host",
    "-M",
    "--machine",
    "--root",
    # journalctl destructive and arbitrary-path flags (it takes no verb)
    "--vacuum-time",
    "--vacuum-size",
    "--vacuum-files",
    "--rotate",
    "--flush",
    "--relinquish-var",
    "--smart-relinquish-var",
    "--sync",
    "--update-catalog",
    "--setup-keys",
    "-D",
    "--directory",
    "--file",
}

# Binaries that take no verb need a flag *allowlist* rather than a denylist:
# short flags cluster, so denying "-K" leaves "-Kn", "-nK" and "--kill" open and
# every bypass is one character away. Anything not listed here is refused.
ALLOWED_FLAGS = {
    "ss": {
        "-t",
        "-u",
        "-a",
        "-n",
        "-p",
        "-s",
        "-l",
        "-i",
        "-e",
        "-m",
        "-o",
        "-4",
        "-6",
        "-x",
        "-r",
        "--tcp",
        "--udp",
        "--all",
        "--numeric",
        "--processes",
        "--summary",
        "--listening",
        "--info",
        "--extended",
        "--memory",
        "--options",
    },
    "df": {
        "-h",
        "-m",
        "-P",
        "-T",
        "-i",
        "-l",
        "-a",
        "--human-readable",
        "--si",
        "--portability",
        "--print-type",
        "--inodes",
        "--local",
        "--all",
        "--total",
        "--output",
    },
    "free": {
        "-b",
        "-m",
        "-g",
        "-h",
        "-t",
        "-w",
        "--bytes",
        "--mega",
        "--giga",
        "--human",
        "--total",
        "--wide",
        "--si",
    },
    "dig": {
        "-4",
        "-6",
        "-x",
        "-t",
        "-q",
    },
    "uptime": {
        "-p",
        "-s",
        "--pretty",
        "--since",
    },
}

# kubectl resource arguments arrive as TYPE, TYPE/NAME or a comma-joined list, and
# may carry a .group suffix. Splitting on those before matching stops
# `secret/db-creds`, `pod,secret` and `secrets.v1.` slipping a denied word past.
_RESOURCE_SEPARATORS = ("/", ",", ".")

MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 60

# The server's own environment carries LLM_API_KEY and any cluster credentials;
# the child needs none of it.
_BASE_CHILD_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"}
# kubectl cannot reach a cluster without these, so they are forwarded by name
# rather than by inheriting the whole environment.
_PASSTHROUGH_ENV = ("HOME", "KUBECONFIG", "KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT")

_DEMO_OUTPUT = {
    "uptime": " 14:02:11 up 9 days,  3:21,  2 users,  load average: 0.42, 0.51, 0.55",
    "df": (
        "Filesystem     1K-blocks      Used Available Use% Mounted on\n"
        "/dev/root       61252420  31204512  27420112  54% /"
    ),
    "free": (
        "               total        used        free\n"
        "Mem:        16384000     9120000     2100000"
    ),
    "systemctl": "Active: active (running)",
}


def clamp_timeout(timeout_s: int) -> int:
    return max(MIN_TIMEOUT_S, min(timeout_s, MAX_TIMEOUT_S))


def _child_env() -> dict[str, str]:
    """A minimal environment: the server's own holds LLM and cluster credentials."""
    env = dict(_BASE_CHILD_ENV)
    for name in _PASSTHROUGH_ENV:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def _fragments(token: str) -> list[str]:
    """The parts of a token that must each clear the denylist.

    Drops any ``=value`` suffix (``--vacuum-time=1s``), extracts the leading
    short option when a value is attached without a space
    (``-fhttps://evil/x.yaml``, ``-Huser@remote``, ``-Mother``), and splits
    kubectl resource syntax (``secret/db-creds``, ``pod,secret``,
    ``secrets.v1.``).
    """
    base = token.split("=", 1)[0]
    parts = [base]
    for separator in _RESOURCE_SEPARATORS:
        parts = [piece for part in parts for piece in part.split(separator)]
    result = [part for part in parts if part]
    if base.startswith("-") and not base.startswith("--") and len(base) > 2:
        result.append(base[:2])
    return result


def _is_short_flag(fragment: str) -> bool:
    return fragment.startswith("-") and not fragment.startswith("--")


def _denied(token: str) -> bool:
    """True if any fragment of ``token`` is denied.

    Short flags are matched case-sensitively (``-H``/``-M``/``-D`` are remote or
    directory dispatch, while ``-h``/``-m``/``-d`` are benign formatting flags);
    long flags, subcommands and resource names are matched both as written and
    case-folded.
    """
    return any(
        (
            fragment in DENIED_TOKENS
            if _is_short_flag(fragment)
            else (fragment in DENIED_TOKENS or fragment.lower() in DENIED_TOKENS)
        )
        for fragment in _fragments(token)
    )


def _expand_short_flags(token: str) -> list[str]:
    """``-tn`` -> ``["-t", "-n"]``; long flags and operands pass through unchanged."""
    if token.startswith("--") or not token.startswith("-") or token == "-":
        return [token]
    return [f"-{char}" for char in token[1:]]


def _guard_flags(binary: str, argv: list[str]) -> None:
    """For verb-less binaries, every flag must be on that binary's allowlist."""
    allowed = ALLOWED_FLAGS.get(binary)
    if allowed is None:
        return
    for token in argv[1:]:
        if not token.startswith("-"):
            continue
        for flag in _expand_short_flags(token.split("=", 1)[0]):
            if flag not in allowed:
                raise PermissionError(
                    f"{binary} flag '{flag}' is not permitted; allowed: {sorted(allowed)}"
                )


def _harness_check(command: str) -> list[str]:
    """Raise if the command violates policy; else return the argv to execute."""
    argv = shlex.split(command)
    if not argv:
        raise ValueError("empty command")

    binary = argv[0]
    if "/" in binary:
        raise PermissionError("binaries must be named, not path-qualified")
    if binary not in ALLOWED_BINARIES:
        raise PermissionError(
            f"binary '{binary}' is not on the allowlist {sorted(ALLOWED_BINARIES)}"
        )

    denied = [token for token in argv if _denied(token)]
    if denied:
        raise PermissionError(f"command contains denied token(s): {denied}")

    allowed_verbs = ALLOWED_VERBS.get(binary)
    if allowed_verbs is not None:
        if len(argv) < 2:
            raise PermissionError(f"'{binary}' requires one of {sorted(allowed_verbs)}")
        verb = argv[1]
        if verb.startswith("-"):
            raise PermissionError(
                f"'{binary}' must be followed directly by its verb, not by '{verb}'"
            )
        if verb not in allowed_verbs:
            raise PermissionError(
                f"{binary} verb '{verb}' is not permitted; allowed: {sorted(allowed_verbs)}"
            )

    _guard_flags(binary, argv)
    return argv


@mcp.tool()
def run_command(command: str, timeout_s: int = 20) -> dict:
    """Run a vetted, non-destructive diagnostic shell command under the harness."""
    argv = _harness_check(command)
    timeout = clamp_timeout(timeout_s)

    if not env_flag("DEMO_MODE", default=True):  # pragma: no cover - needs a real host
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, env=_child_env()
            )
        except subprocess.TimeoutExpired:
            # A structured result, not an exception: the caller is an LLM deciding
            # what to do next, and "the command timed out" is information.
            return {
                "command": command,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"timed out after {timeout}s",
            }
        return {
            "command": command,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-2000:],
        }

    out = _DEMO_OUTPUT.get(argv[0], f"(demo) executed: {command}")
    return {"command": command, "exit_code": 0, "stdout": out, "stderr": ""}


if __name__ == "__main__":
    env_flag("DEMO_MODE", default=True)  # fail fast on a malformed value
    mcp.run(transport="streamable-http")
