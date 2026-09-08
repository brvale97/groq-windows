"""Small on-disk history of the most recent transcriptions.

Only the last few transcripts are kept, newest first, in a JSON file inside
the app data folder. Writes are atomic so a crash never leaves a broken file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MAX_HISTORY_ENTRIES = 20
MAX_ENTRY_CHARACTERS = 20_000


@dataclass(frozen=True)
class HistoryEntry:
    text: str
    created_at: float

    def label(self, now: float | None = None) -> str:
        """Human friendly timestamp such as ``Vandaag 14:02`` or ``ma 8 sep 14:02``."""
        moment = datetime.fromtimestamp(self.created_at)
        today = datetime.fromtimestamp(now if now is not None else time.time()).date()
        clock = moment.strftime("%H:%M")
        if moment.date() == today:
            return f"Vandaag {clock}"
        if (today - moment.date()).days == 1:
            return f"Gisteren {clock}"
        days = ("ma", "di", "wo", "do", "vr", "za", "zo")
        months = ("jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec")
        return f"{days[moment.weekday()]} {moment.day} {months[moment.month - 1]} {clock}"

    def preview(self, limit: int = 90) -> str:
        flat = " ".join(self.text.split())
        return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


class TranscriptionHistory:
    def __init__(self, path: Path, limit: int = MAX_HISTORY_ENTRIES) -> None:
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
        self._entries: list[HistoryEntry] = []
        self._load()

    # -- reading ---------------------------------------------------------------

    @property
    def entries(self) -> tuple[HistoryEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    def __len__(self) -> int:
        return len(self.entries)

    # -- writing ---------------------------------------------------------------

    def add(self, text: str, created_at: float | None = None) -> HistoryEntry | None:
        cleaned = str(text).strip()
        if not cleaned:
            return None
        entry = HistoryEntry(text=cleaned[:MAX_ENTRY_CHARACTERS], created_at=created_at or time.time())
        with self._lock:
            self._entries = [entry, *self._entries][: self.limit]
            self._save()
        return entry

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._save()

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Ignoring unreadable transcription history: %s", exc)
            return
        items = raw.get("entries", []) if isinstance(raw, dict) else raw
        entries: list[HistoryEntry] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text", "")).strip()
                try:
                    created_at = float(item.get("created_at", 0))
                except (TypeError, ValueError):
                    created_at = 0.0
                if text and created_at > 0:
                    entries.append(HistoryEntry(text=text[:MAX_ENTRY_CHARACTERS], created_at=created_at))
        entries.sort(key=lambda entry: entry.created_at, reverse=True)
        self._entries = entries[: self.limit]

    def _save(self) -> None:
        payload = {
            "version": 1,
            "entries": [{"text": entry.text, "created_at": entry.created_at} for entry in self._entries],
        }
        serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix="history-",
                suffix=".tmp",
                delete=False,
            ) as temp:
                temp.write(serialized)
                temp.flush()
                os.fsync(temp.fileno())
                temp_path = Path(temp.name)
            os.replace(temp_path, self.path)
            temp_path = None
        except OSError as exc:
            logging.warning("Could not save transcription history: %s", exc)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
