"""Small on-disk histories of recent transcriptions and recordings.

Only the last few transcripts are kept, newest first, in a JSON file inside
the app data folder. Recordings retain their WAVs even if transcription fails.
Writes are atomic so a crash never leaves a broken file.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import wave
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

MAX_HISTORY_ENTRIES = 20
MAX_RECORDING_ENTRIES = 20
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


@dataclass(frozen=True)
class RecordingEntry(HistoryEntry):
    id: str
    duration: float
    status: str = "saved"
    error: str = ""


class RecordingHistory:
    """Durable WAVs with optional metadata; audio survives interrupted requests.

    Each WAV is committed before its metadata and before contacting Groq. On
    startup, WAVs without readable metadata are recovered into the history.
    """

    def __init__(self, directory: Path, limit: int = MAX_RECORDING_ENTRIES) -> None:
        if limit < 1:
            raise ValueError("Recording history limit must be positive")
        self.directory = directory
        self.limit = limit
        self._lock = threading.Lock()
        self._entries: list[RecordingEntry] = []
        self._load()

    @property
    def entries(self) -> tuple[RecordingEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    def get(self, recording_id: str) -> RecordingEntry:
        with self._lock:
            entry = next((entry for entry in self._entries if entry.id == recording_id), None)
            if entry is None or not self.audio_path(entry).is_file():
                raise RuntimeError("Deze opname is niet meer beschikbaar.")
            return entry

    def audio_path(self, entry: RecordingEntry) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", entry.id):
            raise ValueError("Invalid recording ID")
        return self.directory / f"{entry.id}.wav"

    def add(self, source: Path, created_at: float | None = None) -> RecordingEntry:
        with wave.open(str(source), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        entry = RecordingEntry(
            text="", created_at=time.time() if created_at is None else created_at,
            id=uuid.uuid4().hex, duration=duration,
        )
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.directory, suffix=".tmp", delete=False) as temp:
                    temp_path = Path(temp.name)
                    with source.open("rb") as original:
                        shutil.copyfileobj(original, temp)
                    temp.flush()
                    os.fsync(temp.fileno())
                os.replace(temp_path, self.audio_path(entry))
                temp_path = None
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
            self._entries.insert(0, entry)
            self._save_entry(entry)
            self._prune()
        return entry

    def update(self, recording_id: str, *, status: str, text: str | None = None, error: str = "") -> None:
        if status not in {"saved", "processing", "done", "failed"}:
            raise ValueError("Invalid recording status")
        with self._lock:
            for index, entry in enumerate(self._entries):
                if entry.id == recording_id:
                    updated = replace(
                        entry, status=status, error=error[:2_000],
                        text=entry.text if text is None else text[:MAX_ENTRY_CHARACTERS],
                    )
                    self._entries[index] = updated
                    self._save_entry(updated)
                    return

    def clear(self) -> None:
        with self._lock:
            try:
                for entry in self._entries:
                    self._delete(entry)
            finally:
                self._entries = [entry for entry in self._entries if self.audio_path(entry).exists()]

    def _load(self) -> None:
        if not self.directory.exists():
            return
        for path in self.directory.glob("*.wav"):
            if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
                continue
            try:
                with wave.open(str(path), "rb") as audio:
                    duration = audio.getnframes() / audio.getframerate()
                created_at = path.stat().st_mtime
            except (OSError, EOFError, wave.Error, ZeroDivisionError) as exc:
                logging.warning("Could not read saved recording %s: %s", path.name, exc)
                continue
            raw = {}
            try:
                raw = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass  # The WAV itself is sufficient to recover and retry.
            if not isinstance(raw, dict):
                raw = {}
            try:
                timestamp = float(raw.get("created_at", created_at))
                if math.isfinite(timestamp) and 0 < timestamp < 253_402_214_400:
                    created_at = timestamp
            except (TypeError, ValueError, OverflowError):
                pass
            status = raw.get("status", "saved")
            if status not in ("saved", "processing", "done", "failed"):
                status = "saved"
            error = str(raw.get("error", ""))[:2_000]
            if status == "processing":
                status = "failed"
                error = "Vorige transcriptiepoging is onderbroken. Probeer opnieuw."
            self._entries.append(RecordingEntry(
                text=str(raw.get("text", ""))[:MAX_ENTRY_CHARACTERS], created_at=created_at,
                id=path.stem, duration=duration, status=status, error=error,
            ))
        self._entries.sort(key=lambda entry: entry.created_at, reverse=True)
        self._prune()

    def _delete(self, entry: RecordingEntry) -> None:
        path = self.audio_path(entry)
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)

    def _prune(self) -> None:
        for entry in self._entries[self.limit:]:
            try:
                self._delete(entry)
            except OSError as exc:
                logging.warning("Could not remove expired recording: %s", exc)
        self._entries = self._entries[:self.limit]

    def _save_entry(self, entry: RecordingEntry) -> None:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory, suffix=".tmp", delete=False,
            ) as temp:
                temp_path = Path(temp.name)
                json.dump(asdict(entry), temp, ensure_ascii=False, indent=2)
                temp.write("\n")
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_path, self.audio_path(entry).with_suffix(".json"))
            temp_path = None
        except OSError as exc:
            logging.warning("Could not save recording metadata; WAV remains available: %s", exc)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
