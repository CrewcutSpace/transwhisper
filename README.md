# transwhisper

Local real-time call translator for macOS (Apple Silicon).

Captures the audio of a video call (Meet / Zoom / Teams / anything), transcribes it locally
with faster-whisper and shows a live translation into the language you pick.

```
[call audio] → [BlackHole] → [faster-whisper: speech → text] → [translate] → [overlay + stdout]
```

The window shows the last ~10 lines. The line being spoken appears after ~1.5 s. A word is shown only once it has been
recognised the same way twice, so shown text does not jump around. Each finished sentence or clause is translated
once and stays fixed (white); only the part still being spoken is updated (grey).

At startup the window places itself next to the call window (a Chrome window with Meet, Zoom, Teams — see
`FOLLOW_WINDOW`), stays on top of everything **including apps in full screen**, in every Space, and never takes focus
away from the call. Drag it anywhere with the mouse: that position is remembered for runs where no call window is
found (`~/.local/share/transwhisper/overlay.json`). Final lines (original + translation) are also printed to stdout.

## 1. Allow system audio recording

transwhisper listens to what your Mac plays, using a Core Audio process tap (macOS 14.2+).
Nothing is routed anywhere, so **your speakers, headphones and the volume keys keep working as usual**.

macOS asks for one permission, and for a program started from a terminal it does not show a prompt —
switch it on manually, once:

1. **System Settings → Privacy & Security → Screen & System Audio Recording**
   (`open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"`).
2. Enable your terminal (iTerm, Terminal, ...). Use **+** to add it if it is not listed.
3. **Restart the terminal** — the permission only applies to newly started processes.

The permission is named after screen recording, but nothing is recorded from the screen: on macOS this
single switch also covers system audio. Without it macOS hands the app perfect silence, and it says so in the log.

The helper that does the capture (`audiotap/`, ~130 lines of Swift) is built automatically on first run;
it needs the Xcode command line tools (`xcode-select --install`). Build it by hand with `audiotap/build.sh`.

## 2. Alternative: BlackHole (AUDIO_SOURCE=device)

Only needed if the permission above is not an option, or on macOS older than 14.2. Downside: with a
Multi-Output Device the volume keys stop working.

1. `brew install blackhole-2ch`, then `sudo killall coreaudiod` (or reboot).
2. Open **Audio MIDI Setup**: `open -a "Audio MIDI Setup"`. If you see the MIDI Studio window, switch with
   **Window → Show Audio Devices** (⌘1).
3. **+** in the bottom-left → **Create Multi-Output Device**.
4. Tick your real output (e.g. **MacBook Pro Speakers**) **and** **BlackHole 2ch**; make the real output the
   **Primary Device** and enable **Drift Correction** for BlackHole.
5. **System Settings → Sound → Output** → choose the **Multi-Output Device** (not BlackHole itself — then you
   would hear nothing). Quick switch: ⌥ Option-click the volume icon in the menu bar.
6. Run with `AUDIO_SOURCE=device`.

In both cases your **microphone is not captured**: only what is played to the speakers.

## 3. Python environment

Use Python 3.12 (some dependencies do not ship wheels for newer versions yet).

```sh
brew install python@3.12
cd transwhisper
python3.12 -m venv .venv
source .venv/bin/activate     # fish: source .venv/bin/activate.fish
pip install -r requirements.txt
```

The Whisper model (~150 MB for `base`, ~500 MB for `small`) is downloaded on first run and cached in `~/.cache/huggingface`.

## 4. Configure

Settings are read from environment variables or a `.env` file (`cp .env.example .env`).
Defaults live in `config.py`.

