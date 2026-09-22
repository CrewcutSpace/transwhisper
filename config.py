"""All settings in one place. Every value can be overridden via environment
variables or a .env file in the project root."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _flag(name: str, default: bool) -> bool:
    return _get(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


# --- Audio capture ---------------------------------------------------------
# tap: capture what the Mac plays directly (needs the system audio recording permission).
# device: capture from an input device such as BlackHole (needs a Multi-Output Device).
AUDIO_SOURCE = _get("AUDIO_SOURCE", "tap").lower()
# Substring of the input device name, or its numeric index. Only used with AUDIO_SOURCE=device.
INPUT_DEVICE = _get("INPUT_DEVICE", "BlackHole")
# Audio quieter than this (RMS) is never treated as speech; filters out digital silence and hiss.
SILENCE_THRESHOLD = float(_get("SILENCE_THRESHOLD", "0.003"))

# --- Streaming -------------------------------------------------------------
# How often the line being spoken is re-transcribed and updated on screen.
PARTIAL_STEP_SECONDS = float(_get("PARTIAL_STEP_SECONDS", "0.4"))
# A pause this long ends the current line.
PAUSE_SECONDS = float(_get("PAUSE_SECONDS", "0.6"))
# Lines are force-split when someone talks this long without a pause.
MAX_SEGMENT_SECONDS = float(_get("MAX_SEGMENT_SECONDS", "12"))

# --- Transcription (faster-whisper) ----------------------------------------
# small: ~0.7 s per update, accurate (default) | base: ~0.3 s, mishears more | medium: slow.
# For English the English-only variant (base.en, ...) is picked automatically.
MODEL_SIZE = _get("MODEL_SIZE", "small")
WHISPER_DEVICE = _get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = _get("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_THREADS = int(_get("WHISPER_THREADS", "8"))
# Language spoken in the call: ISO code (en, de, ...) or "auto" to detect per line.
SOURCE_LANG = _get("SOURCE_LANG", "en").lower()

# --- Translation -----------------------------------------------------------
TARGET_LANG = _get("TARGET_LANG", "uk").lower()
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "").strip()
# apple: the translator built into macOS (free, offline, best quality of the free options)
# argos: local models, weaker | deepl: best quality, needs a key and has a character quota
TRANSLATE_BACKEND = _get("TRANSLATE_BACKEND", "deepl" if DEEPL_API_KEY else "apple").lower()
# Backend for the grey line that is still being spoken: it is re-translated twice a second,
# which would burn DeepL's character quota ~4x faster. auto | same | argos | apple | off
# auto = the main backend, unless that is DeepL, in which case local Argos.
LIVE_BACKEND = _get("LIVE_BACKEND", "auto").lower()
# Where downloaded Argos models are stored.
ARGOS_DIR = Path(_get("ARGOS_DIR", str(Path.home() / ".local/share/transwhisper/argos"))).expanduser()

# --- Overlay ---------------------------------------------------------------
MAX_ENTRIES = int(_get("MAX_ENTRIES", "10"))
OVERLAY_OPACITY = float(_get("OVERLAY_OPACITY", "0.85"))
# Window(s) to sit next to at startup, matched against title/app name (comma separated).
FOLLOW_WINDOW = _get("FOLLOW_WINDOW", "Meet,Zoom,Teams")
# Show a Dock icon (easier to find the app); 0 hides it, which can help over full-screen apps.
SHOW_IN_DOCK = _flag("SHOW_IN_DOCK", True)
# Show the original (e.g. English) text above the translation.
SHOW_ORIGINAL = _flag("SHOW_ORIGINAL", False)
