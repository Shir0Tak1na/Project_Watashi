"""Live audio translation: listen to a stream, transcribe it, translate it.

Why this path exists
--------------------
Screen OCR can only read text that is already on screen. A livestream without
subtitles has none: the words exist only as sound. This module turns that sound
into the *same* OverlayUpdate the screen pipeline publishes, so the overlay,
the desktop window, the web panel, the JSON-lines stream, corrections and the
corpus editor all work unchanged. It is a second input path, not a second
application.

::

    system loopback / microphone / WAV file
      -> energy VAD + phrase segmentation   (this module)
      -> faster-whisper (local, CPU)        (this module)
      -> corpus + rules + local NMT         (pipeline.py, unchanged)
      -> OverlayUpdate                      (events.py, unchanged)

Everything in the main path is local. The ASR weights are fetched once, like the
NMT weights; after that watching a stream never touches the network.

Why phrase segmentation rather than a single long transcription: Whisper works
on windows of up to 30 s and a subtitle that only appears every 30 s is not a
subtitle. Cutting at silence keeps the end-to-end latency at roughly one phrase
(silence_ms + ASR + translation) instead of one paragraph. The energy gate is
deliberately simple -- a learned VAD would add another model to install -- and
its known weakness (music counted as speech) is documented in the README rather
than hidden.
"""

from __future__ import annotations

import queue
import re
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from .events import OverlayStats, OverlayUpdate
from .pipeline import Pipeline, PipelineConfig
from .translate import Translator

#: Whisper is trained on 16 kHz mono float32. Everything upstream resamples to
#: this, so one sample rate is the contract for the whole module.
SAMPLE_RATE = 16000

#: How much audio one read returns. 100 ms is small enough that silence is found
#: where it happens and large enough that the capture thread is not a busy loop.
BLOCK_MS = 100


class LiveUnavailable(RuntimeError):
    """The live path cannot start, with an explanation the user can act on."""


# --------------------------------------------------------------------------- #
# devices
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AudioDevice:
    """One capturable audio endpoint, as the CLI and settings surface show it."""

    index: int
    name: str
    loopback: bool
    default: bool

    def label(self) -> str:
        flags = []
        if self.loopback:
            flags.append("loopback")
        if self.default:
            flags.append("default")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        return f"[{self.index}] {self.name}{suffix}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "loopback": self.loopback,
            "default": self.default,
        }


def _soundcard_module():
    """Import soundcard lazily, with an instruction instead of a traceback.

    Imported inside the function on purpose: the fragmenter, the fake
    transcriber and the tests must work on a machine where no audio package is
    installed. A module-level import would make selfcheck_live impossible to run
    in CI without WASAPI/PulseAudio headers.
    """
    try:
        import soundcard as sc  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on the machine
        raise LiveUnavailable(
            "直播模式需要 soundcard 才能读取系统声音："
            "pip install -r prototype/requirements.txt"
            f"（{type(exc).__name__}: {exc}）"
        ) from exc
    return sc


def list_audio_devices() -> list[AudioDevice]:
    """Every microphone plus every loopback endpoint, in a stable order.

    Windows exposes the sound of the default speaker as a *microphone* named
    after that speaker; PulseAudio exposes monitor sources the same way. Calling
    these microphones is the API, not a claim about the hardware.
    """
    sc = _soundcard_module()
    default_speaker = ""
    try:
        default_speaker = str(sc.default_speaker().name)
    except Exception:
        pass
    default_mic = ""
    try:
        default_mic = str(sc.default_microphone().name)
    except Exception:
        pass

    devices: list[AudioDevice] = []
    try:
        microphones = list(sc.all_microphones(include_loopback=True))
    except Exception as exc:
        raise LiveUnavailable(f"无法枚举音频设备：{exc}") from exc

    for microphone in microphones:
        name = str(getattr(microphone, "name", microphone))
        loopback = bool(getattr(microphone, "isloopback", False))
        if not loopback:
            # Some backends do not set the flag but do put monitor/loopback in
            # the name. Treat that as the same thing rather than offering the
            # user two entries with one behaviour.
            lowered = name.casefold()
            loopback = "loopback" in lowered or "monitor of" in lowered
        is_default = name == default_mic or (
            loopback and default_speaker and default_speaker.casefold() in name.casefold()
        )
        devices.append(
            AudioDevice(index=len(devices), name=name, loopback=loopback, default=is_default)
        )
    return devices


def select_device(
    devices: Sequence[AudioDevice], spec: str, *, loopback: bool
) -> AudioDevice | None:
    """Pick one endpoint from a listing.

    spec is what a user typed: empty (the default for this kind), a list
    index, or a case-insensitive substring of the name. An index is only
    meaningful for the listing it came from, which is why --list-audio-devices
    prints it next to the name.
    """
    candidates = [d for d in devices if d.loopback == loopback]
    text = (spec or "").strip()
    if not text:
        for device in candidates:
            if device.default:
                return device
        return candidates[0] if candidates else None

    if text.isdigit():
        index = int(text)
        for device in devices:
            if device.index == index and device.loopback == loopback:
                return device
        return None

    folded = text.casefold()
    matches = [d for d in candidates if folded in d.name.casefold()]
    if not matches:
        return None
    for device in matches:
        if device.default:
            return device
    return matches[0]


