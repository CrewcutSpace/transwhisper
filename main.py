"""Entry point: audio -> transcription -> stdout."""

import argparse
import logging
import queue
import sys
import threading

import numpy as np

import config
from audio import WHISPER_RATE, AudioCapture, DeviceNotFoundError, find_input_device, is_silent, list_input_devices
from transcribe import Transcriber

log = logging.getLogger("translator")


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


def run(args) -> int:
    log.info(
        "Config: model=%s, chunk=%.1fs, source=%s, target=%s, backend=%s, silence<%.4f",
        config.MODEL_SIZE, config.CHUNK_SECONDS, config.SOURCE_LANG,
        config.TARGET_LANG, config.TRANSLATE_BACKEND, config.SILENCE_THRESHOLD,
    )

    chunks: queue.Queue = queue.Queue(maxsize=0 if args.file else config.MAX_QUEUE_CHUNKS)
    capture = None

    if args.file:
        log.info("Reading audio from file: %s", args.file)
    else:
        try:
            device = find_input_device(config.INPUT_DEVICE)
        except DeviceNotFoundError as exc:
            log.error("%s", exc)
            return 1
        log.info("Input device found: [%d]", device)

    log.info("Loading Whisper model '%s' (first run downloads it)...", config.MODEL_SIZE)
    transcriber = Transcriber(config.MODEL_SIZE, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE, config.SOURCE_LANG)

    if args.file:
        threading.Thread(
            target=file_chunks,
            args=(args.file, config.CHUNK_SECONDS, config.SILENCE_THRESHOLD, chunks),
            daemon=True,
        ).start()
    else:
        capture = AudioCapture(device, config.CHUNK_SECONDS, config.SILENCE_THRESHOLD, chunks)
        capture.start()

    log.info("Running. Press Ctrl+C to stop.")
    try:
        while True:
            audio = chunks.get()
            if audio is None:
                break
            result = transcriber.transcribe(audio)
            if result:
                print(f"[{result.language}] {result.text}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if capture:
            capture.stop()
        log.info("Stopped.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Local real-time call translator")
    parser.add_argument("--list-devices", action="store_true", help="list audio input devices and exit")
    parser.add_argument("--file", help="process an audio file instead of live capture (for testing)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Third-party libraries are chatty (model download requests etc.)
    for noisy in ("faster_whisper", "httpx", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.list_devices:
        for i, name in list_input_devices():
            print(f"[{i}] {name}")
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
