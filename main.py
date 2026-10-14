"""Entry point: audio -> live transcription -> translation -> overlay + stdout."""

import argparse
import contextlib
import logging
import queue
import signal
import sys
import threading
import time

import numpy as np
from loguru import logger

import config
from audio import (
    BLOCK_SECONDS,
    WHISPER_RATE,
    AudioCapture,
    DeviceNotFoundError,
    SystemAudioCapture,
    TapUnavailableError,
    find_input_device,
    list_input_devices,
)
from stream import Segment, Segmenter, StableText, normalize_word, split_units
from transcribe import Transcriber
from translate import TranslationError, create_translator


def play_file(path: str, out: queue.Queue):
    """Feed an audio file into the pipeline at real-time speed, like live capture (for testing)."""
    from faster_whisper import decode_audio

    audio = decode_audio(path, sampling_rate=WHISPER_RATE)
    step = int(BLOCK_SECONDS * WHISPER_RATE)
    for start in range(0, len(audio), step):
        out.put(audio[start:start + step])
        time.sleep(BLOCK_SECONDS)
    out.put(None)


# An entry that grew this long is closed at the next pause even if the sentence seems to go on.
MAX_ENTRY_WORDS = 60
# A last sentence longer than this is not heard again with the next line.
MAX_CARRY_SECONDS = 10
# Silence put between the carried sentence and the next line, where the pause was.
GAP = np.zeros(int(0.3 * WHISPER_RATE), dtype=np.float32)


def _carry_verdict(words: list[str], carried: int, final: bool) -> bool | None:
    """The next line is transcribed together with the carried sentence (`carried` words).
    Whisper, hearing both, ends that sentence where it really ends: True if it went on
    after the pause, False if it was complete, None if it is too early to tell."""
    boundary = 0
    for unit in split_units(words)[0]:
        boundary += len(unit)
        if abs(boundary - carried) <= 1:
            return False
    if final or len(words) >= carried + 3:
        return True
    return None


def _find_unit_end(words: list[str], unit: list[str], start: int, slack: int = 3) -> int | None:
    """Index right after `unit` in `words`, looking near `start + len(unit)`. The final
    transcription may differ by a word or two, so match on the unit's last two words."""
    if not unit:
        return start
    expected = start + len(unit)
    anchor = unit[-2:]
    for end in sorted(range(expected - slack, expected + slack + 1), key=lambda e: abs(e - expected)):
        if len(anchor) <= end <= len(words) and end > start:
            if [normalize_word(w) for w in words[end - len(anchor):end]] == anchor:
                return end
    return None


