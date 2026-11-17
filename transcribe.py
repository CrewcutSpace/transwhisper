"""Speech-to-text with Whisper: on the Apple GPU with mlx-whisper, or on the CPU with faster-whisper."""

import os
from dataclasses import dataclass

import numpy as np
from loguru import logger

ENGLISH_ONLY_MODELS = {"tiny", "base", "small", "medium"}
# MLX conversions of the models live under different names on Hugging Face.
MLX_REPOS = {"large-v3-turbo": "mlx-community/whisper-large-v3-turbo"}

# Hugging Face's default downloader (xet) broke off half way here; the plain one works.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


@dataclass
class Transcript:
    text: str
    language: str
    # Start time in seconds of every word of `text`, only for final lines.
    starts: list[float] | None = None


class Transcriber:
    def __init__(self, model_size: str, device: str, compute_type: str, threads: int, language: str):
        # "auto" lets Whisper detect the language of every line.
        self.language = None if language == "auto" else language
        self.mlx_repo = None
        if device == "mlx":
            try:
                self._load_mlx(model_size)
                return
            except Exception as exc:
                logger.warning("Could not load Whisper on the GPU ({}), falling back to 'small' on the CPU", exc)
                model_size, device, compute_type = "small", "cpu", "int8"
        self._load_faster_whisper(model_size, device, compute_type, threads, language)

    def _load_mlx(self, model_size: str):
        import mlx_whisper

        repo = model_size if "/" in model_size else MLX_REPOS.get(model_size, f"mlx-community/whisper-{model_size}-mlx")
        # Loads (and on the first run downloads) the model, and warms it up.
        mlx_whisper.transcribe(np.zeros(16000, np.float32), path_or_hf_repo=repo, language=self.language or "en")
        self._mlx = mlx_whisper
        self.mlx_repo = repo
        logger.info("Whisper model '{}' loaded (device=mlx)", repo)

    def _load_faster_whisper(self, model_size: str, device: str, compute_type: str, threads: int, language: str):
        from faster_whisper import WhisperModel

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
        if self.mlx_repo:
            return self._transcribe_mlx(audio, final)
        segments, info = self.model.transcribe(
            audio,
            language=self.language,
            # Partial lines are redone every second: favour speed, final lines favour accuracy.
            beam_size=5 if final else 1,
            vad_filter=final,
            # Lines are independent; carrying context over tends to cause repetition loops.
            condition_on_previous_text=False,
            # Word times let the last sentence of a final line be cut out and heard again with
            # the next line, if it continues after a pause. They need segment timestamps: without
            # them faster-whisper is 3x slower and makes up words at the end.
            without_timestamps=not final,
            word_timestamps=final,
        )
        if not final:
            text = " ".join(s.text.strip() for s in segments).strip()
            return Transcript(text=text, language=info.language) if text else None
        words = [(w.word.strip(), w.start) for s in segments for w in s.words if w.word.strip()]
        if not words:
            return None
        return Transcript(text=" ".join(w for w, _ in words), language=info.language, starts=[t for _, t in words])

    def _transcribe_mlx(self, audio: np.ndarray, final: bool) -> Transcript | None:
        # Same choices as with faster-whisper above, minus beam search and VAD, which mlx-whisper lacks.
        result = self._mlx.transcribe(
            audio.astype(np.float32, copy=False),
            path_or_hf_repo=self.mlx_repo,
            language=self.language,
            condition_on_previous_text=False,
            without_timestamps=not final,
            word_timestamps=final,
        )
        language = result.get("language") or self.language or "en"
        if not final:
            text = result["text"].strip()
            return Transcript(text=text, language=language) if text else None
        words = [(w["word"].strip(), w["start"]) for s in result["segments"] for w in s.get("words", []) if w["word"].strip()]
        if not words:
            return None
        return Transcript(text=" ".join(w for w, _ in words), language=language, starts=[t for _, t in words])
