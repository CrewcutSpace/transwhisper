# transwhisper

Local real-time call translator for macOS (Apple Silicon).

Captures the audio of a video call (Meet / Zoom / Teams / anything), transcribes it locally
with faster-whisper and shows the original text together with a translation into the language you pick.

```
[call audio] → [BlackHole] → [faster-whisper: speech → text] → [translate] → [overlay + stdout]
```

> **Status:** audio capture + transcription → stdout work. Translation and the overlay window are in progress.

## 1. Install BlackHole

```sh
brew install blackhole-2ch
```

If `BlackHole 2ch` does not appear as an audio device afterwards, log out and back in (or reboot).

## 2. Route call audio to BlackHole *and* your speakers

1. Open **Audio MIDI Setup** (Spotlight → "Audio MIDI Setup").
2. Click **+** in the bottom-left → **Create Multi-Output Device**.
3. Tick your real output (e.g. **MacBook Pro Speakers** or your headphones) **and** **BlackHole 2ch**.
4. Make your real output the **Primary Device** (top), and enable **Drift Correction** for BlackHole.
5. Optional: double-click the name and rename it to e.g. `Speakers + BlackHole`.
6. **System Settings → Sound → Output** → choose the Multi-Output Device.
   Alternatively set it only as the speaker in the call app (Zoom/Teams/Meet audio settings).

Notes:
- The volume keys do not work with a Multi-Output Device; change volume on the real device in Audio MIDI Setup or in the call app.
- Your microphone is **not** captured: the app only reads BlackHole, which receives what is played to the speakers.
- On first run macOS asks the terminal for **microphone permission** (it applies to any audio input, BlackHole included). Allow it in System Settings → Privacy & Security → Microphone.

## 3. Python environment

Use Python 3.12 (some dependencies do not ship wheels for newer versions yet).

```sh
brew install python@3.12
cd translator
python3.12 -m venv .venv
source .venv/bin/activate     # fish: source .venv/bin/activate.fish
pip install -r requirements.txt
```

The Whisper model (~500 MB for `small`) is downloaded on first run and cached in `~/.cache/huggingface`.

## 4. Configure

Settings are read from environment variables or a `.env` file (`cp .env.example .env`).
Defaults live in `config.py`.

| Variable | Default | Meaning |
|---|---|---|
| `INPUT_DEVICE` | `BlackHole` | Input device name substring or index (`python main.py --list-devices`) |
| `CHUNK_SECONDS` | `5` | Length of an audio chunk |
| `SILENCE_THRESHOLD` | `0.003` | Chunks with mean amplitude below this are skipped |
| `MODEL_SIZE` | `small` | Whisper model: `small` (fast) or `medium` (more accurate, slower) |
| `SOURCE_LANG` | `en` | Language spoken in the call, or `auto` to detect it per chunk |
| `TARGET_LANG` | `uk` | Language to translate into (`de`, `pl`, `es`, ...) |
| `TRANSLATE_BACKEND` | `argos`, or `deepl` if a key is set | Translation backend |
| `DEEPL_API_KEY` | — | DeepL API key (free tier), only from env/.env |

## 5. Run

```sh
python main.py                  # live capture
python main.py --list-devices   # show input devices
python main.py --file talk.wav  # run the pipeline on an audio file (testing)
python main.py -v               # debug logging (shows skipped silent chunks)
```

Stop with **Ctrl+C**.

Quick check without a call: play an English YouTube video with the Multi-Output Device selected
and run `python main.py` — transcribed lines appear every ~5 seconds.

## License

MIT, see [LICENSE](LICENSE).
