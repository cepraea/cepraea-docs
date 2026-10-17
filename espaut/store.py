#!/usr/bin/env python3
"""Persistence layer — .espaut/ directory management and JSON/JSONL I/O."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


STORE_DIR_NAME = ".espaut"

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "0.1.0",
    "trigger_mode": "explicit",
    "preservation_events": ["explicit"],
    "eligible_patterns": [],
    "ai_enabled": False,
    "ai_sharing_authorized": False,
    "auto_commit": False,
    "resolver_roles": ["author", "assigned_reviewers"],
    "merge_policy": "manual",
    "correction_max_attempts": 3,
    "correction_test_command": [],
    "retention_policy": "retain_all",
    "ci_required": False,
}


def find_store(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: cwd) looking for a ``.espaut/`` directory."""
    current = (start or Path.cwd()).resolve()
    for ancestor in [current, *current.parents]:
        candidate = ancestor / STORE_DIR_NAME
        if candidate.is_dir():
            return candidate
    return None


def require_store(start: Path | None = None) -> Path:
    """Like :func:`find_store` but raises if not found."""
    store = find_store(start)
    if store is None:
        raise FileNotFoundError(
            "No .espaut/ directory found. Run 'espaut init' first."
        )
    return store


def init_store(root: Path) -> Path:
    """Create the ``.espaut/`` directory tree with default config.

    Returns the path to the created store directory.
    """
    store = root / STORE_DIR_NAME
    store.mkdir(exist_ok=True)
    (store / "tasks").mkdir(exist_ok=True)
    (store / "evidence").mkdir(exist_ok=True)
    (store / "decisions").mkdir(exist_ok=True)
    (store / "cache").mkdir(exist_ok=True)

    config_path = store / "config.json"
    if not config_path.exists():
        write_json(config_path, DEFAULT_CONFIG)

    events_path = store / "events.jsonl"
    if not events_path.exists():
        events_path.write_text("", encoding="utf-8", newline="\n")

    inner_gitignore = store / ".gitignore"
    if not inner_gitignore.exists():
        inner_gitignore.write_text("*\n", encoding="utf-8", newline="\n")

    return store


def new_id(prefix: str = "") -> str:
    """Generate a short unique identifier with an optional prefix."""
    short = uuid.uuid4().hex[:12]
    return f"{prefix}{short}" if prefix else short


# -- JSON I/O ----------------------------------------------------------------

def read_json(path: Path) -> Any:
    """Read and parse a JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    """Atomically write *value* as formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(tmp, path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read all records from a JSONL file."""
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append a single JSON record to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Atomically write a list of records as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    rendered = "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records
    )
    tmp.write_text(rendered, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def load_config(store: Path) -> dict[str, Any]:
    """Load the config.json from a store, falling back to defaults."""
    config_path = store / "config.json"
    if config_path.exists():
        saved = read_json(config_path)
        merged = dict(DEFAULT_CONFIG)
        merged.update(saved)
        return merged
    return dict(DEFAULT_CONFIG)
