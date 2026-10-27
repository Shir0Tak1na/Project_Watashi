# Live audio design

Status: implemented in prototype/watashi/live.py, covered by selfcheck_live.
The C++ tree does not have this path yet.

## The problem it solves

Screen translation needs text on screen. A livestream with no subtitles has
none: the words are in the audio. OCR cannot be extended to read sound, so the
input has to change while everything after it stays the same.

## Shape

    AudioSource (system loopback / microphone / WAV)
        -> SpeechSegmenter
        -> Transcriber (faster-whisper, local)
        -> Pipeline.translate_texts()
        -> OverlayUpdate -> Session -> every existing surface

Two separations make this maintainable:

- AudioSource and Transcriber are protocols with one real implementation each
  and one deterministic fake (WavFileAudioSource, ScriptedTranscriber). The fake
  is named scripted, so no report can mistake it for a real model.
- Translation is not reimplemented. LivePipeline owns a real Pipeline and calls
  translate_texts, which runs the same _make_update path as a screen frame. If
  corpus precedence or scene handling changes, live audio changes with it.

## Lifecycle

Session.build() checks config.live.enabled. In live mode it builds the shared
translator, skips build_ocr entirely, and constructs LivePipeline. Session
retains its event boundary; no surface learns that the input is audio. The
pipeline exposes the same control surface (start/stop/pause/resume/stats/
to_dict/config/paused/kind), and screen-only commands (set_region, use_window,
set_fps, set_diff_threshold) are refused with a reason instead of silently
doing nothing.

A finite source (a WAV file) sets finished when its queue drains, and the CLI
loop exits on that. A live device never finishes.

## Segmentation

The gate is RMS against an adaptive noise floor and an absolute minimum. The
floor is learned only while the gate is closed. A phrase ends after silence_ms
of quiet, or at max_segment_s while speech continues. Pre-roll keeps the first
phoneme; a 200 ms overlap tail is carried into the next phrase after a forced
cut. min_speech_ms rejects clicks and door slams.

This is deliberately not a learned VAD. A learned VAD would be a third model to
install and a second source of model-download failures; the cost is that loud
music can count as speech. That limitation is documented to the user rather
than hidden.

## Recognition

faster-whisper, CPU, int8 by default. condition_on_previous_text is false so a
hallucinated phrase cannot poison the next one. Its VAD runs on each phrase as a
second filter, with no_speech_threshold 0.6 and a compression-ratio guard. The
model is loaded lazily in start(), so a failed load is reported as a startup
error the user can act on, not as a traceback on the first subtitle.

Context is carried by a bounded `initial_prompt` instead: the tail of the
previous phrase (`live.context_chars`, default 120 characters) is passed to
Whisper so names and technical terms are not re-guessed from scratch every
phrase. This is deliberately not `condition_on_previous_text`, which would turn
one bad phrase into a chain. Set `context_chars: 0` to disable it.

The model spec is either a size name (downloaded once by faster-whisper) or a
directory prefetched by fetch_model.py --asr. live.local_files_only makes the
offline promise explicit.

## Stats

OverlayStats gained a mode field. In live mode ocr_ms carries ASR time because
that is the field existing surfaces already show; the label changes to 语音 and
FPS is replaced by a segment count. The alternative -- reusing OCR labels and
lying about what was measured -- is exactly what this project avoids.

## Failure modes and what the user sees

- soundcard missing: LiveUnavailable naming the pip install, printed by the CLI.
- no matching device: the listing command and the requested device are named.
- faster-whisper missing: the same, with the install command.
- model missing and local_files_only set: an error pointing at fetch_model --asr.
- device busy / format refused: all attempted sample rates are listed.
- music mistaken for speech: a false subtitle; tune vad_threshold or turn
  asr_vad on.
- ASR slower than speech: the queue drops the oldest phrase, so the subtitle
  stays on "now" instead of drifting. dropped_segments counts it.

## Measured so far, and what is still not measured

A generated English speech WAV (5.7 s, two sentences) has been run through the
whole path on this development machine with faster-whisper small int8:

- both phrases were segmented, transcribed and translated correctly
  ("他突破了虚空境界." / "这宗门的剑意是个神话.");
- ASR + NMT took **2.7-2.8 s per phrase**, so a 5.7 s clip took 6.7 s to
  process. That is **not yet real-time** on this CPU under its current load;
  `asr_model: base`/`tiny` or a quieter machine is the lever.

Still unmeasured: word error rate, a real WASAPI loopback device, and long
streams where ASR must keep up with continuous speech. The self check proves
segmentation, resampling, wiring, session integration and failure messages; it
does not pretend to prove recognition quality. Measure with --stats-json and the
live counters before quoting a number.
