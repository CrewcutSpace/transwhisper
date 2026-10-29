"""Optional transcript of the conversation: every finished entry (original + translation) is
appended to a text file, one file per run. Off unless switched on."""

import threading
import time
from datetime import datetime
from pathlib import Path

from loguru import logger


class Transcript:
    def __init__(self, directory: Path, enabled: bool, source: str, target: str):
        self.directory = directory
        self.source = source
        self.target = target
        self._enabled = enabled
        self._lock = threading.Lock()
        self._file = None
        self.path: Path | None = None
        if enabled:
            logger.info("Saving the conversation to {}", self.directory)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool):
        """Thread-safe; called from the menu while the pipeline keeps writing."""
        with self._lock:
            if enabled == self._enabled:
                return
            self._enabled = enabled
            if self._file:
                self._file.write(f"\n--- saving {'resumed' if enabled else 'paused'} at {time.strftime('%H:%M:%S')} ---\n\n")
                self._file.flush()
        logger.info("Saving the conversation: {}", "on" if enabled else "off")

    def _open(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        started = datetime.now()
        self.path = self.directory / f"{started:%Y-%m-%d_%H-%M-%S}.txt"
        self._file = open(self.path, "a", encoding="utf-8")
        self._file.write(f"transwhisper, conversation of {started:%Y-%m-%d %H:%M} ({self.source} -> {self.target})\n\n")
        logger.info("Conversation file: {}", self.path)

    def write(self, started_at: float, original: str, translation: str):
        """One finished entry. `started_at` is time.time() when it began to be spoken."""
        with self._lock:
            if not self._enabled:
                return
            if self._file is None:
                self._open()
            stamp = time.strftime("%H:%M:%S", time.localtime(started_at))
            self._file.write(f"[{stamp}] {original}\n           {translation}\n\n")
            # Flushed every time, so nothing is lost if the app is killed mid-call.
            self._file.flush()

    def close(self):
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