# --------------------------------------------------------------------------- #
# sample helpers
# --------------------------------------------------------------------------- #


def resample(samples: np.ndarray, source_rate: int, target_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Linear resampling, enough for speech and dependency free.

    Whisper is robust to the slight high-frequency loss of linear interpolation;
    adding scipy or a resampling library for this would cost far more than the
    accuracy it buys. The function is pure and asserted directly, because a
    wrong length here makes every downstream timestamp wrong.
    """
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if source_rate == target_rate or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    count = max(1, int(round(audio.size * target_rate / float(source_rate))))
    if count == audio.size:
        return audio.astype(np.float32, copy=False)
    source_positions = np.linspace(0.0, audio.size - 1, num=audio.size, dtype=np.float64)
    target_positions = np.linspace(0.0, audio.size - 1, num=count, dtype=np.float64)
    return np.interp(target_positions, source_positions, audio).astype(np.float32)


def _to_mono(data: Any) -> np.ndarray:
    audio = np.asarray(data, dtype=np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    return audio.reshape(-1)


# --------------------------------------------------------------------------- #
# audio sources
# --------------------------------------------------------------------------- #


class AudioSource:
    """Something that produces 16 kHz mono float32 blocks.

    read returns None only when the source has ended (a finished WAV file). A
    live device never ends; it blocks for at most one block.
    """

    KIND = "audio"

    def open(self) -> None:
        raise NotImplementedError

    def read(self) -> np.ndarray | None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        return "audio"


class SystemAudioSource(AudioSource):
    """WASAPI loopback (Windows), PulseAudio monitor (Linux), or a microphone."""

    def __init__(
        self,
        *,
        device: str = "",
        loopback: bool = True,
        target_rate: int = SAMPLE_RATE,
        capture_rate: int = 48000,
        block_ms: int = BLOCK_MS,
    ) -> None:
        self.device = device
        self.loopback = loopback
        self.target_rate = target_rate
        self.capture_rate = capture_rate
        self.block_ms = block_ms
        self._recorder: Any = None
        self._rate = capture_rate
        self._device: AudioDevice | None = None
        self._sc: Any = None

    def open(self) -> None:
        sc = _soundcard_module()
        self._sc = sc
        devices = list_audio_devices()
        chosen = select_device(devices, self.device, loopback=self.loopback)
        if chosen is None:
            kind = "系统声音（loopback）" if self.loopback else "麦克风"
            raise LiveUnavailable(
                f"找不到{kind}设备 {self.device!r}。"
                "先运行 --list-audio-devices 看可用的名字和序号。"
            )
        # Resolving by name rather than index: the listing is a snapshot, and
        # another program can hot-plug a device between the listing and the open.
        try:
            microphone = sc.get_microphone(chosen.name, include_loopback=True)
        except Exception:
            microphone = sc.get_microphone(chosen.index, include_loopback=True)

        errors: list[str] = []
        rates = [self.capture_rate] + [r for r in (48000, 44100, 16000) if r != self.capture_rate]
        for rate in rates:
            block = max(256, int(rate * self.block_ms / 1000.0))
            recorder = None
            try:
                recorder = microphone.recorder(samplerate=rate, channels=1, blocksize=block)
                recorder.__enter__()
            except Exception as exc:
                errors.append(f"{rate} Hz: {exc}")
                try:
                    if recorder is not None:
                        recorder.__exit__(None, None, None)
                except Exception:
                    pass
                continue
            self._recorder = recorder
            self._rate = rate
            self._device = chosen
            return
        name = chosen.name
        raise LiveUnavailable(f"无法打开音频设备 {name!r}：" + "; ".join(errors))

    @property
    def rate(self) -> int:
        return self._rate

    def read(self) -> np.ndarray | None:
        if self._recorder is None:
            return None
        block = max(256, int(self._rate * self.block_ms / 1000.0))
        data = self._recorder.record(numframes=block)
        audio = _to_mono(data)
        return resample(audio, self._rate, self.target_rate)

    def close(self) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder.__exit__(None, None, None)
        except Exception:
            pass
        self._recorder = None

    def describe(self) -> str:
        name = self._device.name if self._device else (self.device or "默认设备")
        kind = "系统声音" if self.loopback else "麦克风"
        return f"{kind}：{name}（{self._rate} Hz -> {self.target_rate} Hz）"


class WavFileAudioSource(AudioSource):
    """A WAV file as an audio source, for demos and deterministic tests.

    PCM 8/16/24/32-bit is accepted; the file is resampled to 16 kHz mono. The
    default is *not* real time, so a short test file is processed as fast as the
    ASR allows; realtime=True paces it like a live stream, which is what a user
    testing the overlay wants.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        target_rate: int = SAMPLE_RATE,
        block_ms: int = BLOCK_MS,
        realtime: bool = False,
        loop: bool = False,
    ) -> None:
        self.path = Path(path)
        self.target_rate = target_rate
        self.block_ms = block_ms
        self.realtime = realtime
        self.loop = loop
        self._wave: wave.Wave_read | None = None
        self._channels = 1
        self._width = 2
        self._rate = target_rate
        self._started = 0.0
        self._played_frames = 0

    def open(self) -> None:
        if not self.path.is_file():
            raise LiveUnavailable(f"音频文件不存在：{self.path}")
        try:
            handle = wave.open(str(self.path), "rb")
        except wave.Error as exc:
            raise LiveUnavailable(
                f"无法读取 {self.path}：只支持未压缩的 PCM WAV（{exc}）"
            ) from exc
        self._wave = handle
        self._channels = handle.getnchannels()
        self._width = handle.getsampwidth()
        self._rate = handle.getframerate()
        if self._width not in (1, 2, 3, 4):
            raise LiveUnavailable(f"不支持的 WAV 采样位宽：{self._width * 8} bit")
        self._started = time.perf_counter()
        self._played_frames = 0

    def _decode(self, raw: bytes) -> np.ndarray:
        width = self._width
        if width == 1:
            data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif width == 2:
            data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif width == 3:
            pieces = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            values = pieces[:, 0] | (pieces[:, 1] << 8) | (pieces[:, 2] << 16)
            values = np.where(values & 0x800000, values - 0x1000000, values)
            data = values.astype(np.float32) / 8388608.0
        else:
            data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        if self._channels > 1:
            data = data.reshape(-1, self._channels).mean(axis=1)
        return data.astype(np.float32)

    def read(self) -> np.ndarray | None:
        if self._wave is None:
            return None
        frames_wanted = max(256, int(self._rate * self.block_ms / 1000.0))
        raw = self._wave.readframes(frames_wanted)
        if not raw:
            if self.loop:
                self._wave.rewind()
                raw = self._wave.readframes(frames_wanted)
                self._started = time.perf_counter()
                self._played_frames = 0
            if not raw:
                return None
        audio = self._decode(raw)
        self._played_frames += frames_wanted
        if self.realtime:
            due = self._started + self._played_frames / float(self._rate)
            delay = due - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        return resample(audio, self._rate, self.target_rate)

    def close(self) -> None:
        if self._wave is not None:
            try:
                self._wave.close()
            except Exception:
                pass
            self._wave = None

    def describe(self) -> str:
        return f"音频文件：{self.path}（{self._rate} Hz, {self._channels} 声道）"


