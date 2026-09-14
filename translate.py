"""Pluggable translation backends: DeepL (API) and Argos (local, offline)."""

import json
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Protocol

from loguru import logger

ARGOS_INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
USER_AGENT = "transwhisper"
LATEST_ARGOS_VERSION = (1, 9)


class TranslationError(RuntimeError):
    pass


class Translator(Protocol):
    name: str

    def prepare(self, source: str) -> None:
        """Make sure the source->target pair is usable (download models, check support)."""

    def translate(self, text: str, source: str) -> str: ...


# --- Argos -----------------------------------------------------------------

def _version(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split(".") if x.isdigit())


def _split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s]


class _ArgosModel:
    """One Argos package: a CTranslate2 model plus its SentencePiece tokenizer."""

    def __init__(self, path: Path):
        import ctranslate2
        import sentencepiece

        if not (path / "sentencepiece.model").exists():
            raise TranslationError(f"Argos package {path.name} uses an unsupported tokenizer (only SentencePiece is supported)")
        metadata = json.loads((path / "metadata.json").read_text())
        self.target_prefix = metadata.get("target_prefix", "")
        self.translator = ctranslate2.Translator(str(path / "model"), device="cpu", compute_type="int8")
        self.tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(path / "sentencepiece.model"))

    def translate(self, text: str) -> str:
        sentences = _split_sentences(text)
        if not sentences:
            return ""
        tokens = [self.tokenizer.encode(s, out_type=str) for s in sentences]
        prefix = [[self.target_prefix]] * len(tokens) if self.target_prefix else None
        results = self.translator.translate_batch(
            tokens, target_prefix=prefix, beam_size=4, replace_unknowns=True, length_penalty=0.2
        )
        out = []
        for result in results:
            value = self.tokenizer.decode_pieces(result.hypotheses[0]).replace("▁", " ").strip()
            if self.target_prefix and value.startswith(self.target_prefix):
                value = value[len(self.target_prefix):].strip()
            out.append(value)
        return " ".join(out)


class ArgosTranslator:
    """Runs Argos Translate models directly with CTranslate2 (no torch/stanza needed).
    Packages are downloaded on demand; pairs without a direct model pivot through English."""

    name = "argos"

    def __init__(self, target: str, models_dir: Path):
        self.target = target
        self.models_dir = models_dir
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._index: dict[tuple[str, str], dict] | None = None
        self._models: dict[tuple[str, str], _ArgosModel] = {}

    def _load_index(self) -> dict[tuple[str, str], dict]:
        if self._index is None:
            index_file = self.models_dir / "index.json"
            if not index_file.exists():
                logger.info("Downloading Argos package index...")
                try:
                    urllib.request.urlretrieve(ARGOS_INDEX_URL, index_file)
                except OSError as exc:
                    raise TranslationError(f"Could not download Argos package index: {exc}") from exc
            packages = json.loads(index_file.read_text())
            self._index = {(p["from_code"], p["to_code"]): p for p in packages}
        return self._index

    def _route(self, source: str) -> list[tuple[str, str]]:
        index = self._load_index()
        if (source, self.target) in index:
            return [(source, self.target)]
        if (source, "en") in index and ("en", self.target) in index:
            return [(source, "en"), ("en", self.target)]
        supported = sorted({to for _, to in index})
        raise TranslationError(
            f"Argos has no model for {source} -> {self.target}. "
            f"Supported target languages: {', '.join(supported)}. Or use TRANSLATE_BACKEND=deepl."
        )

    def _model(self, pair: tuple[str, str]) -> _ArgosModel:
        if pair in self._models:
            return self._models[pair]
        package = self._load_index()[pair]
        installed = [p.parent for p in self.models_dir.glob(f"{package['code']}-*/model")]
        installed.sort(key=lambda p: _version(p.name.rsplit("-", 1)[1]))
        path = installed[-1] if installed else self._download(package)
        self._models[pair] = _ArgosModel(path)
        logger.info("Argos model {} loaded", path.name)
        return self._models[pair]

    @staticmethod
    def _candidates(package: dict) -> list[tuple[str, str]]:
        """(version, url) to try, best first. The public index lags behind: some pairs have
        a newer, noticeably better model on argos-net.com that is not listed yet."""
        listed = [(package["package_version"], url) for url in package["links"] if url.startswith("http")]
        if _version(package["package_version"]) < LATEST_ARGOS_VERSION:
            latest = ".".join(map(str, LATEST_ARGOS_VERSION))
            code_version = "_".join(map(str, LATEST_ARGOS_VERSION))
            listed.insert(0, (latest, f"https://argos-net.com/v1/{package['code']}-{code_version}.argosmodel"))
        return listed

    def _download(self, package: dict) -> Path:
        archive = self.models_dir / f"{package['code']}.argosmodel"
        logger.info("Downloading Argos model {}->{} (~100 MB, one time)...", package["from_code"], package["to_code"])
        last_error = None
        for version, url in self._candidates(package):
            path = self.models_dir / f"{package['code']}-{version}"
            # argos-net.com rejects the default Python-urllib user agent.
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=60) as response, open(archive, "wb") as f:
                    shutil.copyfileobj(response, f)
                break
            except OSError as exc:
                last_error = exc
        else:
            raise TranslationError(f"Could not download Argos model {package['code']}: {last_error}")

        shutil.rmtree(path, ignore_errors=True)
        with zipfile.ZipFile(archive) as zf:
            root = zf.namelist()[0].split("/")[0]
            zf.extractall(self.models_dir)
        extracted = self.models_dir / root
        if extracted != path:
            extracted.rename(path)
        archive.unlink()
        return path

    def prepare(self, source: str) -> None:
        for pair in self._route(source):
            self._model(pair)

    def translate(self, text: str, source: str) -> str:
        if source == self.target:
            return text
        for pair in self._route(source):
            text = self._model(pair).translate(text)
        return text


