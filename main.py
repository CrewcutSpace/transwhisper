"""Entry point: audio -> live transcription -> translation -> overlay + stdout."""

import argparse
import logging
import queue
import signal
import sys
import threading
import time

from loguru import logger

import config
from audio import BLOCK_SECONDS, WHISPER_RATE, AudioCapture, DeviceNotFoundError, find_input_device, list_input_devices
from stream import Segment, Segmenter
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


class Pipeline:
    def __init__(self, blocks: queue.Queue, segmenter: Segmenter, transcriber, translator, on_entry):
        self.blocks = blocks
        self.segmenter = segmenter
        self.transcriber = transcriber
        self.translator = translator
        self.on_entry = on_entry
        self.stopped = threading.Event()
        self._failed_sources: set[str] = set()
        self._shown_partial = False
        self._last_audio_at = time.monotonic()

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

    def _translate(self, text: str, language: str) -> str:
        try:
            return self.translator.translate(text, language)
        except TranslationError as exc:
            if language not in self._failed_sources:
                logger.error("{}", exc)
                self._failed_sources.add(language)
            return f"[{language}] {text}"

    def _handle(self, segment: Segment):
        started = time.monotonic()
        result = self.transcriber.transcribe(segment.audio, final=segment.final)
        transcribed = time.monotonic()
        if not result:
            if segment.final and self._shown_partial:
                self.on_entry("", "", True)
                self._shown_partial = False
            return

        translated = self._translate(result.text, result.language)
        logger.debug(
            "{} {:.1f}s audio: waited {:.2f}s, whisper {:.2f}s, translate {:.2f}s, lag {:.2f}s | {}",
            "final  " if segment.final else "partial", len(segment.audio) / WHISPER_RATE,
            started - self._last_audio_at, transcribed - started, time.monotonic() - transcribed,
            time.monotonic() - self._last_audio_at, result.text[-50:],
        )
        if segment.final:
            print(f"[{result.language}] {result.text}\n     {translated}", flush=True)
        self.on_entry(result.text, translated, segment.final)
        self._shown_partial = not segment.final

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
        logger.info("Audio stream finished")


def run(args) -> int:
    logger.info(
        "Config: model={}, source={}, target={}, backend={}, step={}s, pause={}s",
        config.MODEL_SIZE, config.SOURCE_LANG, config.TARGET_LANG, config.TRANSLATE_BACKEND,
        config.PARTIAL_STEP_SECONDS, config.PAUSE_SECONDS,
    )

    # 1. Audio device
    device = None
    if args.file:
        logger.info("Reading audio from file: {}", args.file)
    else:
        try:
            device = find_input_device(config.INPUT_DEVICE)
        except DeviceNotFoundError as exc:
            logger.error("{}", exc)
            return 1
        logger.info("Input device found: [{}]", device)

    # 2. Translation backend
    translator = create_translator(config.TRANSLATE_BACKEND, config.TARGET_LANG, config.DEEPL_API_KEY, config.ARGOS_DIR)
    if config.SOURCE_LANG != "auto":
        try:
            translator.prepare(config.SOURCE_LANG)
        except TranslationError as exc:
            logger.error("{}", exc)
            return 1
    logger.info("Translation backend ready: {} ({} -> {})", translator.name, config.SOURCE_LANG, config.TARGET_LANG)

    # 3. Whisper
    logger.info("Loading Whisper model '{}' (first run downloads it)...", config.MODEL_SIZE)
    transcriber = Transcriber(
        config.MODEL_SIZE, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE, config.WHISPER_THREADS, config.SOURCE_LANG
    )

    # 4. Output
    overlay = app = None
    if not args.no_overlay:
        from overlay import Overlay, create_app

        app = create_app()
        overlay = Overlay(config.MAX_ENTRIES, config.OVERLAY_OPACITY, config.SHOW_ORIGINAL)
        overlay.show()

    blocks: queue.Queue = queue.Queue()
    segmenter = Segmenter(config.PARTIAL_STEP_SECONDS, config.PAUSE_SECONDS, config.MAX_SEGMENT_SECONDS, config.SILENCE_THRESHOLD)
    pipeline = Pipeline(blocks, segmenter, transcriber, translator, overlay.update_entry if overlay else lambda *_: None)

    # 5. Start
    capture = None
    if args.file:
        threading.Thread(target=play_file, args=(args.file, blocks), daemon=True).start()
    else:
        capture = AudioCapture(device, blocks)
        capture.start()

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
        if capture:
            capture.stop()
        pipeline.stop()
        logger.info("Stopped.")
    return 0


@logger.catch
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
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
