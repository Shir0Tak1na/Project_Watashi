#!/usr/bin/env python3
"""Live audio translation self check. Headless: no screen, no microphone, no model.

The live path is the one feature that cannot be proved by looking at a screen,
because the input is sound. These checks keep it honest without requiring any
hardware:

* the phrase segmenter is driven with synthetic tones, so its silence and
  max-length cuts are asserted rather than assumed;
* the WAV source is driven with a real file written by the check itself;
* the whole chain (audio -> segment -> scripted transcript -> corpus translation
  -> event) runs end to end with no model download;
* the Session integration is exercised with the audio and transcriber builders
  replaced, proving that live mode does not build OCR and that the existing
  commands keep their meaning.

What is deliberately not checked here: a real WASAPI/PulseAudio device and a
real Whisper model. Both need a machine and a download, and pretending a fake
covers them would be the kind of green check this repository keeps trying to
avoid. The fake is named scripted everywhere so it cannot be mistaken for ASR.

    run.cmd selfcheck_live
"""

from __future__ import annotations

import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402
from watashi.config import AppConfig  # noqa: E402
from watashi.pipeline import PipelineConfig  # noqa: E402
from watashi.translate import CorpusTranslator  # noqa: E402
from watashi.session import Session, build_corpus  # noqa: E402
from watashi import live  # noqa: E402


SAMPLE_RATE = live.SAMPLE_RATE