# --- DeepL -----------------------------------------------------------------

# DeepL requires a regional variant for some target languages.
_DEEPL_TARGET_VARIANTS = {"en": "EN-US", "pt": "PT-BR", "zh": "ZH-HANS"}


class DeepLTranslator:
    name = "deepl"

    def __init__(self, target: str, api_key: str):
        import deepl

        self._deepl = deepl
        self.client = deepl.DeepLClient(api_key)
        try:
            usage = self.client.get_usage()
            self.target_code = self._resolve_target(target)
            self.source_codes = {lang.code.lower() for lang in self.client.get_source_languages()}
        except deepl.DeepLException as exc:
            raise TranslationError(f"DeepL is not usable: {exc}") from exc
        if usage.character.valid:
            logger.info("DeepL usage: {} / {} characters this period", usage.character.count, usage.character.limit)

    def _resolve_target(self, target: str) -> str:
        available = {lang.code.upper() for lang in self.client.get_target_languages()}
        code = _DEEPL_TARGET_VARIANTS.get(target, target.upper())
        if code not in available:
            raise TranslationError(f"DeepL does not support target language '{target}'. Supported: {', '.join(sorted(available))}")
        return code

    def prepare(self, source: str) -> None:
        pass

    def translate(self, text: str, source: str) -> str:
        if self.target_code.split("-")[0].lower() == source:
            return text
        # Unknown source languages are left to DeepL's own detection.
        source_lang = source.upper() if source in self.source_codes else None
        try:
            return self.client.translate_text(text, source_lang=source_lang, target_lang=self.target_code).text
        except self._deepl.DeepLException as exc:
            raise TranslationError(f"DeepL request failed: {exc}") from exc


def create_translator(backend: str, target: str, api_key: str, argos_dir: Path) -> Translator:
    if backend == "deepl":
        if not api_key:
            logger.warning("TRANSLATE_BACKEND=deepl but DEEPL_API_KEY is not set, falling back to local Argos")
        else:
            try:
                return DeepLTranslator(target, api_key)
            except TranslationError as exc:
                logger.warning("{} Falling back to local Argos.", exc)
    elif backend != "argos":
        logger.warning("Unknown TRANSLATE_BACKEND '{}', using argos", backend)
    return ArgosTranslator(target, argos_dir)
