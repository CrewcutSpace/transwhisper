"""System audio capture from a virtual input device (BlackHole)."""

import logging
import queue
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

WHISPER_RATE = 16000


class DeviceNotFoundError(RuntimeError):
    pass


def list_input_devices() -> list[tuple[int, str]]:
    return [
        (i, d["name"])
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]


def find_input_device(spec: str) -> int:
    """Resolve a device index from a numeric index or a name substring."""
    inputs = list_input_devices()
    if spec.isdigit():
        index = int(spec)
        if any(i == index for i, _ in inputs):
            return index
    else:
        for i, name in inputs:
            if spec.lower() in name.lower():
                return i

    available = "\n".join(f"  [{i}] {name}" for i, name in inputs) or "  (none)"
    raise DeviceNotFoundError(
        f"Input device '{spec}' not found.\n"
        f"Available input devices:\n{available}\n\n"
        "Fix:\n"
        "  1. brew install blackhole-2ch  (then log out/in or reboot if it does not show up)\n"
        "  2. Set up a Multi-Output Device in Audio MIDI Setup (see README)\n"
        "  3. Or set INPUT_DEVICE to one of the names/indices above"
    )


def to_whisper_format(frames: np.ndarray, rate: int) -> np.ndarray:
    """Downmix to mono and resample to 16 kHz float32."""
    mono = frames.mean(axis=1) if frames.ndim == 2 else frames
    if rate == WHISPER_RATE:
        return mono.astype(np.float32)
    if rate % WHISPER_RATE == 0:
        # Integer ratio (e.g. 48k -> 16k): block averaging acts as a simple low-pass.
        factor = rate // WHISPER_RATE
        usable = len(mono) - len(mono) % factor
        return mono[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)
    target_len = int(len(mono) * WHISPER_RATE / rate)
    positions = np.linspace(0, len(mono) - 1, target_len)
    return np.interp(positions, np.arange(len(mono)), mono).astype(np.float32)


def is_silent(audio: np.ndarray, threshold: float) -> bool:
    return float(np.mean(np.abs(audio))) < threshold


def bounded_put(q: queue.Queue, item) -> None:
    """Put an item, dropping the oldest one if the queue is full."""
    while True:
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            try:
                q.get_nowait()
                log.warning("Transcription is falling behind, dropped an audio chunk")
            except queue.Empty:
                pass


class AudioCapture:
    """Reads the input device and pushes 16 kHz mono chunks of speech into a queue."""

    def __init__(self, device: int, chunk_seconds: float, silence_threshold: float, out: queue.Queue):
        info = sd.query_devices(device)
        self.device = device
        self.name = info["name"]
        self.rate = int(info["default_samplerate"])
        self.channels = min(2, info["max_input_channels"])
        self.chunk_frames = int(chunk_seconds * self.rate)
        self.silence_threshold = silence_threshold
        self.out = out

        self._blocks: queue.Queue[np.ndarray] = queue.Queue()
        self._stop = threading.Event()
        self._stream: sd.InputStream | None = None
        self._worker: threading.Thread | None = None

    def _callback(self, indata, frames, time_info, status):
        if status:
            log.debug("Audio status: %s", status)
        self._blocks.put(indata.copy())

    def _assemble(self):
        buffered: list[np.ndarray] = []
        count = 0
        while not self._stop.is_set():
            try:
                block = self._blocks.get(timeout=0.2)
            except queue.Empty:
                continue
            buffered.append(block)
            count += len(block)
            if count < self.chunk_frames:
                continue

            frames = np.concatenate(buffered)
            buffered, count = [], 0
            audio = to_whisper_format(frames, self.rate)
            if is_silent(audio, self.silence_threshold):
                log.debug("Skipped silent chunk (level %.5f)", float(np.mean(np.abs(audio))))
                continue
            bounded_put(self.out, audio)

    def start(self):
        self._stream = sd.InputStream(
            device=self.device,
            channels=self.channels,
            samplerate=self.rate,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        self._worker = threading.Thread(target=self._assemble, name="audio-assembler", daemon=True)
        self._worker.start()
        log.info("Capturing from [%d] %s @ %d Hz, %d ch", self.device, self.name, self.rate, self.channels)

    def stop(self):
        self._stop.set()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        if self._worker is not None:
            self._worker.join(timeout=1)