# --------------------------------------------------------------------------- #
# phrase segmentation
# --------------------------------------------------------------------------- #


@dataclass
class AudioSegment:
    """One phrase of speech, ready for the recogniser."""

    samples: np.ndarray
    speech_s: float = 0.0
    duration_s: float = 0.0

    @property
    def enough(self) -> bool:
        return self.samples.size > 0


class SpeechSegmenter:
    """Cut a continuous stream into phrases at silence.

    The gate is an adaptive RMS threshold: the noise floor is learned while the
    room is quiet, so a fan or background music does not permanently count as
    speech. A segment ends after silence_ms of quiet, or at max_segment_s even
    while speech continues -- a monologue must still produce subtitles, and
    Whisper's 30 s window is far too long to wait for.

    min_speech_ms is the anti-click guard: a door slam is a loud frame and must
    not become a subtitle.
    """

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        silence_ms: int = 600,
        min_speech_ms: int = 300,
        max_segment_s: float = 8.0,
        threshold: float = 2.5,
        min_rms: float = 0.004,
        pre_roll_ms: int = 300,
        overlap_ms: int = 200,
    ) -> None:
        self.sample_rate = sample_rate
        self.silence_frames = max(1, int(sample_rate * silence_ms / 1000.0))
        self.min_speech_frames = max(1, int(sample_rate * min_speech_ms / 1000.0))
        self.max_frames = max(1, int(sample_rate * max_segment_s))
        self.threshold = max(1.01, float(threshold))
        self.min_rms = max(1e-6, float(min_rms))
        self.pre_roll_frames = max(0, int(sample_rate * pre_roll_ms / 1000.0))
        self.overlap_frames = max(0, int(sample_rate * overlap_ms / 1000.0))
        self._pre_roll: deque[np.ndarray] = deque()
        self._buffer: list[np.ndarray] = []
        self._buffer_frames = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._active = False
        self._floor = self.min_rms * 0.5
        self.segments_emitted = 0

    def _append_pre_roll(self, chunk: np.ndarray) -> None:
        if self.pre_roll_frames <= 0:
            return
        self._pre_roll.append(chunk)
        total = sum(len(part) for part in self._pre_roll)
        while self._pre_roll and total - len(self._pre_roll[0]) >= self.pre_roll_frames:
            total -= len(self._pre_roll.popleft())

    def _emit(self) -> AudioSegment | None:
        if not self._active or not self._buffer:
            self._reset()
            return None
        if self._speech_frames < self.min_speech_frames:
            self._reset()
            return None
        samples = np.concatenate(self._buffer).astype(np.float32, copy=False)
        segment = AudioSegment(
            samples=samples,
            speech_s=self._speech_frames / float(self.sample_rate),
            duration_s=samples.size / float(self.sample_rate),
        )
        self.segments_emitted += 1
        self._reset()
        return segment

    def _reset(self) -> None:
        self._buffer = []
        self._buffer_frames = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._active = False

    def feed(self, chunk: np.ndarray) -> list[AudioSegment]:
        audio = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return []
        rms = float(np.sqrt(np.mean(np.square(audio), dtype=np.float64)))
        threshold = max(self.min_rms, self._floor * self.threshold)
        voiced = rms >= threshold
        if not voiced:
            # Learn the floor only while the gate is closed, so a loud phrase
            # cannot raise the threshold above its own tail.
            self._floor = 0.98 * self._floor + 0.02 * rms
        out: list[AudioSegment] = []

        if not self._active:
            if not voiced:
                self._append_pre_roll(audio)
                return []
            self._active = True
            self._buffer = list(self._pre_roll) + [audio]
            self._buffer_frames = sum(len(part) for part in self._buffer)
            self._pre_roll.clear()
            self._speech_frames = audio.size
            self._silence_frames = 0
            return self._maybe_split()

        self._buffer.append(audio)
        self._buffer_frames += audio.size
        if voiced:
            self._speech_frames += audio.size
            self._silence_frames = 0
        else:
            self._silence_frames += audio.size

        if self._silence_frames >= self.silence_frames:
            segment = self._emit()
            if segment is not None:
                out.append(segment)
            return out

        out.extend(self._maybe_split())
        return out

    def _maybe_split(self) -> list[AudioSegment]:
        if self._buffer_frames < self.max_frames:
            return []
        segment = self._emit()
        if segment is None:
            return []
        # Keep the last fraction of a second as the next phrase's pre-roll: a
        # cut exactly on the boundary clips the first phoneme of the next word.
        if self.overlap_frames:
            tail = segment.samples[-self.overlap_frames:]
            self._append_pre_roll(tail)
        return [segment]

    def flush(self) -> AudioSegment | None:
        """Emit the phrase in flight, e.g. when the stream stops mid-sentence."""
        return self._emit()

    def reset(self) -> None:
        self._reset()
        self._pre_roll.clear()
        self._silence_frames = 0