| Variable | Default | Meaning |
|---|---|---|
| `AUDIO_SOURCE` | `tap` | `tap`: capture what the Mac plays; `device`: capture from an input device (BlackHole) |
| `INPUT_DEVICE` | `BlackHole` | Input device name substring or index, with `AUDIO_SOURCE=device` (`python main.py --list-devices`) |
| `SILENCE_THRESHOLD` | `0.003` | Audio quieter than this (RMS) is never treated as speech |
| `PARTIAL_STEP_SECONDS` | `0.4` | How often the line being spoken is updated |
| `PAUSE_SECONDS` | `0.6` | A pause this long finalises the line |
| `MAX_SEGMENT_SECONDS` | `12` | Long speech without pauses is split at a short breath around this length |
| `MODEL_SIZE` | `small` | Whisper model: `small` (~0.7 s per update), `base` (~0.3 s, mishears more), `medium` (slow). With `SOURCE_LANG=en` the English-only variant is used |
| `WHISPER_THREADS` | `8` | CPU threads for Whisper |
| `SOURCE_LANG` | `en` | Language spoken in the call, or `auto` to detect it per line |
| `TARGET_LANG` | `uk` | Language to translate into (`de`, `pl`, `es`, ...) |
| `TRANSLATE_BACKEND` | `apple`, or `deepl` if a key is set | Translation backend: `apple` (macOS), `argos`, `deepl` |
| `LIVE_BACKEND` | `auto` | Backend for the line still being spoken: `auto`, `same`, `argos`, `apple`, `off` |
| `DEEPL_API_KEY` | — | DeepL API key (free tier), only from env/.env |
| `ARGOS_DIR` | `~/.local/share/transwhisper/argos` | Where Argos models are downloaded |
| `MAX_ENTRIES` | `10` | Lines kept in the window |
| `OVERLAY_OPACITY` | `0.85` | Window opacity |
| `FOLLOW_WINDOW` | `Meet,Zoom,Teams` | Window titles/apps to place the overlay next to at startup |
| `SHOW_ORIGINAL` | `0` | `1` shows the original text above the translation |

### Translation backends

- **macOS** (`apple`, the default): the translator built into macOS — free, offline, no key, no quota, and clearly
  better than Argos (~90 ms per sentence). Install the language pair once in **System Settings → General →
  Language & Region → Translation Languages**; an app without a window cannot start that download, so the app
  says so and falls back to Argos if the pair is missing. Needs macOS 15+ and the Xcode command line tools.
  The helper (`appletranslate/`) is built on first run. With `SOURCE_LANG=auto` the language Whisper reports for
  each line is used; a pair whose language pack is missing falls back to the text being left untranslated.
- **Argos** (`argos`, no key needed): local models, weaker quality. The model for the language pair (~70–100 MB)
  is downloaded on first run. If there is no direct model for a pair, it translates through English.
- **DeepL** (better quality, needs internet): create a free API account at <https://www.deepl.com/pro-api>
  (Developer plan: a **one-time** credit of 1 000 000 characters, no monthly reset), copy the key from
  *Account → API Keys* and put it into `.env`:

  ```sh
  DEEPL_API_KEY=your-key:fx
  ```

  With a key present DeepL is used automatically. If the key is missing or invalid the app falls back to Argos.
  Note that with DeepL the call transcript is sent to DeepL's servers.

  To stretch the one-time credit, the grey line that is still being spoken is translated by local Argos and only
  finished clauses go to DeepL (`LIVE_BACKEND`). Measured on a test recording: ~890 characters per minute of
  speech, so 1 000 000 characters last roughly **19 hours** of talking. Translating the live line with DeepL too
  (`LIVE_BACKEND=same`) costs ~3 500 characters per minute, about 4.7 hours.

## 5. Run

```sh
python main.py                  # live capture
python main.py --list-devices   # show input devices
python main.py --file talk.wav  # play an audio file through the pipeline in real time (testing)
python main.py --no-overlay     # stdout only, no window
python main.py -v               # debug logging (latency of every update)
```

Stop with **Ctrl+C**.

Quick check without a call: play an English YouTube video
and run `python main.py` — the translation appears ~1.5 s after the speaker starts talking.

## License

MIT, see [LICENSE](LICENSE).