def tone(seconds: float, amplitude: float = 0.35, hz: float = 220.0) -> np.ndarray:
    count = int(round(SAMPLE_RATE * seconds))
    t = np.arange(count, dtype=np.float32) / float(SAMPLE_RATE)
    return (amplitude * np.sin(2.0 * np.pi * hz * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(round(SAMPLE_RATE * seconds)), dtype=np.float32)


def feed(segmenter: live.SpeechSegmenter, samples: np.ndarray, block: int = 1600) -> list:
    found = []
    for start in range(0, samples.size, block):
        found.extend(segmenter.feed(samples[start : start + block]))
    return found


def write_wav(path: Path, samples: np.ndarray, rate: int = SAMPLE_RATE, channels: int = 1) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    if channels > 1:
        pcm = np.repeat(pcm[:, None], channels, axis=1).reshape(-1)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Live audio translation self check (no screen, no microphone, no model)")
    print("=" * 78)

    # ---------------------------------------------------------------- #
    check.section("the segmenter cuts at silence, not at a timer")
    segmenter = live.SpeechSegmenter(silence_ms=500, min_speech_ms=250, max_segment_s=8.0)
    audio = np.concatenate([silence(0.4), tone(1.2), silence(0.8)])
    segments = feed(segmenter, audio)
    check.check(
        "a phrase surrounded by silence becomes exactly one segment",
        len(segments) == 1,
        f"got {len(segments)}",
    )
    if segments:
        check.check(
            "its speech duration is close to the tone, not the whole buffer",
            1.0 <= segments[0].speech_s <= 1.4,
            f"speech_s={segments[0].speech_s:.2f}",
        )
        check.check(
            "it includes the pre-roll and the trailing silence for context",
            segments[0].duration_s > segments[0].speech_s,
            f"{segments[0].duration_s:.2f} s vs {segments[0].speech_s:.2f} s",
        )

    quiet = live.SpeechSegmenter(silence_ms=500, min_speech_ms=250)
    check.check(
        "pure silence produces no transcript",
        feed(quiet, silence(3.0)) == [],
    )
    blip = live.SpeechSegmenter(silence_ms=500, min_speech_ms=250)
    check.check(
        "a 100 ms click is not speech",
        feed(blip, np.concatenate([silence(0.2), tone(0.1), silence(1.0)])) == [],
    )

    long_form = live.SpeechSegmenter(silence_ms=500, min_speech_ms=250, max_segment_s=3.0)
    many = feed(long_form, tone(9.0), block=1600)
    check.check(
        "a continuous monologue is cut into segments instead of waiting forever",
        len(many) >= 3,
        f"got {len(many)}",
    )
    check.check(
        "and no segment exceeds the configured maximum by more than a block",
        all(segment.duration_s <= 3.0 + 0.2 for segment in many),
        f"max={max((s.duration_s for s in many), default=0):.2f} s",
    )

    flushed = live.SpeechSegmenter(silence_ms=500, min_speech_ms=250)
    feed(flushed, tone(1.0))
    check.check(
        "flush emits a phrase that is still in flight",
        flushed.flush() is not None,
    )

    # ---------------------------------------------------------------- #
    check.section("resampling and the WAV source")
    source_48 = tone(1.0)
    source_48 = np.repeat(source_48, 3)[: 48000 * 1]
    converted = live.resample(source_48, 48000, 16000)
    check.check(
        "48 kHz to 16 kHz keeps one third of the samples",
        abs(converted.size - 16000) <= 1,
        f"got {converted.size}",
    )
    check.check(
        "and the result still carries the signal",
        float(np.sqrt(np.mean(np.square(converted)))) > 0.1,
    )

    tmp = Path(tempfile.mkdtemp(prefix="watashi-live-"))
    try:
        wav = tmp / "phrase.wav"
        write_wav(wav, np.concatenate([silence(0.2), tone(0.8), silence(0.3)]))
        wav_source = live.WavFileAudioSource(wav, block_ms=100)
        wav_source.open()
        chunks = []
        while True:
            chunk = wav_source.read()
            if chunk is None:
                break
            chunks.append(chunk)
        wav_source.close()
        total = sum(chunk.size for chunk in chunks)
        check.check(
            "the WAV source returns the whole file",
            abs(total - 20800) <= 2,
            f"got {total} samples",
        )
        check.check(
            "and then reports the end rather than looping",
            wav_source.read() is None,
        )

        stereo = tmp / "stereo48.wav"
        # 0.5 s at 48 kHz: three samples for every 16 kHz tone sample.
        write_wav(stereo, np.repeat(tone(0.5), 3), rate=48000, channels=2)
        stereo_source = live.WavFileAudioSource(stereo, block_ms=100)
        stereo_source.open()
        stereo_total = 0
        while True:
            chunk = stereo_source.read()
            if chunk is None:
                break
            stereo_total += chunk.size
        stereo_source.close()
        check.check(
            "a 48 kHz stereo file becomes 16 kHz mono",
            abs(stereo_total - 8000) <= 2,
            f"got {stereo_total} samples",
        )

        # ------------------------------------------------------------ #
        check.section("device choice is explicit, not accidental")
        devices = [
            live.AudioDevice(0, "Speakers (Realtek)", loopback=True, default=True),
            live.AudioDevice(1, "Headset Microphone", loopback=False, default=True),
            live.AudioDevice(2, "HDMI Output", loopback=True, default=False),
        ]
        chosen = live.select_device(devices, "", loopback=True)
        check.check(
            "the default loopback wins when no device is named",
            chosen is not None and chosen.name.startswith("Speakers"),
            getattr(chosen, "name", None),
        )
        check.check(
            "a microphone request does not silently return the speaker",
            live.select_device(devices, "", loopback=False).name == "Headset Microphone",
        )
        check.check(
            "a name substring selects the matching loopback",
            live.select_device(devices, "hdmi", loopback=True).index == 2,
        )
        check.check(
            "an index selects exactly that entry",
            live.select_device(devices, "2", loopback=True).index == 2,
        )
        check.check(
            "an index of the wrong kind is refused",
            live.select_device(devices, "1", loopback=True) is None,
        )
        check.check(
            "an unknown name is refused",
            live.select_device(devices, "not-a-device", loopback=True) is None,
        )

        original_soundcard = live._soundcard_module
        try:
            live._soundcard_module = lambda: (_ for _ in ()).throw(
                live.LiveUnavailable("soundcard is not installed")
            )
            try:
                live.SystemAudioSource().open()
                check.check("a missing audio package is reported", False, "no error")
            except live.LiveUnavailable as exc:
                check.check(
                    "a missing audio package is reported with an instruction",
                    "pip install" in str(exc) or "soundcard" in str(exc),
                    str(exc)[:80],
                )
        finally:
            live._soundcard_module = original_soundcard

        # ------------------------------------------------------------ #
        check.section("the faster-whisper adapter calls the API the way it is documented")
        import types

        captured: dict = {}

        class _FakeSegment:
            text = " Hello world. "
            avg_logprob = -0.2

        class _FakeInfo:
            language = "en"
            language_probability = 0.99

        class _FakeWhisper:
            def __init__(self, *args, **kwargs):
                captured["model_args"] = args
                captured["model_kwargs"] = kwargs

            def transcribe(self, audio, **kwargs):
                captured["transcribe_kwargs"] = kwargs
                captured["audio_size"] = getattr(audio, "size", None)
                return [_FakeSegment()], _FakeInfo()

        fake_module = types.ModuleType("faster_whisper")
        fake_module.WhisperModel = _FakeWhisper  # type: ignore[attr-defined]
        saved_module = sys.modules.get("faster_whisper")
        sys.modules["faster_whisper"] = fake_module
        try:
            transcriber = live.FasterWhisperTranscriber("tiny", language="auto", threads=3)
            transcriber.ensure_loaded()
            result = transcriber.transcribe(np.zeros(1600, dtype=np.float32))
            check.check(
                "auto language is passed as None, which is how Whisper means detect",
                captured["transcribe_kwargs"].get("language") is None,
                captured["transcribe_kwargs"].get("language"),
            )
            check.check(
                "the model is constructed on the CPU with the configured threads",
                captured["model_kwargs"].get("device") == "cpu"
                and captured["model_kwargs"].get("cpu_threads") == 3,
                str(captured["model_kwargs"]),
            )
            check.check(
                "the result text is cleaned and the detected language is kept",
                result.text == "Hello world." and result.language == "en",
                f"{result.text!r}/{result.language!r}",
            )
            check.check(
                "the backend names the model, for the trace line",
                result.backend == "faster-whisper:tiny",
                result.backend,
            )
            first_kwargs = dict(captured["transcribe_kwargs"])
            check.check(
                "the first phrase has no context to carry",
                "initial_prompt" not in first_kwargs,
                str(first_kwargs.get("initial_prompt")),
            )
            transcriber.transcribe(np.zeros(1600, dtype=np.float32))
            second_kwargs = dict(captured["transcribe_kwargs"])
            check.check(
                "the next phrase sees the previous one as a prompt",
                "Hello world." in str(second_kwargs.get("initial_prompt", "")),
                str(second_kwargs.get("initial_prompt")),
            )
            check.check(
                "and condition_on_previous_text stays off, so one hallucination "
                "cannot poison the rest",
                second_kwargs.get("condition_on_previous_text") is False,
                str(second_kwargs.get("condition_on_previous_text")),
            )

            no_context = live.FasterWhisperTranscriber("tiny", context_chars=0)
            no_context.ensure_loaded()
            no_context.transcribe(np.zeros(1600, dtype=np.float32))
            no_context.transcribe(np.zeros(1600, dtype=np.float32))
            check.check(
                "context_chars 0 really disables the prompt",
                "initial_prompt" not in captured["transcribe_kwargs"],
                str(captured["transcribe_kwargs"].get("initial_prompt")),
            )
            check.check(
                "a non-speech label such as [Music] is dropped, not drawn",
                live._clean_transcript("[Music]") == ""
                and live._clean_transcript("Hello there.") == "Hello there.",
            )
        finally:
            if saved_module is None:
                sys.modules.pop("faster_whisper", None)
            else:
                sys.modules["faster_whisper"] = saved_module

        # ------------------------------------------------------------ #
        check.section("the whole chain runs without a model or a microphone")
        config = AppConfig.load()
        config.translation["nmt_model"] = None
        translator = CorpusTranslator(build_corpus(config))
        chain_wav = tmp / "chain.wav"
        write_wav(
            chain_wav,
            np.concatenate(
                [
                    silence(0.3),
                    tone(1.0),
                    silence(0.7),
                    silence(0.3),
                    tone(1.0),
                    silence(0.7),
                ]
            ),
        )
        updates = []
        stats = []
        pipeline = live.LivePipeline(
            source=live.WavFileAudioSource(chain_wav, realtime=False),
            transcriber=live.ScriptedTranscriber(
                ["the sword intent of this sect is a myth", "gg wp noob"]
            ),
            translator=translator,
            settings=live.LiveSettings(
                file=str(chain_wav),
                silence_ms=500,
                min_speech_ms=250,
                max_segment_s=5.0,
            ),
            config=PipelineConfig(target_lang="zh-CN", source_lang="auto"),
            on_update=updates.append,
            on_stats=stats.append,
        )
        pipeline.start(warmup=True)
        deadline = time.perf_counter() + 10.0
        while time.perf_counter() < deadline and not pipeline.finished:
            time.sleep(0.05)
        pipeline.stop()

        check.check(
            "two phrases produce two subtitle updates",
            len(updates) == 2,
            f"got {len(updates)}",
        )
        check.check(
            "each update carries source and target text",
            all(update.source_text and update.target_text for update in updates),
            [u.source_text[:24] for u in updates],
        )
        check.check(
            "the corpus really translated at least one of them",
            any(update.target_text != update.source_text for update in updates),
            [u.target_text[:24] for u in updates],
        )
        check.check(
            "the trace names the ASR backend, so a line is traceable",
            all("ASR" in update.trace for update in updates),
            updates[0].trace.splitlines()[0] if updates else "",
        )
        check.check(
            "stats are labelled live rather than OCR",
            pipeline.stats().mode == "live",
            pipeline.stats().mode,
        )
        pipeline.pause()
        check.check(
            "pause is visible in stats, so every surface can draw it",
            pipeline.stats().paused is True,
        )
        pipeline.resume()
        check.check(
            "resume clears it again",
            pipeline.stats().paused is False,
        )
        detail = pipeline.to_dict()
        check.check(
            "the live counters include segments and audio seconds",
            detail.get("segments") == 2 and detail.get("audio_seconds", 0) > 3.0,
            f"segments={detail.get('segments')} audio={detail.get('audio_seconds')}",
        )
        check.check(
            "the scripted transcriber is named as such, never as a real model",
            detail.get("asr_backend") == "scripted",
            detail.get("asr_backend"),
        )

        # ------------------------------------------------------------ #
        check.section("Session treats live audio as the input, not as OCR")
        live_config = AppConfig.load()
        live_config.translation["nmt_model"] = None
        live_config.live.update(
            {
                "enabled": True,
                "source": "file",
                "file": str(chain_wav),
                "asr_model": "scripted",
                "local_files_only": True,
            }
        )
        session_updates = []
        session = Session(
            live_config,
            translator=CorpusTranslator(build_corpus(live_config)),
        )

        original_source = live.build_audio_source
        original_transcriber = live.build_transcriber
        try:
            live.build_audio_source = lambda _config, _settings=None: live.WavFileAudioSource(
                chain_wav, realtime=False
            )
            live.build_transcriber = lambda _config, _settings=None: live.ScriptedTranscriber(
                ["the sword intent of this sect is a myth", "gg wp noob"]
            )
            session.build()
            check.check(
                "live mode does not build the OCR engine",
                session._ocr is None,  # noqa: SLF001 - the point of the check
                f"ocr={session._ocr!r}",
            )
            check.check(
                "info reports audio capture and the listening source",
                session.info().get("capture_mode") == "audio"
                and "音频" in str(session.info().get("region")),
                str(session.info().get("region")),
            )
            check.check(
                "a screen region command is refused in live mode",
                session.command("set_region", {"region": "0,0,100,100"}).get("ok") is False,
            )
            check.check(
                "so is a capture fps command",
                session.command("set_fps", {"value": 5}).get("ok") is False,
            )

            session.subscribe()  # a surface must be able to attach
            session.start()
            deadline = time.perf_counter() + 10.0
            while time.perf_counter() < deadline and not session.finished:
                time.sleep(0.05)
            check.check(
                "a finite live source finishes on its own",
                session.finished,
            )
            session.stop()
            check.check(
                "the session still answers a target language change",
                session.command("set_target_lang", {"target_lang": "en"}).get("ok") is True,
            )
        finally:
            live.build_audio_source = original_source
            live.build_transcriber = original_transcriber

        # ------------------------------------------------------------ #
        check.section("configuration describes the live path in one place")
        defaults = AppConfig.load().live
        check.check(
            "the shipped config has a live section with every key",
            all(
                key in defaults
                for key in (
                    "enabled",
                    "source",
                    "device",
                    "file",
                    "language",
                    "asr_model",
                    "silence_ms",
                    "min_speech_ms",
                    "max_segment_s",
                )
            ),
            sorted(defaults)[:8],
        )
        check.check(
            "live is off by default, so the screen path is unchanged",
            defaults.get("enabled") is False,
        )
        settings = live.LiveSettings.from_config(live_config)
        check.check(
            "LiveSettings reads the file source and language from the config",
            settings.source == "file" and settings.asr_model == "scripted",
            f"{settings.source}/{settings.asr_model}",
        )

        # fetch_model --asr writes <repo>/models/whisper/<name>; the default
        # live.asr_model is a bare size name, so resolution has to look there or
        # the documented prefetch would be ignored in favour of a download.
        fake_proto = tmp / "prototype"
        prefetched = tmp / "models" / "whisper" / "tiny"
        fake_proto.mkdir(parents=True, exist_ok=True)
        prefetched.mkdir(parents=True, exist_ok=True)

        class _Config:
            base_dir = fake_proto
            live: dict = {}

        resolved, _root = live._resolve_model(_Config(), "tiny")
        check.check(
            "a prefetched model directory is used by its bare size name",
            Path(resolved) == prefetched,
            resolved,
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    return check.report()


if __name__ == "__main__":
    raise SystemExit(main())
