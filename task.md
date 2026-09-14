TASK: Local real-time meeting translator (any→any, default EN→UK) for macOS (Apple Silicon)
Context

Build a personal, local, real-time speech translation assistant. During video calls (Google Meet / Zoom / Teams — any app), it captures the call audio, transcribes it (source language, default English), translates to a configurable target language (default Ukrainian), and shows both in a side overlay window. Purpose: help a non-native speaker follow fast/accented speech by glancing at the translated text.

Everything runs locally on the host Mac. No Docker, no VM (they cannot access host audio). Target machine: macOS on Apple Silicon (M-series).

Architecture
[call audio] → [audio capture via BlackHole] → [faster-whisper: speech→source text]
             → [translate source→target] → [overlay window: shows original + translation, auto-updating]
Requirements
1. Audio capture
Capture system/app audio (what comes out of the speakers), NOT the microphone.
Use BlackHole 2ch (brew install blackhole-2ch) as the virtual input device.
Document how to set up a Multi-Output Device (via Audio MIDI Setup) so the user hears the call AND audio is routed to BlackHole simultaneously.
Capture in chunks (~5s) with a queue; skip silent chunks (mean amplitude threshold).
Make the input device name/index configurable and auto-detect the BlackHole device index.
2. Transcription
Use faster-whisper, running locally. Default model small, switchable to medium via a config variable.
Use Apple Silicon acceleration where possible; if not stable, fall back to device="cpu", compute_type="int8".
Enable vad_filter=True (voice activity detection) to drop non-speech noise (notifications, music).
Source language configurable via SOURCE_LANG (default en; "auto" = Whisper language detection per chunk).
3. Translation (source→target) — pluggable backend
Target language configurable via TARGET_LANG (default uk). Any language supported by the chosen backend; unsupported pair → clear startup error.
Implement translation behind a small interface with two swappable backends:
DeepL API (free tier) — read API key from an environment variable DEEPL_API_KEY (never hardcode).
Argos Translate (fully local, offline, free) — as the no-API-key fallback. Auto-download needed language packages; pivot through English when no direct pair exists.
Backend selectable via a config/env variable (e.g. TRANSLATE_BACKEND=deepl|argos). Default to argos if no DeepL key present.
4. Output — overlay window
A small, always-on-top, semi-transparent window (Python — PyQt6 or tkinter) shown on the side of the screen.
Shows a rolling feed: each entry = original line + translated line beneath it.
Keep only the last ~10 entries visible; auto-scroll.
Must not steal focus from the call window.
Also print the same to stdout (for debugging / headless run).
5. Config & UX
Single config section at top (or a .env / config.py): model size, chunk length, input device name, translation backend, source language, target language, silence threshold.
Clean start/stop (Ctrl+C).
Clear console logging: startup checks (device found? model loaded? translation backend ready?).
Deliverables
README.md — exact setup for macOS Apple Silicon: brew installs, Audio MIDI Multi-Output setup steps, Python venv, pip install, how to get/set the DeepL key OR use Argos, how to run.
requirements.txt.
Source code, structured (not one giant file): e.g. audio.py (capture), transcribe.py, translate.py (with both backends), overlay.py (window), main.py (wires it together), config.py.
Sensible error handling: if BlackHole device not found → clear message with fix; if DeepL key missing → auto-fallback to Argos with a notice.
Constraints
No hardcoded secrets — DeepL key only from env var.
No Docker/VM — native macOS only.
Keep dependencies minimal and free.
Prioritize a working end-to-end MVP first (audio → EN text → UK text → visible in window), then polish.
Definition of Done
Playing an English YouTube video with audio routed through BlackHole produces, in near-real-time, a side window showing English transcription + translation into TARGET_LANG, updating in ~5s chunks, without capturing microphone or unrelated app sounds when the Multi-Output is set correctly.
Runs with python main.py after following README.
Works with the local Argos backend even with zero API keys.
First step

Start by scaffolding the project, writing README.md and requirements.txt, then implement audio capture + transcription and print to stdout. Confirm that works before adding translation and the overlay window.
