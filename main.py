"""Entry point: audio -> transcription -> translation -> overlay + stdout."""

import argparse
import logging
import queue
import signal
import sys
import threading

import numpy as np
from loguru import logger

import config
from audio import WHISPER_RATE, AudioCapture, DeviceNotFoundError, find_input_device, is_silent, list_input_devices
from transcribe import Transcriber
from translate import TranslationError, create_translator


def file_chunks(path: str, chunk_seconds: float, silence_threshold: float, out: queue.Queue):
    """Feed an audio file through the pipeline as if it were live audio (for testing)."""
    from faster_whisper import decode_audio

    audio = decode_audio(path, sampling_rate=WHISPER_RATE)
    step = int(chunk_seconds * WHISPER_RATE)
    for start in range(0, len(audio), step):
        chunk = audio[start:start + step].astype(np.float32)
        if not is_silent(chunk, silence_threshold):
            out.put(chunk)
    out.put(None)


class Pipeline:
    def __init__(self, chunks: queue.Queue, transcriber, translator, on_entry):
        self.chunks = chunks
        self.transcriber = transcriber
        self.translator = translator
        self.on_entry = on_entry
        self.stopped = threading.Event()
        self._failed_sources: set[str] = set()

    def stop(self):
        self.stopped.set()
        self.chunks.put(None)

    @logger.catch
    def run(self):
        while not self.stopped.is_set():
            audio = self.chunks.get()
            if audio is None:
                break
            result = self.transcriber.transcribe(audio)
            if not result:
                continue
            try:
                translated = self.translator.translate(result.text, result.language)
            except TranslationError as exc:
                if result.language not in self._failed_sources:
                    logger.error("{}", exc)
                    self._failed_sources.add(result.language)
                translated = "—"
            print(f"[{result.language}] {result.text}\n     {translated}", flush=True)
            self.on_entry(result.text, translated)
        logger.info("Audio stream finished")


def run(args) -> int:
    logger.info(
        "Config: model={}, chunk={}s, source={}, target={}, backend={}, silence<{}",
        config.MODEL_SIZE, config.CHUNK_SECONDS, config.SOURCE_LANG,
        config.TARGET_LANG, config.TRANSLATE_BACKEND, config.SILENCE_THRESHOLD,
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
    transcriber = Transcriber(config.MODEL_SIZE, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE, config.SOURCE_LANG)

    # 4. Output
    overlay = app = None
    if not args.no_overlay:
        from overlay import Overlay, create_app

        app = create_app()
        overlay = Overlay(config.MAX_ENTRIES, config.OVERLAY_OPACITY)
        overlay.show()

    chunks: queue.Queue = queue.Queue(maxsize=0 if args.file else config.MAX_QUEUE_CHUNKS)
    pipeline = Pipeline(chunks, transcriber, translator, overlay.add_entry if overlay else lambda *_: None)

    # 5. Start
    capture = None
    if args.file:
        threading.Thread(
            target=file_chunks, args=(args.file, config.CHUNK_SECONDS, config.SILENCE_THRESHOLD, chunks), daemon=True
        ).start()
    else:
        capture = AudioCapture(device, config.CHUNK_SECONDS, config.SILENCE_THRESHOLD, chunks)
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
    parser.add_argument("--file", help="process an audio file instead of live capture (for testing)")
    parser.add_argument("--no-overlay", action="store_true", help="stdout only, no window")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if args.verbose else "INFO",
        format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> {message}",
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
