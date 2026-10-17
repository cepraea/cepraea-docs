#!/usr/bin/env python3
"""Tests for security primitives."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))

from espaut.security import contains_secret, sanitize_external_text, validate_subprocess_args


def test_safe_args() -> dict[str, str]:
    args = ["git", "status", "--porcelain"]
    result = validate_subprocess_args(args)
    return {"passthrough": "PASS" if result == args else "FAIL"}


def test_metachar_rejected() -> dict[str, str]:
    cases = {
        "semicolon": ["git", "status; rm -rf /"],
        "pipe": ["git", "log | cat"],
        "backtick": ["git", "`whoami`"],
        "dollar": ["git", "$(id)"],
        "ampersand": ["git", "status & echo pwned"],
    }
    checks: dict[str, str] = {}
    for name, args in cases.items():
        try:
            validate_subprocess_args(args)
            checks[name] = "PASS"
        except ValueError:
            checks[name] = "FAIL"
    return checks


def test_github_token_detected() -> dict[str, str]:
    text = "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn"
    findings = contains_secret(text)
    return {"github_token": "PASS" if findings else "FAIL"}


def test_aws_key_detected() -> dict[str, str]:
    text = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE"
    findings = contains_secret(text)
    return {"aws_key": "PASS" if findings else "FAIL"}


def test_private_key_detected() -> dict[str, str]:
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
    findings = contains_secret(text)
    return {"private_key": "PASS" if findings else "FAIL"}


def test_generic_secret_detected() -> dict[str, str]:
    text = 'api_key = "sk-proj-abcdefghij1234567890"'
    findings = contains_secret(text)
    return {"generic_secret": "PASS" if findings else "FAIL"}


def test_clean_text_no_detection() -> dict[str, str]:
    text = "This is a normal PR comment with no secrets."
    findings = contains_secret(text)
    return {"no_false_positive": "PASS" if not findings else "FAIL"}


def test_sanitize_strips_control() -> dict[str, str]:
    text = "hello\x00world\x07test\ttab\nnewline"
    cleaned = sanitize_external_text(text)
    return {
        "null_removed": "PASS" if "\x00" not in cleaned else "FAIL",
        "bell_removed": "PASS" if "\x07" not in cleaned else "FAIL",
        "tab_kept": "PASS" if "\t" in cleaned else "FAIL",
        "newline_kept": "PASS" if "\n" in cleaned else "FAIL",
        "content_preserved": "PASS" if "hello" in cleaned and "world" in cleaned else "FAIL",
    }


def test_sanitize_unicode() -> dict[str, str]:
    text = "café résumé 日本語"
    cleaned = sanitize_external_text(text)
    return {"unicode_preserved": "PASS" if cleaned == text else "FAIL"}


ALL_TESTS = [
    ("safe_args", test_safe_args),
    ("metachar_rejected", test_metachar_rejected),
    ("github_token_detected", test_github_token_detected),
    ("aws_key_detected", test_aws_key_detected),
    ("private_key_detected", test_private_key_detected),
    ("generic_secret_detected", test_generic_secret_detected),
    ("clean_text_no_detection", test_clean_text_no_detection),
    ("sanitize_strips_control", test_sanitize_strips_control),
    ("sanitize_unicode", test_sanitize_unicode),
]


def main() -> int:
    total_pass = 0
    total_checks = 0

    for name, fn in ALL_TESTS:
        try:
            checks = fn()
        except Exception as exc:
            print(f"  [FAIL] {name} — exception: {exc}")
            total_checks += 1
            continue

        for sub, status in checks.items():
            total_checks += 1
            if status == "PASS":
                total_pass += 1
            print(f"  [{status}] {name}.{sub}")

    print(f"\nsecurity: {total_pass}/{total_checks} passed")
    return 0 if total_pass == total_checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
