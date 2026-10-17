#!/usr/bin/env python3
"""Security primitives: subprocess sanitization and secret detection."""

from __future__ import annotations

import re


# All subprocess invocations use argument arrays, never a shell.
# Shell punctuation in PR bodies, GraphQL queries and filenames is literal text.
SHELL_META = re.compile(r"\x00")

SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,255}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,255}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[A-Za-z0-9\-_]{35}"),
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?:api[_-]?key|secret|token|password|credential|auth)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9\-_./+]{16,}['\"]?"
    ),
]


def validate_subprocess_args(args: list[str]) -> list[str]:
    """Reject arguments containing shell metacharacters.

    Returns the argument list unchanged when safe.
    Raises ``ValueError`` if any argument contains dangerous characters.
    """
    for i, arg in enumerate(args):
        if SHELL_META.search(arg):
            raise ValueError(
                f"Argument {i} contains shell metacharacter: {arg!r}"
            )
    return args


def contains_secret(text: str) -> list[str]:
    """Return descriptions of detected secret patterns, empty if none found."""
    findings: list[str] = []
    for pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            snippet = match.group()[:12] + "..."
            findings.append(f"pattern={pattern.pattern[:40]}... match={snippet}")
    return findings


def sanitize_external_text(text: str) -> str:
    """Strip control characters from untrusted external text (PR comments, logs).

    Preserves newlines, tabs, and printable content.
    """
    cleaned: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in ("\n", "\r", "\t"):
            cleaned.append(ch)
        elif code < 0x20 or (0x7F <= code <= 0x9F):
            continue
        else:
            cleaned.append(ch)
    return "".join(cleaned)
