"""Speech-to-text with faster-whisper."""

from dataclasses import dataclass

import numpy as np
from faster_whisper import WhisperModel
from loguru import logger

ENGLISH_ONLY_MODELS = {"tiny", "base", "small", "medium"}


@dataclass
class Transcript:
    text: str
    language: str


class Transcriber:
    def __init__(self, model_size: str, device: str, compute_type: str, threads: int, language: str):
        # "auto" lets Whisper detect the language of every line.
        self.language = None if language == "auto" else language
        # English-only models are faster and more accurate for English.
        if language == "en" and model_size in ENGLISH_ONLY_MODELS:
            model_size = f"{model_size}.en"
        try:
            self.model = WhisperModel(model_size, device=device, compute_type=compute_type, cpu_threads=threads)
            logger.info("Whisper model '{}' loaded (device={}, compute_type={}, threads={})", model_size, device, compute_type, threads)
        except Exception as exc:
            if (device, compute_type) == ("cpu", "int8"):
                raise
            logger.warning("Could not load model with device={}/{} ({}), falling back to cpu/int8", device, compute_type, exc)
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=threads)
            logger.info("Whisper model '{}' loaded (device=cpu, compute_type=int8)", model_size)

    def transcribe(self, audio: np.ndarray, final: bool) -> Transcript | None:
        segments, info = self.model.transcribe(
            audio,
            language=self.language,
            # Partial lines are redone every second: favour speed, final lines favour accuracy.
            beam_size=5 if final else 1,
            vad_filter=final,
            # Lines are independent; carrying context over tends to cause repetition loops.
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            return None
        return Transcript(text=text, language=info.language)
