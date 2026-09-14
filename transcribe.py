"""Speech-to-text with faster-whisper."""

from dataclasses import dataclass

import numpy as np
from faster_whisper import WhisperModel
from loguru import logger


@dataclass
class Transcript:
    text: str
    language: str


class Transcriber:
    def __init__(self, model_size: str, device: str, compute_type: str, language: str):
        # "auto" lets Whisper detect the language of every chunk.
        self.language = None if language == "auto" else language
        try:
            self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
            logger.info("Whisper model '{}' loaded (device={}, compute_type={})", model_size, device, compute_type)
        except Exception as exc:
            if (device, compute_type) == ("cpu", "int8"):
                raise
            logger.warning("Could not load model with device={}/{} ({}), falling back to cpu/int8", device, compute_type, exc)
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
            logger.info("Whisper model '{}' loaded (device=cpu, compute_type=int8)", model_size)

    def transcribe(self, audio: np.ndarray) -> Transcript | None:
        segments, info = self.model.transcribe(
            audio,
            language=self.language,
            vad_filter=True,
            # Chunks are independent; carrying context over tends to cause repetition loops.
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            return None
        return Transcript(text=text, language=info.language)
