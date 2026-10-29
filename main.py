"""Entry point: audio -> live transcription -> translation -> overlay + stdout."""

import argparse
import contextlib
import logging
import os
import queue
import signal
import sys
import threading
import time
from types import SimpleNamespace

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
from transcript import Transcript
from translate import LANGUAGE_NAMES, TranslationError, create_translator


def play_file(path: str, out: queue.Queue):
    """Feed an audio file into the pipeline at real-time speed, like live capture (for testing)."""
    from faster_whisper import decode_audio

    try:
        audio = decode_audio(path, sampling_rate=WHISPER_RATE)
        step = int(BLOCK_SECONDS * WHISPER_RATE)
        for start in range(0, len(audio), step):
            out.put(audio[start:start + step])
            time.sleep(BLOCK_SECONDS)
    except Exception as exc:
        logger.error("Could not read {}: {}", path, exc)
    finally:
        # Ends the pipeline, also when the file could not be read.
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


# Stands in the translation cache for a sentence the slow backend failed on.
FAILED = object()


class Pipeline:
    def __init__(self, blocks: queue.Queue, segmenter: Segmenter, transcriber, translator, live_translator, on_entry,
                 join_seconds: float, transcript: Transcript):
        self.blocks = blocks
        self.segmenter = segmenter
        self.transcriber = transcriber
        self.translator = translator
        # The line still being spoken may use a cheaper backend; see config.LIVE_BACKEND.
        self.live_translator = live_translator
        self.on_entry = on_entry
        self.join_seconds = join_seconds
        self.transcript = transcript
        self.stopped = threading.Event()
        self._failed_sources: set[str] = set()
        self._last_audio_at = time.monotonic()
        self.stable = StableText()
        # Translations by (language, sentence). Final ones are kept while an entry waits for them.
        self._live_cache: dict[tuple[str, str], str] = {}
        self._final: dict[tuple[str, str], object] = {}
        # A slow main backend (a local LLM) translates finished sentences in the background, in order;
        # meanwhile they are shown grey with the live translation.
        self._jobs: queue.Queue = queue.Queue()
        self._queued: set[tuple[str, str]] = set()
        self._upgraded = threading.Event()
        if getattr(translator, "slow", False):
            threading.Thread(target=self._translate_in_background, name="translate", daemon=True).start()
        # One entry on screen spans the lines of a sentence interrupted by pauses.
        self._id = 0
        # Source sentences of the open entry from earlier lines.
        self._entry: list[str] = []
        # The last sentence of the previous line and its audio: it may go on after the pause,
        # so it is transcribed again together with the next line.
        self._carry = ""
        self._carry_audio: np.ndarray | None = None
        self._carry_since = 0.0
        # Whether the current line turned out to continue the carried sentence (None: not known yet).
        self._continues: bool | None = None
        # Current line: source sentences already frozen on screen.
        self._frozen: list[str] = []
        self._language = transcriber.language or "en"
        # What each entry shows (original, done, pending), and when it started.
        self._rendered: dict[int, tuple[str, str, str]] = {}
        self._started: dict[int, float] = {}
        # The open entry as last shown, to show it again when a translation arrives.
        self._open_view: tuple[list[str], str, str] = ([], "", "")
        # Closed entries still waiting for final translations: id -> (sentences, language).
        self._waiting: dict[int, tuple[list[str], str]] = {}

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

    def _call(self, translator, text: str, language: str) -> str:
        try:
            return translator.translate(text, language)
        except TranslationError as exc:
            if (translator.name, language) not in self._failed_sources:
                logger.error("{}", exc)
                self._failed_sources.add((translator.name, language))
            raise

    @staticmethod
    def _key(text: str, language: str) -> tuple[str, str]:
        # Punctuation is part of the key: "tight," and "tight." end different sentences.
        return language, " ".join(text.split())

    def _live(self, text: str, language: str) -> str:
        """Quick translation of text that may still change (cached: shown again unchanged)."""
        if self.live_translator is None or not text:
            return ""
        key = self._key(text, language)
        if key not in self._live_cache:
            try:
                self._live_cache[key] = self._call(self.live_translator, text, language)
            except TranslationError:
                return f"[{language}] {text}"
        return self._live_cache[key]

    def _sentence(self, text: str, language: str) -> tuple[str, bool]:
        """The translation of a finished sentence, and whether it is final. A slow backend's
        translation is requested in the background; until it arrives the live one stands in."""
        key = self._key(text, language)
        if key not in self._final and not getattr(self.translator, "slow", False):
            try:
                self._final[key] = self._call(self.translator, text, language)
            except TranslationError:
                self._final[key] = FAILED
        if key in self._final:
            value = self._final[key]
            if value is FAILED:
                return self._live(text, language) or f"[{language}] {text}", True
            return value, True
        if key not in self._queued:
            self._queued.add(key)
            self._jobs.put(key)
        return self._live(text, language), False

    def _translate_in_background(self):
        while True:
            key = self._jobs.get()
            language, text = key
            try:
                self._final[key] = self._call(self.translator, text, language)
            except TranslationError:
                self._final[key] = FAILED
            except Exception:
                logger.exception("Background translation failed")
                self._final[key] = FAILED
            self._queued.discard(key)
            self._upgraded.set()
            self._jobs.task_done()

    def _render(self, entry_id: int, sentences: list[str], tail_source: str, tail: str, language: str) -> bool:
        """Show an entry: white up to the first sentence still waiting for its final translation,
        grey after it. Returns True when nothing in it can change any more."""
        done: list[str] = []
        pending: list[str] = []
        for sentence in sentences:
            translated, final = self._sentence(sentence, language)
            (done if final and not pending else pending).append(translated)
        state = (" ".join(sentences + [tail_source]).strip(), " ".join(done), " ".join(pending + [tail]).strip())
        if not state[0]:
            if entry_id in self._rendered:
                self.on_entry(entry_id, "", "", "")
                del self._rendered[entry_id]
            return True
        if entry_id not in self._rendered:
            self._started[entry_id] = time.time()
        if self._rendered.get(entry_id) != state:
            self._rendered[entry_id] = state
            self.on_entry(entry_id, *state)
        return not pending and not tail_source

    def _commit(self, entry_id: int):
        """An entry is final: print it and save it."""
        original, done, pending = self._rendered.pop(entry_id, ("", "", ""))
        started = self._started.pop(entry_id, time.time())
        if not original:
            return
        translation = f"{done} {pending}".strip()
        print(f"[{self._language}] {original}\n     {translation}", flush=True)
        self.transcript.write(started, original, translation)

    def _close_entry(self):
        """Close the open entry: the carried sentence did not go on."""
        sentences = self._entry + ([self._carry] if self._carry else [])
        if self._render(self._id, sentences, "", "", self._language):
            self._commit(self._id)
        else:
            self._waiting[self._id] = (sentences, self._language)
        self._id += 1
        self._entry, self._carry, self._carry_audio, self._continues = [], "", None, None
        self._open_view = ([], "", "")
        self._live_cache.clear()

    def _show_open(self, sentences: list[str], tail_source: str, tail: str):
        self._open_view = (sentences, tail_source, tail)
        self._render(self._id, sentences, tail_source, tail, self._language)

    def _apply_translations(self):
        """Background translations arrived: update the entries waiting for them."""
        if not self._upgraded.is_set():
            return
        self._upgraded.clear()
        for entry_id, (sentences, language) in list(self._waiting.items()):
            if self._render(entry_id, sentences, "", "", language):
                del self._waiting[entry_id]
                self._commit(entry_id)
        if self._open_view[0] or self._open_view[1]:
            self._render(self._id, *self._open_view, self._language)
        # Translations no entry is waiting for any more.
        if len(self._final) > 500:
            for key in list(self._final)[:250]:
                del self._final[key]

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
                self._frozen = []
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
        # Complete sentences are frozen: their text never changes on screen again.
        self._frozen += [" ".join(unit) for unit in units]
        tail_source = " ".join(tail)

        if segment.final:
            # The last sentence may go on after the pause, so it stays open (grey) for now.
            if self._continues is None and self._carry:
                # Nothing was heard: the carried sentence stays as it was.
                self._frozen.insert(0, self._carry)
            self._entry += self._frozen
            self._frozen = []
            self.stable.reset()
            self._carry, self._carry_audio, self._continues = tail_source, None, None
            self._carry_since = time.monotonic()
            if tail and result and result.starts:
                start = max(0, int((result.starts[len(result.starts) - len(tail)] - 0.1) * WHISPER_RATE))
                if len(audio) - start <= MAX_CARRY_SECONDS * WHISPER_RATE:
                    self._carry_audio = audio[start:]
            pending_text = self._live(tail_source, self._language)
            if tail_source and getattr(self.translator, "slow", False):
                # Usually the sentence did end: have its final translation ready when the wait is over.
                self._sentence(tail_source, self._language)
            entry_words = sum(len(source.split()) for source in self._entry) + len(tail)
            if self._carry_audio is None or entry_words >= MAX_ENTRY_WORDS:
                self._close_entry()
            else:
                self._show_open(self._entry, tail_source, pending_text)
        else:
            pending_text = self._live(tail_source, self._language)
            self._show_open(self._entry + self._frozen, tail_source, pending_text)
        logger.debug(
            "{} {:.1f}s audio: waited {:.2f}s, whisper {:.2f}s, translate {:.2f}s, lag {:.2f}s | {}",
            "final  " if segment.final else "partial", len(audio) / WHISPER_RATE,
            started - self._last_audio_at, transcribed - started, time.monotonic() - transcribed,
            time.monotonic() - self._last_audio_at, pending_text,
        )

    def _frozen_words(self) -> list[str]:
        return " ".join(self._frozen).split()

    def _final_words(self, final: list[str]) -> list[str]:
        """Reconcile the final transcription with sentences already frozen on screen: keep those
        that the final text still contains and return only the words after them."""
        position = 0
        kept = 0
        for source in self._frozen:
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
            self._apply_translations()
        if self._entry or self._carry:
            self._close_entry()
        # Give the background translations still running a moment to arrive.
        deadline = time.monotonic() + 10
        while self._waiting and time.monotonic() < deadline and not self.stopped.is_set():
            self._upgraded.wait(0.2)
            self._apply_translations()
        for entry_id in list(self._waiting):
            self._commit(entry_id)
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

    # 1. Window first, so it is there while the models load (started from the app, there is no console)
    overlay = app = None
    if not args.no_overlay:
        from overlay import Overlay, create_app

        app = create_app(config.SHOW_IN_DOCK)
        overlay = Overlay(
            config.MAX_ENTRIES, config.OVERLAY_OPACITY, config.SHOW_ORIGINAL,
            config.FOLLOW_WINDOW, config.SHOW_IN_DOCK,
        )
        overlay.show()
        backends = {"apple": "macOS translator (fast)", "ollama": f"AI: {config.OLLAMA_MODEL} via Ollama (best)"}
        if config.TRANSLATE_BACKEND not in backends:
            backends[config.TRANSLATE_BACKEND] = config.TRANSLATE_BACKEND
        overlay.set_settings(backends, config.TRANSLATE_BACKEND, LANGUAGE_NAMES, config.TARGET_LANG)
        overlay.notice("Starting...")

    transcript = Transcript(config.TRANSCRIPT_DIR, config.SAVE_TRANSCRIPT or args.save, config.SOURCE_LANG, config.TARGET_LANG)
    if overlay:
        overlay.set_transcript(transcript)

    blocks: queue.Queue = queue.Queue()
    # What has been started so far, to stop it again; filled by start().
    parts = SimpleNamespace(capture=None, translators=[], pipeline=None, failed=False)

    def fail(message: str):
        logger.error("{}", message)
        parts.failed = True
        if overlay:
            overlay.notice(message)

    def start() -> Pipeline | None:
        # 2. Audio source
        if args.file:
            logger.info("Reading audio from file: {}", args.file)
        else:
            try:
                parts.capture = create_capture(blocks)
            except (DeviceNotFoundError, TapUnavailableError) as exc:
                return fail(str(exc))
            if overlay:
                parts.capture.on_warning = overlay.notice

        # 3. Translation backends: one for the text that stays, a cheap one for the live line
        translator = create_translator(
            config.TRANSLATE_BACKEND, config.TARGET_LANG, config.DEEPL_API_KEY, config.ARGOS_DIR,
            config.OLLAMA_MODEL, config.OLLAMA_URL,
        )
        parts.translators.append(translator)
        live_backend = config.LIVE_BACKEND
        if live_backend == "auto":
            # DeepL bills per character and the live line is re-translated twice a second;
            # a local LLM is too slow for it.
            live_backend = {"deepl": "argos", "ollama": "apple"}.get(translator.name, "same")
        live_translator = translator
        if live_backend == "off":
            live_translator = None
        elif live_backend not in ("same", translator.name):
            live_translator = create_translator(live_backend, config.TARGET_LANG, "", config.ARGOS_DIR)
            parts.translators.append(live_translator)
        if config.SOURCE_LANG != "auto":
            try:
                translator.prepare(config.SOURCE_LANG)
                if live_translator is not None and live_translator is not translator:
                    live_translator.prepare(config.SOURCE_LANG)
            except TranslationError as exc:
                return fail(str(exc))
        logger.info(
            "Translation ready: {} for finished text, {} for the live line ({} -> {})",
            translator.name, live_translator.name if live_translator else "none",
            config.SOURCE_LANG, config.TARGET_LANG,
        )

        # 4. Whisper
        logger.info("Loading Whisper model '{}' (first run downloads it)...", config.MODEL_SIZE)
        transcriber = Transcriber(
            config.MODEL_SIZE, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE, config.WHISPER_THREADS, config.SOURCE_LANG
        )

        segmenter = Segmenter(config.PARTIAL_STEP_SECONDS, config.PAUSE_SECONDS, config.MAX_SEGMENT_SECONDS, config.SILENCE_THRESHOLD)
        parts.pipeline = Pipeline(
            blocks, segmenter, transcriber, translator, live_translator,
            overlay.update_entry if overlay else lambda *_: None, config.JOIN_PAUSE_SECONDS, transcript,
        )

        # 5. Start
        if args.file:
            threading.Thread(target=play_file, args=(args.file, blocks), daemon=True).start()
        else:
            try:
                parts.capture.start()
            except TapUnavailableError as exc:
                return fail(str(exc))

        if overlay:
            # Say so when the chosen translator could not be used, otherwise clear "Starting...".
            fallback = translator.name != config.TRANSLATE_BACKEND
            overlay.notice(
                f"{config.TRANSLATE_BACKEND} is not available, using {translator.name} (details in the log)"
                if fallback else ""
            )
        logger.info("Running. Press Ctrl+C to stop.")
        return parts.pipeline

    def work():
        pipeline = start()
        if pipeline is not None:
            pipeline.run()

    try:
        if app is None:
            work()
        else:
            from PySide6.QtCore import QTimer

            # Loads the models and then runs the pipeline, while the window stays responsive.
            worker = threading.Thread(target=logger.catch(work), name="pipeline", daemon=True)
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
            if parts.capture:
                parts.capture.stop()
            if parts.pipeline:
                parts.pipeline.stop()
            transcript.close()
            for backend in parts.translators:
                close = getattr(backend, "close", None)
                if close:
                    close()
        logger.info("Stopped.")
    if overlay and overlay.restart_requested:
        # A setting changed in the menu: start again with it (same process, so the app stays one app).
        os.execv(sys.executable, [sys.executable, *sys.argv])
    return 1 if parts.failed and app is None else 0


@logger.catch(exclude=KeyboardInterrupt)
def main() -> int:
    parser = argparse.ArgumentParser(description="transwhisper: local real-time call translator")
    parser.add_argument("--list-devices", action="store_true", help="list audio input devices and exit")
    parser.add_argument("--file", help="play an audio file through the pipeline instead of live capture (for testing)")
    parser.add_argument("--no-overlay", action="store_true", help="stdout only, no window")
    parser.add_argument("--save", action="store_true", help=f"save the conversation to a text file in {config.TRANSCRIPT_DIR}")
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
