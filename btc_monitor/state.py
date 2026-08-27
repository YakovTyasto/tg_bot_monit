from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from .config import STATE_PATH

DEFAULT_STATE: dict[str, Any] = {
    "version": 1,
    "last_daily": None,
    "alerts": {
        "last_sent_at": None,
        "last_reference_price": None,
        "last_assessment": None,
        "sent": {},
    },
}


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return deepcopy(DEFAULT_STATE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return deepcopy(DEFAULT_STATE)
    state = deepcopy(DEFAULT_STATE)
    if not isinstance(raw, dict):
        return state
    alerts = state["alerts"]
    state.update(raw)
    state["alerts"] = alerts
    if isinstance(raw.get("alerts"), dict):
        state["alerts"].update(deepcopy(raw["alerts"]))
    if not isinstance(state["alerts"].get("sent"), dict):
        state["alerts"]["sent"] = {}
    return state


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
