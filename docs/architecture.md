# Architecture Overview

The application is divided into clear layers:

1. UI layer
2. service layer
3. core translation layer
4. data and model layer

The core translation path is:

screen capture -> OCR -> text normalization -> dictionary rules -> local LLM -> final output -> cache

with one deliberate exception ahead of all of it: a **human correction**, which
outranks the dictionary and stops the model from being consulted at all. A wrong
answer from an authoritative corpus is not a gap, so the only thing that can fix it
is a person saying what the line should have said.

## Local-first principle

Every major component is designed to run on the local machine without cloud access. This gives better latency, privacy, and offline reliability.

## Plugin and rules principle

Rules, slang dictionaries, and domain-specific translation logic are stored in local files or SQLite tables and can be extended without modifying the main engine.
