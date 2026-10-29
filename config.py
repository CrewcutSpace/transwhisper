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
# A sentence interrupted by a pause up to this long is joined with what follows and translated whole.
JOIN_PAUSE_SECONDS = float(_get("JOIN_PAUSE_SECONDS", "3"))
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
# apple: the translator built into macOS (free, offline, instant)
# ollama: a local LLM (best with idioms and context, ~1 s per sentence, needs Ollama running)
# argos: local models, weaker | deepl: very good, needs a key and has a character quota
TRANSLATE_BACKEND = _get("TRANSLATE_BACKEND", "deepl" if DEEPL_API_KEY else "apple").lower()
# Backend for the grey line that is still being spoken: it is re-translated twice a second,
# which would burn DeepL's character quota ~4x faster and is too much for a local LLM.
# auto | same | argos | apple | off
# auto = the main backend, unless that is DeepL (then Argos) or Ollama (then apple).
LIVE_BACKEND = _get("LIVE_BACKEND", "auto").lower()
# Ollama model and server for TRANSLATE_BACKEND=ollama. gemma3:12b translates best of the ones
# tried; gemma3:4b is ~3x faster but takes liberties. Install with: ollama pull gemma3:12b
OLLAMA_MODEL = _get("OLLAMA_MODEL", "gemma3:12b")
OLLAMA_URL = _get("OLLAMA_URL", "http://localhost:11434")
# Where downloaded Argos models are stored.
ARGOS_DIR = Path(_get("ARGOS_DIR", str(Path.home() / ".local/share/transwhisper/argos"))).expanduser()

# --- Overlay ---------------------------------------------------------------
# Lines kept in the window; scroll up to read them. Older ones are dropped.
MAX_ENTRIES = int(_get("MAX_ENTRIES", "500"))
OVERLAY_OPACITY = float(_get("OVERLAY_OPACITY", "0.85"))
# Windows to sit next to, matched against window title/app name, best first (comma separated).
# Checked every few seconds, so the window follows the call when it opens, moves or resizes.
FOLLOW_WINDOW = _get("FOLLOW_WINDOW", "Meet,Zoom,Teams,Safari,Chrome,Brave,Arc,Firefox,Edge")
# A Dock icon makes the app easy to find, but macOS then refuses to float the window over
# full-screen apps (Meet in full screen). Off by default; the menu bar item is the way in.
SHOW_IN_DOCK = _flag("SHOW_IN_DOCK", False)
# Show the original (e.g. English) text in a column to the left of the translation.
SHOW_ORIGINAL = _flag("SHOW_ORIGINAL", True)

# --- Saving the conversation -----------------------------------------------
# Append every finished line (original + translation) to a text file, one per run.
# Off by default; can also be switched on with --save or from the menu bar item.
SAVE_TRANSCRIPT = _flag("SAVE_TRANSCRIPT", False)
TRANSCRIPT_DIR = Path(_get("TRANSCRIPT_DIR", str(Path.home() / "Documents/transwhisper"))).expanduser()