# --------------------------------------------------------------------------- #
# recognition
# --------------------------------------------------------------------------- #


@dataclass
class Transcript:
    text: str
    language: str = ""
    confidence: float = 0.0
    elapsed_ms: float = 0.0
    backend: str = ""


#: A result Whisper invents over music or silence often looks like this. It is
#: not a complete solution to hallucination; it stops the most common line from
#: becoming a permanent subtitle.
_NON_SPEECH = re.compile(r"^[\s\[\]\(\)\{\}♪♫*\-–—.…,，。！？!?'\"]*$")

#: Whisper labels non-speech audio this way. The label is not a translation of
#: anything a viewer needs, so it is dropped rather than drawn as a subtitle.
_NOISE_TAG = re.compile(
    r"^[\[\(（【]?\s*(music|applause|laughter|silence|music playing|"
    r"掌声|音乐|笑声|静音)\s*[\]\)）】]?$",
    re.IGNORECASE,
)


def _clean_transcript(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text or _NON_SPEECH.match(text) or _NOISE_TAG.match(text):
        return ""
    return text


class FasterWhisperTranscriber:
    """faster-whisper on the CPU, loaded lazily.

    The model is a directory or a size name (tiny/base/small/...). A size name
    downloads from Hugging Face on first use into the cache; use
    fetch_model.py --asr first to keep the machine offline while watching.
    """

    def __init__(
        self,
        model: str = "small",
        *,
        compute_type: str = "int8",
        threads: int = 4,
        language: str = "auto",
        beam_size: int = 1,
        vad_filter: bool = True,
        download_root: str | None = None,
        local_files_only: bool = False,
        context_chars: int = 120,
    ) -> None:
        self.model_spec = str(model or "small")
        self.compute_type = compute_type
        self.threads = threads
        self.language = (language or "auto").strip()
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.download_root = download_root
        self.local_files_only = local_files_only
        #: How much of the previous phrase to carry into the next one. Whisper
        #: is phrase-level, so without this a name or a technical term spoken in
        #: the previous sentence is re-guessed from scratch every time. A prompt
        #: is used instead of `condition_on_previous_text` because that switch
        #: lets one hallucinated phrase poison every phrase after it.
        self.context_chars = max(0, int(context_chars))
        self._context = ""
        self._model: Any = None
        self._lock = threading.Lock()
        self.load_ms = 0.0
        self.load_error: str | None = None
        self.backend = f"faster-whisper:{Path(self.model_spec).name or self.model_spec}"

    def ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            started = time.perf_counter()
            try:
                from faster_whisper import WhisperModel  # type: ignore
            except Exception as exc:  # pragma: no cover - depends on the machine
                raise LiveUnavailable(
                    "直播模式需要 faster-whisper 才能转写语音："
                    "pip install -r prototype/requirements.txt"
                    f"（{type(exc).__name__}: {exc}）"
                ) from exc
            try:
                self._model = WhisperModel(
                    self.model_spec,
                    device="cpu",
                    compute_type=self.compute_type,
                    cpu_threads=max(1, int(self.threads)),
                    download_root=self.download_root,
                    local_files_only=self.local_files_only,
                )
            except Exception as exc:
                self.load_error = f"{type(exc).__name__}: {exc}"
                raise LiveUnavailable(
                    f"无法加载语音模型 {self.model_spec!r}：{self.load_error}。"
                    "首次使用请先运行 prototype\\run.cmd fetch_model --asr small"
                ) from exc
            self.load_ms = (time.perf_counter() - started) * 1000.0

    def context_prompt(self) -> str:
        """The tail of the previous phrase, or "" when context is disabled."""
        if self.context_chars <= 0 or not self._context:
            return ""
        return self._context[-self.context_chars:].strip()

    def transcribe(
        self, samples: np.ndarray, sample_rate: int = SAMPLE_RATE, language: str = ""
    ) -> Transcript:
        self.ensure_loaded()
        started = time.perf_counter()
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if sample_rate != SAMPLE_RATE:
            audio = resample(audio, sample_rate, SAMPLE_RATE)
        language_arg: str | None = (language or self.language or "auto").strip()
        if language_arg.lower() in ("", "auto"):
            language_arg = None
        model = self._model
        prompt = self.context_prompt()
        kwargs = dict(
            language=language_arg,
            beam_size=max(1, int(self.beam_size)),
            vad_filter=bool(self.vad_filter),
            vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            temperature=0.0,
        )
        if prompt:
            kwargs["initial_prompt"] = prompt
        try:
            segments, info = model.transcribe(audio, **kwargs)
        except TypeError:
            # An older faster-whisper without initial_prompt must still run; the
            # context is an improvement, not a requirement for live translation.
            kwargs.pop("initial_prompt", None)
            segments, info = model.transcribe(audio, **kwargs)
        pieces: list[str] = []
        logprobs: list[float] = []
        for segment in segments:
            text = _clean_transcript(getattr(segment, "text", ""))
            if text:
                pieces.append(text)
                try:
                    logprobs.append(float(getattr(segment, "avg_logprob", -0.5)))
                except Exception:
                    pass
        text = " ".join(pieces).strip()
        if not text:
            return Transcript(
                text="", language=str(getattr(info, "language", "")), backend=self.backend
            )
        # Remember this phrase for the next one. Only the tail is kept, so a
        # monologue cannot grow an unbounded prompt.
        if self.context_chars > 0:
            self._context = text[-max(80, self.context_chars):]
        confidence = 0.0
        if logprobs:
            confidence = float(np.clip(np.exp(sum(logprobs) / len(logprobs)), 0.0, 1.0))
        return Transcript(
            text=text,
            language=str(getattr(info, "language", "") or ""),
            confidence=confidence,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            backend=self.backend,
        )

    def close(self) -> None:
        self._model = None

    def describe(self) -> str:
        return (
            f"faster-whisper {self.model_spec}（{self.compute_type}, "
            f"{self.threads} 线程, 语言 {self.language}）"
        )


class ScriptedTranscriber:
    """Deterministic stand-in used by the self check and by demos without a model.

    It exists because an end-to-end test must not depend on a 75-500 MB model or
    on a microphone. It returns the next line each time a phrase arrives.
    """

    def __init__(self, lines: Sequence[str], *, delay_s: float = 0.0) -> None:
        self.lines = list(lines)
        self.delay_s = delay_s
        self.calls = 0
        self.backend = "scripted"
        self.load_ms = 0.0

    def ensure_loaded(self) -> None:
        return None

    def transcribe(
        self, samples: np.ndarray, sample_rate: int = SAMPLE_RATE, language: str = ""
    ) -> Transcript:
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        index = self.calls
        self.calls += 1
        text = self.lines[index] if index < len(self.lines) else ""
        return Transcript(
            text=text,
            language=language or "en",
            confidence=0.9,
            elapsed_ms=self.delay_s * 1000.0,
            backend=self.backend,
        )

    def close(self) -> None:
        return None

    def describe(self) -> str:
        return f"脚本转写（{len(self.lines)} 行，测试用）"


# --------------------------------------------------------------------------- #
# live pipeline
# --------------------------------------------------------------------------- #


@dataclass
class LiveSettings:
    """The live config section, parsed once."""

    source: str = "system"
    device: str = ""
    file: str = ""
    language: str = "auto"
    asr_model: str = "small"
    asr_compute_type: str = "int8"
    asr_threads: int = 4
    asr_beam_size: int = 1
    asr_vad: bool = True
    #: How many characters of the previous phrase whisper sees as context. 0
    #: disables it. A prompt keeps names/terms stable without
    #: condition_on_previous_text's hallucination chain.
    context_chars: int = 120
    silence_ms: int = 600
    min_speech_ms: int = 300
    max_segment_s: float = 8.0
    vad_threshold: float = 2.5
    min_rms: float = 0.004
    pre_roll_ms: int = 300
    capture_rate: int = 48000
    block_ms: int = BLOCK_MS
    file_realtime: bool = False
    model_dir: str = ""
    local_files_only: bool = False

    @classmethod
    def from_config(cls, config: Any) -> "LiveSettings":
        raw = getattr(config, "live", None) or {}
        defaults = cls()

        def pick(name: str, caster: Callable[[Any], Any], fallback: Any) -> Any:
            value = raw.get(name, fallback)
            if value is None or value == "":
                return caster(fallback)
            try:
                return caster(value)
            except (TypeError, ValueError):
                return caster(fallback)

        return cls(
            source=str(raw.get("source", defaults.source) or defaults.source),
            device=str(raw.get("device", defaults.device) or ""),
            file=str(raw.get("file", defaults.file) or ""),
            language=str(raw.get("language", defaults.language) or "auto"),
            asr_model=str(raw.get("asr_model", defaults.asr_model) or defaults.asr_model),
            asr_compute_type=str(
                raw.get("asr_compute_type", defaults.asr_compute_type) or defaults.asr_compute_type
            ),
            asr_threads=pick("asr_threads", int, defaults.asr_threads),
            asr_beam_size=pick("asr_beam_size", int, defaults.asr_beam_size),
            asr_vad=pick("asr_vad", bool, defaults.asr_vad),
            context_chars=pick("context_chars", int, defaults.context_chars),
            silence_ms=pick("silence_ms", int, defaults.silence_ms),
            min_speech_ms=pick("min_speech_ms", int, defaults.min_speech_ms),
            max_segment_s=pick("max_segment_s", float, defaults.max_segment_s),
            vad_threshold=pick("vad_threshold", float, defaults.vad_threshold),
            min_rms=pick("min_rms", float, defaults.min_rms),
            pre_roll_ms=pick("pre_roll_ms", int, defaults.pre_roll_ms),
            capture_rate=pick("capture_rate", int, defaults.capture_rate),
            block_ms=pick("block_ms", int, defaults.block_ms),
            file_realtime=pick("file_realtime", bool, defaults.file_realtime),
            model_dir=str(raw.get("model_dir", defaults.model_dir) or ""),
            local_files_only=pick("local_files_only", bool, defaults.local_files_only),
        )


class LivePipeline:
    """Audio -> speech -> translation, publishing the same events as OCR.

    It deliberately duck-types the parts of Pipeline that Session uses
    (start/stop/pause/resume/stats/to_dict/config) so that every existing
    surface and command keeps working. The translation half is the real
    Pipeline object, not a copy: corpus precedence, scene handling,
    target-language passthrough, deduplication, refinement and corrections all
    behave exactly as they do on a screen frame.
    """

    KIND = "live"

    def __init__(
        self,
        *,
        source: AudioSource,
        transcriber: Any,
        translator: Translator,
        settings: LiveSettings | None = None,
        config: PipelineConfig | None = None,
        on_update: Callable[[OverlayUpdate], None] | None = None,
        on_stats: Callable[[OverlayStats], None] | None = None,
    ) -> None:
        self.source = source
        self.transcriber = transcriber
        self.translator = translator
        self.settings = settings or LiveSettings()
        self.config = config or PipelineConfig()
        self.on_update = on_update
        self.on_stats = on_stats
        self._core = Pipeline(
            capturer=None,  # type: ignore[arg-type]
            ocr=None,  # type: ignore[arg-type]
            translator=translator,
            config=self.config,
        )
        self._segmenter = SpeechSegmenter(
            silence_ms=self.settings.silence_ms,
            min_speech_ms=self.settings.min_speech_ms,
            max_segment_s=self.settings.max_segment_s,
            threshold=self.settings.vad_threshold,
            min_rms=self.settings.min_rms,
            pre_roll_ms=self.settings.pre_roll_ms,
        )
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=4)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._threads: list[threading.Thread] = []
        self._errors = 0
        self._segments = 0
        self._empty = 0
        self._duplicates = 0
        self._dropped = 0
        self._audio_seconds = 0.0
        self._last_text = ""
        self._last_text_at = 0.0
        self._source_done = False
        self._finished = False
        self._started_at = 0.0
        self._lock = threading.Lock()
        self.asr_error: str | None = None
        self.load_ms = 0.0
        self.asr_model = ""

    # -- lifecycle -------------------------------------------------------- #

    def start(self, warmup: bool = True) -> None:
        if self._threads:
            return
        if warmup:
            self.transcriber.ensure_loaded()
        try:
            self.source.open()
        except Exception:
            # The model may already be loaded; release it rather than leaving a
            # failed start holding hundreds of megabytes for a process that is
            # about to print an error and exit.
            try:
                self.transcriber.close()
            except Exception:
                pass
            raise
        self._stop.clear()
        self._paused.clear()
        self._finished = False
        self._source_done = False
        self._segmenter.reset()
        self._started_at = time.perf_counter()
        self._threads = [
            threading.Thread(target=self._capture_loop, name="watashi-audio", daemon=True),
            threading.Thread(target=self._work_loop, name="watashi-asr", daemon=True),
        ]
        try:
            for thread in self._threads:
                thread.start()
        except Exception:
            # Do not leave a device open because a thread could not start.
            self.stop()
            raise

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._paused.clear()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []
        try:
            self.source.close()
        except Exception:
            pass
        try:
            self.transcriber.close()
        except Exception:
            pass

    def pause(self) -> None:
        self._paused.set()
        self._segmenter.reset()
        self._drain_queue()
        self._emit_stats()

    def resume(self) -> None:
        self._paused.clear()
        self._segmenter.reset()
        self._emit_stats()

    def toggle_pause(self) -> bool:
        if self._paused.is_set():
            self.resume()
            return False
        self.pause()
        return True

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def running(self) -> bool:
        return bool(self._threads) and not self._stop.is_set()

    @property
    def recent(self):
        """The translation helper's short-term memory.

        Session's correction path asks the pipeline to forget a corrected
        sentence so the old answer is not served again for ten seconds. Live
        shares that memory with the screen path by construction; exposing it
        keeps the correction behaviour identical instead of silently weaker.
        """
        return getattr(self._core, "recent", None)

    # -- loops ------------------------------------------------------------ #

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _enqueue(self, segment: AudioSegment) -> None:
        try:
            self._queue.put_nowait(segment)
            return
        except queue.Full:
            pass
        if self.settings.file:
            # A file is not a live device: waiting is better than dropping the
            # middle of a sentence. The ASR worker is the only consumer.
            try:
                self._queue.put(segment, timeout=0.5)
                return
            except queue.Full:
                pass
        # Live: the newest phrase matters. Dropping the oldest keeps the
        # subtitle on "now" rather than drifting behind the stream.
        try:
            self._queue.get_nowait()
            self._dropped += 1
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(segment)
        except queue.Full:
            self._dropped += 1

    def _capture_loop(self) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            try:
                chunk = self.source.read()
            except Exception as exc:
                self._errors += 1
                self.asr_error = f"audio: {exc}"
                print(f"[live] audio read failed: {exc}")
                time.sleep(0.2)
                continue
            if chunk is None:
                self._source_done = True
                try:
                    self._queue.put(None, timeout=1.0)
                except queue.Full:
                    pass
                return
            with self._lock:
                self._audio_seconds += chunk.size / float(SAMPLE_RATE)
            for segment in self._segmenter.feed(chunk):
                self._enqueue(segment)

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._source_done and self._queue.empty():
                    self._finished = True
                    self._emit_stats()
                    return
                continue
            if item is None:
                self._finished = True
                self._emit_stats()
                return
            if self._paused.is_set():
                continue
            if not item.samples.size:
                continue

            started = time.perf_counter()
            try:
                transcript = self.transcriber.transcribe(
                    item.samples, SAMPLE_RATE, self.settings.language
                )
            except Exception as exc:
                self._errors += 1
                self.asr_error = f"{type(exc).__name__}: {exc}"
                print(f"[live] transcription failed: {exc}")
                continue
            asr_ms = (time.perf_counter() - started) * 1000.0
            text = _clean_transcript(transcript.text)
            if not text:
                with self._lock:
                    self._empty += 1
                self._emit_stats()
                continue
            if self._is_duplicate(text):
                self._emit_stats()
                continue

            try:
                update = self._core.translate_texts(
                    [text], started=started, recognition_ms=asr_ms
                )
            except Exception as exc:
                self._errors += 1
                print(f"[live] translation failed: {exc}")
                continue
            if update is None:
                self._emit_stats()
                continue
            with self._lock:
                self._segments += 1
            update.latency_ms = (time.perf_counter() - started) * 1000.0
            update.trace = (
                f"ASR {transcript.backend or 'asr'} "
                f"{transcript.language or '?'} {asr_ms:.0f} ms\n{update.trace}"
            )
            if self.on_update is not None:
                self.on_update(update)
            self._emit_stats()

    def _is_duplicate(self, text: str) -> bool:
        from .recent import normalize

        key = normalize(text)
        now = time.perf_counter()
        if key and key == self._last_text and now - self._last_text_at < 2.5:
            self._duplicates += 1
            return True
        self._last_text = key
        self._last_text_at = now
        return False

    # -- stats ------------------------------------------------------------ #

    def _emit_stats(self) -> None:
        if self.on_stats is not None:
            self.on_stats(self.stats())

    def stats(self) -> OverlayStats:
        stats = self._core.stats()
        stats.mode = "live"
        stats.skipped = self._empty + self._duplicates
        # core is a translation helper, not the thing being paused. Session and
        # every surface read this flag to draw "已暂停", so it has to come from
        # the live pipeline, not from the helper it happens to own.
        stats.paused = self.paused
        return stats

    def to_dict(self) -> dict[str, Any]:
        data = self._core.to_dict()
        describer = getattr(self.transcriber, "describe", None)
        data.update(
            {
                "capture_mode": "audio",
                "paused": self.paused,
                "live_source": self.source.describe(),
                "live_asr": describer() if callable(describer) else "",
                "asr_model": self.asr_model,
                "asr_backend": getattr(self.transcriber, "backend", ""),
                "asr_load_ms": round(float(getattr(self.transcriber, "load_ms", 0.0) or 0.0), 1),
                "live_language": self.settings.language,
                "segments": self._segments,
                "empty_segments": self._empty,
                "duplicate_segments": self._duplicates,
                "dropped_segments": self._dropped,
                "audio_seconds": round(self._audio_seconds, 1),
                "live_finished": self._finished,
                "live_errors": self._errors,
                "asr_error": self.asr_error,
            }
        )
        return data


