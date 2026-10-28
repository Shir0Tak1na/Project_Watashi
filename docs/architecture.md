# Architecture Overview

The application is divided into clear layers:

1. UI layer
2. service layer
3. core translation layer
4. data and model layer

The core translation path is:

screen capture -> OCR -> text normalization -> dictionary rules
  -> local model -> final output -> cache

A second input path feeds the *same* core when there is no text to read (a
livestream without subtitles):

system loopback / microphone / WAV -> energy VAD phrases -> local ASR
  -> text normalization -> dictionary rules -> local model -> final output -> cache

with one deliberate exception ahead of all of it: a **human correction**, which
outranks the dictionary and stops the model from being consulted at all. A wrong
answer from an authoritative corpus is not a gap, so the only thing that can fix it
is a person saying what the line should have said.

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
screenshots or audio, and can be disabled per session.

## Interface boundaries

Three interfaces are deliberately separate, because "swap the model", "change the
settings" and "connect another application" have different trust and versioning
requirements.

| Interface | Owner | Current state |
| --- | --- | --- |
| Model backend | `translate.Translator` | corpus + rules and a local NMT backend; the `translator` plugin point is reserved |
| Settings | `settings_schema.py` | schema-driven settings and a loopback web panel |
| Application collaboration | event/command envelope | CLI JSON-lines, overlay, desktop window and web panel share one schema |

Rules:

1. Additive by default; changing the meaning of an existing field requires a
   `SCHEMA_VERSION` bump.
2. Loopback by default; any LAN listener needs explicit opt-in and authentication.
3. Data boundary first: a backend that sends text, screenshots or audio must state
   what it sends, to whom, and require consent.

## Plugin and rules principle

Rules, slang dictionaries, and domain-specific translation logic are stored in
local files or SQLite tables and can be extended without modifying the main engine.
