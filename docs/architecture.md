# Architecture Overview

The application is divided into clear layers:

1. UI layer
2. service/session layer
3. core translation layer
4. data and model layer

The core translation path is:

~~~text
screen capture -> OCR -> text normalization -> corpus lookup -> rules
  -> local NMT (only when needed) -> final output -> cache -> event stream
~~~

A second input path feeds the *same* core when there is no text to read (a
livestream without subtitles):

~~~text
system loopback / microphone / WAV -> energy VAD phrases -> local ASR
  -> text normalization -> corpus lookup -> rules -> local NMT
  -> final output -> cache -> event stream
~~~

with one deliberate exception ahead of all of it: a **human correction**, which
outranks the dictionary and stops the model from being consulted at all. A wrong
answer from an authoritative corpus is not a gap, so the only thing that can fix
it is a person saying what the line should have said.

## Current implementation (2026-10-31)

The C++ mainline now implements the screen path end to end:

| Layer | Implementation |
| --- | --- |
| Capture | GDI BitBlt in PerMonitorV2 physical pixels; screen, window (DWM frame, follows movement), synthetic |
| Change detection | Downsampled grey difference; skips OCR while still |
| OCR | ONNX Runtime 1.30.0 + PP-OCRv4 det/cls/rec, optional model load |
| Corpus | Layered user > domain > general; scene and when_line/near/window; conflicts; mtime hot reload |
| Rules | affix / morpheme / template / transliterate with confidence and trace |
| NMT | CTranslate2 4.8.4 + NLLB-200 int8; corpus first, term protection, placeholder retry, fallback reason |
| Session | Single-writer event stream (v/seq/ts); stdin JSON command channel (pause/resume/target/scene/fps/shutdown) |
| UI | watashi_ui: Win32 layered, click-through, topmost, no-focus overlay; consumes watashi_cli --json-lines |
| Compatibility | The same JSONL stream is consumed by the Python overlay adapter (W1.4) |

The native UI does **not** link the engine. It spawns the engine binary next to
it and decodes the event stream; commands go back over stdin. This keeps the
contract boundary real: a different UI can replace watashi_ui without touching
OCR, corpus, rules or NMT.

Qt6 was evaluated in W3.0 and rejected for this host: no local Qt6, vcpkg source
build exceeded the one-day timebox, and Qt's install/package footprint was out
of proportion to a subtitle overlay. The decision and evidence are in
[docs/adr/0009-ui-toolkit.md](adr/0009-ui-toolkit.md).

## Local-first, not local-only

Every major component is designed to run on the local machine without cloud
access. That remains the default: no account, no network, no telemetry.

The same model boundary also accepts two opt-in alternatives, both off by default:

- a model server on another device on the local network (Ollama, vLLM, or
  llama.cpp server);
- an external HTTP API, configured by the user, with an explicit notice of what
  text leaves the machine.

The distinction that matters is not "local versus remote" but "default versus
explicit". A remote backend never enters the default path, never receives
screenshots or audio, and can be disabled per session. **v1.0 implements no
remote backend**; it only reserves the Translator seam.

## Interface boundaries

Three interfaces are deliberately separate, because "swap the model", "change the
settings" and "connect another application" have different trust and versioning
requirements.

| Interface | Owner | Current state |
| --- | --- | --- |
| Model backend | Translator protocol | corpus + rules + local CTranslate2 NMT implemented; remote backends reserved, off by default |
| Settings | settings_schema.py | schema is frozen; native settings page is W3.4, not implemented yet |
| Application collaboration | event/command envelope | JSON-lines events + stdin JSON commands implemented; Python overlay and native UI share them |

Rules:

1. Additive by default; changing the meaning of an existing field requires a
   SCHEMA_VERSION bump.
2. Loopback by default; any LAN listener needs explicit opt-in and authentication.
3. Data boundary first: a backend that sends text, screenshots or audio must state
   what it sends, to whom, and require consent.
4. UI never reimplements engine logic. It decodes events, sends commands, and
   renders; the engine owns OCR, corpus, rules and NMT.

## Plugin and rules principle

Rules, slang dictionaries, and domain-specific translation logic are stored in
local files or SQLite tables and can be extended without modifying the main engine.
The C++ plugin host is still v1.1 work; v1.0 ships the corpus/rules path only.
