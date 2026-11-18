"""Tiny JSON-file store for plans and jobs (atomic tmp -> replace writes, one process-wide lock)."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import JOBS_DIR, PLANS_DIR

LOCK = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(kind: str, ident: str) -> Path:
    if not ident or any(c not in "0123456789abcdef-" for c in ident.lower()):
        raise KeyError(ident)
    return (PLANS_DIR if kind == "plan" else JOBS_DIR) / f"{ident}.json"


def load(kind: str, ident: str) -> dict | None:
    try:
        p = _path(kind, ident)
    except KeyError:
        return None
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save(kind: str, record: dict) -> dict:
    record["updated_at"] = now()
    p = _path(kind, record["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    return record


def list_all(kind: str) -> list[dict]:
    d = PLANS_DIR if kind == "plan" else JOBS_DIR
    items = []
    for p in d.glob("*.json"):
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    items.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return items