# --------------------------------------------------------------------------- #
# construction from config
# --------------------------------------------------------------------------- #


def _resolve_model(config: Any, spec: str) -> tuple[str, str | None]:
    """Return (model argument, download root) for faster-whisper.

    A path that exists is used as-is. A bare size name is left for
    faster-whisper to resolve, with the configured cache directory when one is
    set. Resolution is relative to the config file so a portable profile works.
    """
    text = (spec or "small").strip()
    path = Path(text).expanduser()
    if not path.is_absolute():
        candidate = Path(config.base_dir) / path
        if candidate.exists():
            path = candidate
    if path.exists():
        return str(path), None
    # fetch_model --asr writes to <repo>/models/whisper/<name>. Looking there
    # means the documented default (live.asr_model: small) uses the prefetched
    # copy instead of quietly going to the network for the same weights.
    if "/" not in text and "\\" not in text:
        for root in (
            Path(config.base_dir).parent / "models" / "whisper",
            Path(config.base_dir) / "models" / "whisper",
        ):
            candidate = root / text
            if candidate.is_dir():
                return str(candidate), None
    model_dir = str((getattr(config, "live", {}) or {}).get("model_dir") or "").strip()
    download_root = None
    if model_dir:
        base = Path(model_dir)
        download_root = str(base if base.is_absolute() else Path(config.base_dir) / base)
    return text, download_root


