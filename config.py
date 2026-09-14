"""All settings in one place. Every value can be overridden via environment
variables or a .env file in the project root."""

import os

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


# --- Audio capture ---------------------------------------------------------
# Substring of the input device name, or its numeric index.
INPUT_DEVICE = _get("INPUT_DEVICE", "BlackHole")
CHUNK_SECONDS = float(_get("CHUNK_SECONDS", "5"))
# Chunks whose mean absolute amplitude is below this are skipped as silence.
SILENCE_THRESHOLD = float(_get("SILENCE_THRESHOLD", "0.003"))
# If transcription falls behind, oldest chunks are dropped beyond this backlog.
MAX_QUEUE_CHUNKS = int(_get("MAX_QUEUE_CHUNKS", "3"))

# --- Transcription (faster-whisper) ----------------------------------------
MODEL_SIZE = _get("MODEL_SIZE", "small")  # small | medium | ...
WHISPER_DEVICE = _get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = _get("WHISPER_COMPUTE_TYPE", "int8")
# Language spoken in the call: ISO code (en, de, ...) or "auto" to detect per chunk.
SOURCE_LANG = _get("SOURCE_LANG", "en").lower()

# --- Translation -----------------------------------------------------------
TARGET_LANG = _get("TARGET_LANG", "uk").lower()
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "").strip()
# deepl | argos. Defaults to deepl only when a key is present.
TRANSLATE_BACKEND = _get("TRANSLATE_BACKEND", "deepl" if DEEPL_API_KEY else "argos").lower()

# --- Overlay ---------------------------------------------------------------
MAX_ENTRIES = int(_get("MAX_ENTRIES", "10"))
