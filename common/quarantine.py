"""Untrusted workload telemetry quarantine and sanitization.

Mitigates indirect prompt injection from pod logs, trace span attributes, and
event messages by neutralizing escape sequences, escaping closing delimiter tags,
and wrapping raw text in explicit structural XML tags.
"""

from __future__ import annotations

import re

# Match ANSI escape sequences
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
# Match any case-insensitive telemetry_data tag with arbitrary spacing
_TAG_INJECTION_PATTERN = re.compile(r"<\s*/?\s*telemetry_data\b[^>]*>", re.IGNORECASE)


def sanitize_telemetry_text(text: str) -> str:
    """Strip ANSI escapes and escape closing telemetry tags to prevent tag injection."""
    if not text:
        return ""
    # Remove terminal escape sequences
    cleaned = _ANSI_ESCAPE_PATTERN.sub("", text)
    # Neutralize any attempts to close or open the telemetry_data or wrapper tags
    cleaned = _TAG_INJECTION_PATTERN.sub(
        lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), cleaned
    )
    return cleaned


def _escape_attr(val: str) -> str:
    """Escape XML attribute values."""
    return (
        val.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    )


def wrap_untrusted_telemetry(
    content: str,
    *,
    source: str,
    service: str | None = None,
    namespace: str | None = None,
) -> str:
    """Wrap untrusted telemetry content into explicit structural XML tags."""
    sanitized = sanitize_telemetry_text(content)
    attrs = [f'source="{_escape_attr(source)}"', 'untrusted="true"']
    if service:
        attrs.append(f'service="{_escape_attr(service)}"')
    if namespace:
        attrs.append(f'namespace="{_escape_attr(namespace)}"')
    attr_str = " ".join(attrs)
    return f"<telemetry_data {attr_str}>\n{sanitized}\n</telemetry_data>"