class Pipeline:
    def __init__(self, blocks: queue.Queue, segmenter: Segmenter, transcriber, translator, live_translator, on_entry,
                 join_seconds: float):
        self.blocks = blocks
        self.segmenter = segmenter
        self.transcriber = transcriber
        self.translator = translator
        # The line still being spoken may use a cheaper backend; see config.LIVE_BACKEND.
        self.live_translator = live_translator
        self.on_entry = on_entry
        self.join_seconds = join_seconds
        self.stopped = threading.Event()
        self._failed_sources: set[str] = set()
        self._shown_partial = False
        self._last_audio_at = time.monotonic()
        self.stable = StableText()
        self._cache: dict[tuple[str, str, str], str] = {}
        # One entry on screen spans the lines of a sentence interrupted by pauses.
        # Sentences of the open entry from earlier lines: (source, translation).
        self._entry: list[tuple[str, str]] = []
        # The last sentence of the previous line and its audio: it may go on after the pause,
        # so it is transcribed again together with the next line.
        self._carry = ""
        self._carry_audio: np.ndarray | None = None
        self._carry_since = 0.0
        # Whether the current line turned out to continue the carried sentence (None: not known yet).
        self._continues: bool | None = None
        # Current line: (source, translation) sentences already frozen on screen.
        self._frozen: list[tuple[str, str]] = []
        self._shown = ("", "")
        self._language = transcriber.language or "en"

    def stop(self):
        self.stopped.set()
        self.blocks.put(None)

    def _drain(self) -> bool:
        """Move all captured audio into the segmenter. Returns False when the stream has ended."""
        try:
            block = self.blocks.get(timeout=0.2)
        except queue.Empty:
            return True
        while True:
            if block is None:
                return False
            self.segmenter.add(block)
            self._last_audio_at = time.monotonic()
            try:
                block = self.blocks.get_nowait()
            except queue.Empty:
                return True

    def _translate(self, text: str, language: str, live: bool = False) -> str:
        """Translate one sentence. Cached, so a sentence already on screen keeps exactly the
        same translation when it is shown again (and DeepL is not billed twice)."""
        translator = self.live_translator if live else self.translator
        if translator is None or not text:
            return ""
        # Punctuation is part of the key: "tight," and "tight." end different sentences.
        key = (translator.name, language, " ".join(text.split()))
        if key in self._cache:
            return self._cache[key]
        try:
            translated = translator.translate(text, language)
        except TranslationError as exc:
            if language not in self._failed_sources:
                logger.error("{}", exc)
                self._failed_sources.add(language)
            return f"[{language}] {text}"
        self._cache[key] = translated
        return translated

    def _show(self, sentences: list[tuple[str, str]], pending_source: str, pending: str, final: bool):
        original = " ".join(source for source, _ in sentences + [(pending_source, "")]).strip()
        done = " ".join(translated for _, translated in sentences).strip()
        if not original:
            if self._shown_partial:
                self.on_entry("", "", "", True)
                self._shown_partial = False
            return
        if final:
            print(f"[{self._language}] {original}\n     {done}", flush=True)
        self.on_entry(original, done, pending, final)
        self._shown_partial = not final

    def _close_entry(self):
        """Commit the open entry: the carried sentence did not go on."""
        sentences = self._entry + ([(self._carry, self._translate(self._carry, self._language))] if self._carry else [])
        self._show(sentences, "", "", final=True)
        self._entry, self._carry, self._carry_audio, self._continues = [], "", None, None
        self._cache.clear()

    def _handle(self, segment: Segment):
        started = time.monotonic()
        audio = segment.audio
        if self._carry_audio is not None:
            audio = np.concatenate([self._carry_audio, GAP, audio])
        result = self.transcriber.transcribe(audio, final=segment.final)
        transcribed = time.monotonic()

        if result:
            self._language = result.language
        if self._carry_audio is not None and self._continues is None:
            # Someone is speaking again: no timeout until it is known whether the sentence goes on.
            self._carry_since = time.monotonic()
            if result:
                self._continues = _carry_verdict(result.text.split(), len(self._carry.split()), segment.final)
            if self._continues is False:
                # A new sentence: commit the entry and hear this line on its own.
                self._close_entry()
                self.stable.reset()
                self._frozen, self._shown = [], ("", "")
                self._handle(segment)
                return
        text = result.text if result else ""

        if segment.final:
            words = self._final_words(text.split())
        else:
            words = self.stable.update(text).split()[len(self._frozen_words()):]
            if not words and not self._frozen:
                return

        units, tail = split_units(words)
        # Complete sentences are translated once and never change on screen again.
        for unit in units:
            source = " ".join(unit)
            self._frozen.append((source, self._translate(source, self._language)))
        tail_source = " ".join(tail)

        if segment.final:
            # The last sentence may go on after the pause, so it stays open (grey) for now.
            if self._continues is None and self._carry:
                # Nothing was heard: the carried sentence stays as it was.
                self._frozen.insert(0, (self._carry, self._translate(self._carry, self._language)))
            self._entry += self._frozen
            self._frozen, self._shown = [], ("", "")
            self.stable.reset()
            self._carry, self._carry_audio, self._continues = tail_source, None, None
            self._carry_since = time.monotonic()
            if tail and result and result.starts:
                start = max(0, int((result.starts[len(result.starts) - len(tail)] - 0.1) * WHISPER_RATE))
                if len(audio) - start <= MAX_CARRY_SECONDS * WHISPER_RATE:
                    self._carry_audio = audio[start:]
            done_text, pending_text = "", self._translate(tail_source, self._language)
            entry_words = sum(len(source.split()) for source, _ in self._entry) + len(tail)
            if self._carry_audio is None or entry_words >= MAX_ENTRY_WORDS:
                self._close_entry()
            else:
                self._show(self._entry, tail_source, pending_text, final=False)
        else:
            done_text = " ".join(translated for _, translated in self._frozen)
            pending_text = self._translate(tail_source, self._language, live=True)
            if (done_text, pending_text) == self._shown:
                return
            self._shown = (done_text, pending_text)
            self._show(self._entry + self._frozen, tail_source, pending_text, final=False)
        logger.debug(
            "{} {:.1f}s audio: waited {:.2f}s, whisper {:.2f}s, translate {:.2f}s, lag {:.2f}s | {} ~ {}",
            "final  " if segment.final else "partial", len(audio) / WHISPER_RATE,
            started - self._last_audio_at, transcribed - started, time.monotonic() - transcribed,
            time.monotonic() - self._last_audio_at, done_text[-50:], pending_text,
        )

    def _frozen_words(self) -> list[str]:
        return " ".join(source for source, _ in self._frozen).split()

    def _final_words(self, final: list[str]) -> list[str]:
        """Reconcile the final transcription with sentences already frozen on screen: keep those
        that the final text still contains and return only the words after them."""
        position = 0
        kept = 0
        for source, _ in self._frozen:
            unit = [normalize_word(w) for w in source.split()]
            end = _find_unit_end(final, unit, position)
            if end is None:
                break
            position, kept = end, kept + 1
        # Sentences the final text does not contain (a line split before them) are heard again in the next line.
        self._frozen = self._frozen[:kept]
        return final[position:]

    @logger.catch
    def run(self):
        while not self.stopped.is_set():
            running = self._drain()
            if not running:
                segment = self.segmenter.flush()
                if segment:
                    self._handle(segment)
                break
            segment = self.segmenter.poll()
            while segment is not None:
                self._handle(segment)
                segment = self.segmenter.poll() if segment.final else None
            # Nobody went on speaking: the carried sentence was complete.
            if self._carry and self._continues is None and time.monotonic() - self._carry_since > self.join_seconds:
                self._close_entry()
        if self._entry or self._carry:
            self._close_entry()
        logger.info("Audio stream finished")


