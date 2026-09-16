"""Voice I/O: text-to-speech, microphone recording and speech-to-text.

Everything is behind small protocols so the conversational flow in
``resolve.py`` can be tested with fakes. Real backends are optional
dependencies (``pip install -e ".[voice]"``):

  TTS  - pyttsx3 (cross-platform, offline) or macOS ``say``; falls back to
         printing the question.
  Mic  - sounddevice with RMS-based silence detection.
  STT  - faster-whisper (local, offline after the first model download).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("autoapply.voice")


@dataclass
class Audio:
    samples: Any  # numpy float32 array, mono
    sample_rate: int

    @property
    def seconds(self) -> float:
        try:
            return float(len(self.samples)) / float(self.sample_rate)
        except Exception:  # noqa: BLE001
            return 0.0


class Speaker(Protocol):
    def say(self, text: str) -> None: ...


class Recorder(Protocol):
    def record(self, max_seconds: float, silence_seconds: float) -> Audio | None: ...


class Transcriber(Protocol):
    def transcribe(self, audio: Audio) -> str: ...


class VoiceError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Speakers
# --------------------------------------------------------------------------- #


class PrintSpeaker:
    """Fallback when no TTS engine is available: the question is printed only."""

    def say(self, text: str) -> None:  # noqa: D401
        return None


class SayCommandSpeaker:
    """macOS built-in `say`."""

    def say(self, text: str) -> None:
        try:
            subprocess.run(["say", text], check=False, timeout=60)
        except Exception as e:  # noqa: BLE001
            log.warning("say failed: %s", e)


class Pyttsx3Speaker:
    def __init__(self) -> None:
        import pyttsx3

        self._engine = pyttsx3.init()

    def say(self, text: str) -> None:
        try:
            self._engine.say(text)
            self._engine.runAndWait()
        except Exception as e:  # noqa: BLE001
            log.warning("pyttsx3 failed: %s", e)


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #


class SoundDeviceRecorder:
    """Record from the default microphone until silence, timeout, or Enter."""

    def __init__(self, sample_rate: int = 16000, energy_threshold: float = 0.012, min_seconds: float = 1.0):
        import numpy as np  # noqa: F401 - checked at construction
        import sounddevice as sd

        self.sd = sd
        self.sample_rate = sample_rate
        self.energy_threshold = energy_threshold
        self.min_seconds = min_seconds
        self.stop_flag = threading.Event()

    def stop(self) -> None:
        self.stop_flag.set()

    def record(self, max_seconds: float, silence_seconds: float) -> Audio | None:
        import numpy as np

        chunks: list[Any] = []
        self.stop_flag.clear()
        state = {"last_voice": None, "started": time.time(), "heard": False}

        def callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                log.debug("audio status: %s", status)
            mono = indata[:, 0].copy()
            chunks.append(mono)
            rms = float(np.sqrt(np.mean(mono**2))) if len(mono) else 0.0
            now = time.time()
            if rms > self.energy_threshold:
                state["last_voice"] = now
                state["heard"] = True

        try:
            with self.sd.InputStream(samplerate=self.sample_rate, channels=1, dtype="float32", blocksize=int(self.sample_rate * 0.1), callback=callback):
                while True:
                    time.sleep(0.05)
                    now = time.time()
                    elapsed = now - state["started"]
                    if self.stop_flag.is_set():
                        break
                    if elapsed >= max_seconds:
                        break
                    if state["heard"] and elapsed >= self.min_seconds and state["last_voice"] and now - state["last_voice"] >= silence_seconds:
                        break
                    if not state["heard"] and elapsed >= max(6.0, silence_seconds * 3):
                        # Nothing said at all.
                        return None
        except Exception as e:  # noqa: BLE001
            raise VoiceError(f"microphone error: {e}") from e
        if not chunks or not state["heard"]:
            return None
        return Audio(np.concatenate(chunks), self.sample_rate)


# --------------------------------------------------------------------------- #
# Transcriber
# --------------------------------------------------------------------------- #


class FasterWhisperTranscriber:
    def __init__(self, model_size: str = "base.en", language: str = "en"):
        from faster_whisper import WhisperModel

        self.language = language
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8")

    def transcribe(self, audio: Audio) -> str:
        segments, _info = self.model.transcribe(audio.samples, language=self.language or None, beam_size=5, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #


class VoiceIO:
    def __init__(self, speaker: Speaker, recorder: Recorder | None, transcriber: Transcriber | None,
                 max_record_seconds: float = 90.0, silence_seconds: float = 2.5):
        self.speaker = speaker
        self.recorder = recorder
        self.transcriber = transcriber
        self.max_record_seconds = max_record_seconds
        self.silence_seconds = silence_seconds

    @property
    def can_listen(self) -> bool:
        return self.recorder is not None and self.transcriber is not None

    def say(self, text: str) -> None:
        self.speaker.say(text)

    def listen(self, on_start=None) -> str | None:
        """Record one utterance and return its transcription (None on silence/timeout)."""
        if not self.can_listen:
            return None
        assert self.recorder and self.transcriber
        if on_start:
            on_start()
        audio = self.recorder.record(self.max_record_seconds, self.silence_seconds)
        if audio is None or audio.seconds < 0.3:
            return None
        return self.transcriber.transcribe(audio).strip() or None

    def stop_listening(self) -> None:
        stop = getattr(self.recorder, "stop", None)
        if stop:
            stop()


def build_voice(settings: dict[str, Any], console=None) -> VoiceIO:
    """Construct a VoiceIO from settings; degrades gracefully when deps are missing."""
    warn = (lambda m: console.print(f"[yellow]{m}[/]")) if console else log.warning
    tts = str(settings.get("tts", "auto")).lower()
    speaker: Speaker = PrintSpeaker()
    if tts in ("auto", "say") and sys.platform == "darwin" and shutil.which("say"):
        speaker = SayCommandSpeaker()
    elif tts in ("auto", "pyttsx3"):
        try:
            speaker = Pyttsx3Speaker()
        except Exception as e:  # noqa: BLE001
            warn(f"Text-to-speech unavailable ({e}); questions will be shown in the terminal only.")
    recorder: Recorder | None = None
    transcriber: Transcriber | None = None
    stt = str(settings.get("stt", "faster_whisper")).lower()
    if stt == "faster_whisper":
        try:
            recorder = SoundDeviceRecorder(sample_rate=int(settings.get("sample_rate", 16000)))
        except Exception as e:  # noqa: BLE001
            warn(f"Microphone unavailable ({e}). Install voice extras: pip install -e \".[voice]\". You can still type answers.")
        try:
            transcriber = FasterWhisperTranscriber(str(settings.get("whisper_model", "base.en")), str(settings.get("language", "en")))
        except Exception as e:  # noqa: BLE001
            warn(f"Speech-to-text unavailable ({e}). You can still type answers.")
            recorder = None
    return VoiceIO(speaker, recorder, transcriber,
                   max_record_seconds=float(settings.get("max_record_seconds", 90)),
                   silence_seconds=float(settings.get("silence_seconds", 2.5)))
