"""Turns a continuous 16 kHz audio stream into lines: partial ones while someone is
speaking (re-transcribed every step) and a final one when they pause."""

from dataclasses import dataclass

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps

RATE = 16000
VAD_WINDOW = 512  # Silero VAD frame size at 16 kHz
KEEP_BEFORE_SPEECH = int(0.3 * RATE)


@dataclass
class Segment:
    audio: np.ndarray
    final: bool


class Segmenter:
    def __init__(self, step_seconds: float, pause_seconds: float, max_seconds: float, silence_threshold: float):
        self.step = int(step_seconds * RATE)
        self.max_len = int(max_seconds * RATE)
        self.silence_threshold = silence_threshold
        self.vad_options = VadOptions(min_silence_duration_ms=int(pause_seconds * 1000), speech_pad_ms=100)
        self.short_gap_options = VadOptions(min_silence_duration_ms=100, speech_pad_ms=30)
        self.min_split = self.max_len // 3
        self.buffer = np.empty(0, dtype=np.float32)
        self._since_partial = 0

    def add(self, audio: np.ndarray):
        self.buffer = np.concatenate([self.buffer, audio])
        self._since_partial += len(audio)

    def _take(self, cut: int) -> Segment:
        segment = Segment(self.buffer[:cut], final=True)
        self.buffer = self.buffer[cut:]
        self._since_partial = len(self.buffer)
        return segment

    def poll(self) -> Segment | None:
        """Return the next segment to transcribe, if any. Call again after a final one."""
        if len(self.buffer) < KEEP_BEFORE_SPEECH:
            return None
        if float(np.sqrt(np.mean(self.buffer ** 2))) < self.silence_threshold:
            self.buffer = self.buffer[-KEEP_BEFORE_SPEECH:]
            self._since_partial = 0
            return None

        speech = get_speech_timestamps(self.buffer, self.vad_options)
        if not speech:
            self.buffer = self.buffer[-KEEP_BEFORE_SPEECH:]
            self._since_partial = 0
            return None

        # Drop silence before the first word.
        lead = max(0, speech[0]["start"] - KEEP_BEFORE_SPEECH)
        if lead:
            self.buffer = self.buffer[lead:]
            speech = [{"start": s["start"] - lead, "end": s["end"] - lead} for s in speech]

        last = speech[-1]
        # The VAD only closes a speech region once the pause is long enough.
        if last["end"] < len(self.buffer) - VAD_WINDOW:
            return self._take(last["end"])
        if len(speech) > 1:
            # A pause happened and speech already resumed after it.
            return self._take((speech[-2]["end"] + last["start"]) // 2)

        if len(self.buffer) >= self.max_len:
            return self._take(self._split_point())

        if self._since_partial >= self.step:
            self._since_partial = 0
            return Segment(self.buffer.copy(), final=False)
        return None

    def _split_point(self) -> int:
        """Someone talks without a real pause: cut at the last short breath instead of mid-word."""
        short_gaps = get_speech_timestamps(self.buffer, self.short_gap_options)
        cuts = [
            (prev["end"] + nxt["start"]) // 2
            for prev, nxt in zip(short_gaps, short_gaps[1:])
            if (prev["end"] + nxt["start"]) // 2 >= self.min_split
        ]
        return cuts[-1] if cuts else len(self.buffer)

    def flush(self) -> Segment | None:
        if len(self.buffer) and get_speech_timestamps(self.buffer, self.vad_options):
            return self._take(len(self.buffer))
        return None