def create_capture(blocks: queue.Queue):
    if config.AUDIO_SOURCE == "tap":
        return SystemAudioCapture(blocks)
    device = find_input_device(config.INPUT_DEVICE)
    logger.info("Input device found: [{}]", device)
    return AudioCapture(device, blocks)


def run(args) -> int:
    logger.info(
        "Config: model={}, source={}, target={}, backend={}, step={}s, pause={}s",
        config.MODEL_SIZE, config.SOURCE_LANG, config.TARGET_LANG, config.TRANSLATE_BACKEND,
        config.PARTIAL_STEP_SECONDS, config.PAUSE_SECONDS,
    )

    blocks: queue.Queue = queue.Queue()

    # 1. Audio source
    capture = None
    if args.file:
        logger.info("Reading audio from file: {}", args.file)
    else:
        try:
            capture = create_capture(blocks)
        except (DeviceNotFoundError, TapUnavailableError) as exc:
            logger.error("{}", exc)
            return 1

    # 2. Translation backends: one for the text that stays, a cheap one for the live line
    translator = create_translator(config.TRANSLATE_BACKEND, config.TARGET_LANG, config.DEEPL_API_KEY, config.ARGOS_DIR)
    live_backend = config.LIVE_BACKEND
    if live_backend == "auto":
        # DeepL bills per character and the live line is re-translated twice a second.
        live_backend = "argos" if translator.name == "deepl" else "same"
    live_translator = translator
    if live_backend == "off":
        live_translator = None
    elif live_backend not in ("same", translator.name):
        live_translator = create_translator(live_backend, config.TARGET_LANG, "", config.ARGOS_DIR)
    if config.SOURCE_LANG != "auto":
        try:
            translator.prepare(config.SOURCE_LANG)
            if live_translator is not None and live_translator is not translator:
                live_translator.prepare(config.SOURCE_LANG)
        except TranslationError as exc:
            logger.error("{}", exc)
            return 1
    logger.info(
        "Translation ready: {} for finished text, {} for the live line ({} -> {})",
        translator.name, live_translator.name if live_translator else "none",
        config.SOURCE_LANG, config.TARGET_LANG,
    )

    # 3. Whisper
    logger.info("Loading Whisper model '{}' (first run downloads it)...", config.MODEL_SIZE)
    transcriber = Transcriber(
        config.MODEL_SIZE, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE, config.WHISPER_THREADS, config.SOURCE_LANG
    )

    # 4. Output
    overlay = app = None
    if not args.no_overlay:
        from overlay import Overlay, create_app

        app = create_app(config.SHOW_IN_DOCK)
        overlay = Overlay(
            config.MAX_ENTRIES, config.OVERLAY_OPACITY, config.SHOW_ORIGINAL,
            config.FOLLOW_WINDOW, config.SHOW_IN_DOCK,
        )
        overlay.show()

    segmenter = Segmenter(config.PARTIAL_STEP_SECONDS, config.PAUSE_SECONDS, config.MAX_SEGMENT_SECONDS, config.SILENCE_THRESHOLD)
    pipeline = Pipeline(
        blocks, segmenter, transcriber, translator, live_translator,
        overlay.update_entry if overlay else lambda *_: None, config.JOIN_PAUSE_SECONDS,
    )

    # 5. Start
    if args.file:
        threading.Thread(target=play_file, args=(args.file, blocks), daemon=True).start()
    else:
        try:
            capture.start()
        except TapUnavailableError as exc:
            logger.error("{}", exc)
            return 1

    logger.info("Running. Press Ctrl+C to stop.")
    try:
        if app is None:
            pipeline.run()
        else:
            from PySide6.QtCore import QTimer

            worker = threading.Thread(target=pipeline.run, name="pipeline", daemon=True)
            worker.start()
            signal.signal(signal.SIGINT, lambda *_: app.quit())
            # Qt's event loop blocks Python signal handling; wake the interpreter periodically.
            timer = QTimer()
            timer.timeout.connect(lambda: None)
            timer.start(200)
            app.exec()
    except KeyboardInterrupt:
        pass
    finally:
        # Ctrl+C can land again while we are already shutting down.
        with contextlib.suppress(KeyboardInterrupt):
            if capture:
                capture.stop()
            pipeline.stop()
            for backend in (translator, live_translator):
                close = getattr(backend, "close", None)
                if close:
                    close()
        logger.info("Stopped.")
    return 0


@logger.catch(exclude=KeyboardInterrupt)
def main() -> int:
    parser = argparse.ArgumentParser(description="transwhisper: local real-time call translator")
    parser.add_argument("--list-devices", action="store_true", help="list audio input devices and exit")
    parser.add_argument("--file", help="play an audio file through the pipeline instead of live capture (for testing)")
    parser.add_argument("--no-overlay", action="store_true", help="stdout only, no window")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging (latency of every update)")
    args = parser.parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if args.verbose else "INFO",
        format="<green>{time:HH:mm:ss.SSS}</green> <level>{level: <7}</level> {message}",
    )
    # Third-party libraries log through stdlib logging; keep only their errors.
    for noisy in ("faster_whisper", "httpx", "huggingface_hub", "deepl"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    if args.list_devices:
        for i, name in list_input_devices():
            print(f"[{i}] {name}")
        return 0
    try:
        return run(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
