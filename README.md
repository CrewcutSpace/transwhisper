# transwhisper

Local real-time call translator for macOS (Apple Silicon).

Captures the audio of a video call (Meet / Zoom / Teams / anything), transcribes it locally
with faster-whisper and shows a live translation into the language you pick.

```
[call audio] → [BlackHole] → [faster-whisper: speech → text] → [translate] → [overlay + stdout]
```

The window shows the last ~10 lines. The line being spoken appears after ~1.5 s and is refined every second
(grey); when the speaker pauses it is finalised (white). The window stays on top, does not take focus from the call,
and can be dragged with the mouse. Final lines (original + translation) are also printed to stdout.

## 1. Install BlackHole

```sh
brew install blackhole-2ch
```

The driver is loaded only after restarting Core Audio: `sudo killall coreaudiod` (or reboot).
Check with `python main.py --list-devices` — `BlackHole 2ch` must be in the list.

## 2. Route call audio to BlackHole *and* your speakers

1. Open **Audio MIDI Setup**: `open -a "Audio MIDI Setup"` (or ⌘ Space → "Audio MIDI Setup").
   If you see the MIDI Studio window, switch with **Window → Show Audio Devices** (⌘1).
2. Click **+** in the bottom-left → **Create Multi-Output Device**.
3. Tick your real output (e.g. **MacBook Pro Speakers** or your headphones) **and** **BlackHole 2ch**.
4. Make your real output the **Primary Device** (top), and enable **Drift Correction** for BlackHole.
5. Optional: double-click the name and rename it to e.g. `Speakers + BlackHole`.
6. **System Settings → Sound → Output** → choose the **Multi-Output Device** (not BlackHole 2ch itself —
   then you would hear nothing). Quick switch: ⌥ Option-click the volume icon in the menu bar.
   Alternatively set it only as the speaker in the call app (Zoom/Teams/Meet audio settings).

Notes:
- The volume keys do not work with a Multi-Output Device; change volume on the real device in Audio MIDI Setup or in the call app.
- Your microphone is **not** captured: the app only reads BlackHole, which receives what is played to the speakers.
- On first run macOS asks the terminal for **microphone permission** (it applies to any audio input, BlackHole included). Allow it in System Settings → Privacy & Security → Microphone.

## 3. Python environment

Use Python 3.12 (some dependencies do not ship wheels for newer versions yet).

```sh
brew install python@3.12
cd transwhisper
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
| `SILENCE_THRESHOLD` | `0.003` | Audio quieter than this (RMS) is never treated as speech |
| `PARTIAL_STEP_SECONDS` | `1.0` | How often the line being spoken is updated |
| `PAUSE_SECONDS` | `0.6` | A pause this long finalises the line |
| `MAX_SEGMENT_SECONDS` | `8` | Long speech without pauses is split at a short breath around this length |
| `MODEL_SIZE` | `small` | Whisper model: `base` (fastest, less accurate), `small`, `medium` (slow) |
| `WHISPER_THREADS` | `8` | CPU threads for Whisper |
| `SOURCE_LANG` | `en` | Language spoken in the call, or `auto` to detect it per line |
| `TARGET_LANG` | `uk` | Language to translate into (`de`, `pl`, `es`, ...) |
| `TRANSLATE_BACKEND` | `argos`, or `deepl` if a key is set | Translation backend |
| `DEEPL_API_KEY` | — | DeepL API key (free tier), only from env/.env |
| `ARGOS_DIR` | `~/.local/share/transwhisper/argos` | Where Argos models are downloaded |
| `MAX_ENTRIES` | `10` | Lines kept in the window |
| `OVERLAY_OPACITY` | `0.85` | Window opacity |
| `SHOW_ORIGINAL` | `0` | `1` shows the original text above the translation |

### Translation backends

- **Argos** (default, no key needed): runs fully offline on your Mac. The model for the language pair (~70–100 MB)
  is downloaded on first run. If there is no direct model for a pair, it translates through English.
- **DeepL** (better quality, needs internet): create a free API account at <https://www.deepl.com/pro-api>
  (Free plan, 500 000 characters/month), copy the key from *Account → API Keys* and put it into `.env`:

  ```sh
  DEEPL_API_KEY=your-key:fx
  ```

  With a key present DeepL is used automatically. If the key is missing or invalid the app falls back to Argos.
  Note that with DeepL the call transcript is sent to DeepL's servers, and live updates re-translate the current
  line every second, which uses the character quota several times faster than final lines alone.

## 5. Run

```sh
python main.py                  # live capture
python main.py --list-devices   # show input devices
python main.py --file talk.wav  # play an audio file through the pipeline in real time (testing)
python main.py --no-overlay     # stdout only, no window
python main.py -v               # debug logging (latency of every update)
```

Stop with **Ctrl+C**.

Quick check without a call: play an English YouTube video with the Multi-Output Device selected
and run `python main.py` — the translation appears ~1.5 s after the speaker starts talking.

## License

MIT, see [LICENSE](LICENSE).
