"""Validate HTTP bearer configuration before handing authentication to the SDK."""

from __future__ import annotations

import os
import re


def validate_http_auth() -> None:
    """Reject malformed or empty bearer configuration without printing credentials."""
    raw = os.environ.get("VGI_BEARER_TOKENS")
    if raw is None:
        return
    seen: set[str] = set()
    for entry in raw.split(","):
        token, separator, principal = entry.strip().partition("=")
        if (
            not separator
            or re.fullmatch(r"[A-Za-z0-9._~+/\-]{32,4096}", token) is None
            or not principal
            or len(principal) > 256
            or any(char.isspace() or not char.isprintable() for char in principal)
            or token in seen
        ):
            raise ValueError(
                "Invalid VGI_BEARER_TOKENS: use unique token=principal pairs separated by commas, "
                "with nonempty principals and 32-4096 character bearer tokens containing no spaces, "
                "commas, or '='"
            )
        seen.add(token)