def build_audio_source(config: Any, settings: LiveSettings | None = None) -> AudioSource:
    settings = settings or LiveSettings.from_config(config)
    if settings.source == "file" or settings.file:
        if not settings.file:
            raise LiveUnavailable("live.source 是 file，但没有设置 live.file")
        path = Path(settings.file)
        return WavFileAudioSource(
            path if path.is_absolute() else Path(config.base_dir) / path,
            realtime=settings.file_realtime,
            block_ms=settings.block_ms,
        )
    return SystemAudioSource(
        device=settings.device,
        loopback=settings.source != "microphone",
        capture_rate=settings.capture_rate,
        block_ms=settings.block_ms,
    )


def build_transcriber(config: Any, settings: LiveSettings | None = None) -> Any:
    settings = settings or LiveSettings.from_config(config)
    model, download_root = _resolve_model(config, settings.asr_model)
    return FasterWhisperTranscriber(
        model,
        compute_type=settings.asr_compute_type,
        threads=settings.asr_threads,
        language=settings.language,
        beam_size=settings.asr_beam_size,
        vad_filter=settings.asr_vad,
        download_root=download_root,
        local_files_only=settings.local_files_only,
        context_chars=settings.context_chars,
    )


def build_live_pipeline(
    config: Any,
    translator: Translator,
    *,
    on_update: Callable[[OverlayUpdate], None] | None = None,
    on_stats: Callable[[OverlayStats], None] | None = None,
) -> LivePipeline:
    """Build the whole live path from config.live and the shared translator."""
    settings = LiveSettings.from_config(config)
    pipeline_config = PipelineConfig(
        target_lang=str(config.translation.get("target", "zh-CN")),
        source_lang=str(config.translation.get("source", "auto")),
        min_confidence=float(config.translation.get("min_confidence", 0.0)),
        scene=str(config.translation.get("scene") or "").strip(),
        max_boxes=int(config.ocr.get("max_boxes", 0) or 0),
        final_translation=bool(config.translation.get("final_translation", True)),
        join_fragmented_lines=bool(
            config.translation.get("join_fragmented_lines", True)
        ),
        join_max_lines=int(config.translation.get("join_max_lines", 3) or 3),
        join_max_chars=int(config.translation.get("join_max_chars", 240) or 240),
    )
    pipeline = LivePipeline(
        source=build_audio_source(config, settings),
        transcriber=build_transcriber(config, settings),
        translator=translator,
        settings=settings,
        config=pipeline_config,
        on_update=on_update,
        on_stats=on_stats,
    )
    pipeline.asr_model = settings.asr_model
    return pipeline
