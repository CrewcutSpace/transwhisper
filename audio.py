"""Audio capture: either straight from what the Mac plays (Core Audio process tap, no extra
setup) or from a virtual input device such as BlackHole."""

import json
import queue
import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
from loguru import logger

WHISPER_RATE = 16000
BLOCK_SECONDS = 0.1

TAP_DIR = Path(__file__).resolve().parent / "audiotap"
TAP_BINARY = TAP_DIR / "audiotap"

PERMISSION_HINT = (
    "macOS is not letting this app hear the system audio.\n"
    "Fix: System Settings -> Privacy & Security -> Screen & System Audio Recording -> "
    "enable your terminal (iTerm/Terminal), then restart the terminal.\n"
    "(The permission is named after screen recording, but it also covers system audio.)\n"
    "Alternative: set AUDIO_SOURCE=device to capture via BlackHole instead."
)


class DeviceNotFoundError(RuntimeError):
    pass


class TapUnavailableError(RuntimeError):
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
        "  1. brew install blackhole-2ch, then: sudo killall coreaudiod (or reboot)\n"
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


def build_tap() -> Path:
    """Compile the small Swift helper that taps the system audio."""
    if TAP_BINARY.exists() and TAP_BINARY.stat().st_mtime >= (TAP_DIR / "main.swift").stat().st_mtime:
        return TAP_BINARY
    if not shutil.which("swiftc"):
        raise TapUnavailableError(
            "swiftc not found, so the system audio helper cannot be built.\n"
            "Fix: install the Xcode command line tools (xcode-select --install), "
            "or set AUDIO_SOURCE=device to capture via BlackHole."
        )
    logger.info("Building the system audio helper (one time)...")
    build = subprocess.run([str(TAP_DIR / "build.sh")], capture_output=True, text=True)
    if build.returncode != 0 or not TAP_BINARY.exists():
        raise TapUnavailableError(f"Could not build the system audio helper:\n{build.stdout}{build.stderr}")
    return TAP_BINARY


class SystemAudioCapture:
    """Captures whatever the Mac is playing, through the Swift helper. No virtual device,
    no Multi-Output Device, and the volume keys keep working."""

    name = "system audio"

    def __init__(self, out: queue.Queue):
        self.out = out
        self._process: subprocess.Popen | None = None
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    def _read(self, rate: int):
        block_bytes = int(rate * BLOCK_SECONDS) * 4  # float32 mono
        silent_blocks = 0
        warned = False
        assert self._process is not None and self._process.stdout is not None
        while not self._stop.is_set():
            data = self._process.stdout.read(block_bytes)
            if not data:
                break
            block = np.frombuffer(data, dtype=np.float32)
            if not warned:
                # Without the permission macOS delivers a stream of perfect silence.
                silent_blocks = silent_blocks + 1 if not np.any(block) else 0
                if silent_blocks > 5 / BLOCK_SECONDS:
                    logger.warning(PERMISSION_HINT)
                    warned = True
            self.out.put(to_whisper_format(block, rate))

    def start(self):
        binary = build_tap()
        self._process = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        header = self._process.stdout.readline()
        if not header:
            error = self._process.stderr.read().decode().strip()
            raise TapUnavailableError(error or PERMISSION_HINT)
        rate = int(json.loads(header)["rate"])
        self._worker = threading.Thread(target=self._read, args=(rate,), name="audio-tap", daemon=True)
        self._worker.start()
        logger.info("Capturing the system audio @ {} Hz (no BlackHole needed)", rate)

    def stop(self):
        self._stop.set()
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._worker is not None:
            self._worker.join(timeout=1)


class AudioCapture:
    """Reads the input device and pushes short 16 kHz mono blocks into a queue."""

    name = "input device"

    def __init__(self, device: int, out: queue.Queue):
        info = sd.query_devices(device)
        self.device = device
        self.name = info["name"]
        self.rate = int(info["default_samplerate"])
        self.channels = min(2, info["max_input_channels"])
        self.out = out

        self._raw: queue.Queue[np.ndarray] = queue.Queue()
        self._stop = threading.Event()
        self._stream: sd.InputStream | None = None
        self._worker: threading.Thread | None = None

    def _callback(self, indata, frames, time_info, status):
        if status:
            logger.debug("Audio status: {}", status)
        self._raw.put(indata.copy())

    def _convert(self):
        while not self._stop.is_set():
            try:
                block = self._raw.get(timeout=0.2)
            except queue.Empty:
                continue
            self.out.put(to_whisper_format(block, self.rate))

    def start(self):
        self._stream = sd.InputStream(
            device=self.device,
            channels=self.channels,
            samplerate=self.rate,
            blocksize=int(self.rate * BLOCK_SECONDS),
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        self._worker = threading.Thread(target=self._convert, name="audio-convert", daemon=True)
        self._worker.start()
        logger.info("Capturing from [{}] {} @ {} Hz, {} ch", self.device, self.name, self.rate, self.channels)

    def stop(self):
        self._stop.set()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        if self._worker is not None:
            self._worker.join(timeout=1)
