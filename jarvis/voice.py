"""
Voice for Jarvis: a British male speaking voice and speech recognition.

Speaking uses Microsoft's neural British voices through `edge-tts` (needs
internet), with the computer's own voices via `pyttsx3` as an offline fallback.
Listening records from the microphone with `sounddevice` and transcribes with
Google's speech service through `SpeechRecognition` (needs internet).
"""

from __future__ import annotations

import asyncio
import queue
import re
import sys
import threading
import time
from collections import deque
from typing import Callable

BRITISH_VOICES = {
    "Ryan (British male)": "en-GB-RyanNeural",
    "Thomas (British male)": "en-GB-ThomasNeural",
}
DEFAULT_VOICE = "en-GB-RyanNeural"


class VoiceError(Exception):
    pass


def _clean_for_speech(text: str) -> str:
    text = re.sub(r"[`*_#>|]", "", text)
    return re.sub(r"\s+", " ", text).strip()


class Speaker:
    """Speaks text aloud. `on_level(0..1)` reports loudness while speaking."""

    def __init__(self, voice: str = DEFAULT_VOICE, rate: str = "+4%", pitch: str = "-3Hz"):
        self.voice, self.rate, self.pitch = voice, rate, pitch
        self.on_level: Callable[[float], None] = lambda v: None
        self.on_state: Callable[[bool], None] = lambda speaking: None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple] = {}
        self._offline = None
        self._neural_retry_at = 0.0  # after a failure, use the offline voice for a while

    # ---- public ---------------------------------------------------------- #

    def say(self, text: str) -> None:
        text = _clean_for_speech(text)
        if not text:
            return
        with self._lock:
            self._stop.clear()
            self.on_state(True)
            try:
                if time.time() >= self._neural_retry_at:
                    try:
                        self._say_neural(text)
                        return
                    except Exception as e:  # no internet, missing package, no audio device…
                        print(f"[voice] neural voice unavailable ({e}); using the offline voice", file=sys.stderr)
                        self._neural_retry_at = time.time() + 60
                self._say_offline(text)
            finally:
                self.on_level(0.0)
                self.on_state(False)

    def stop(self) -> None:
        self._stop.set()
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:
            pass

    # ---- neural (edge-tts) ---------------------------------------------- #

    def _synthesise(self, text: str):
        key = (text, self.voice, self.rate, self.pitch)
        if key in self._cache:
            return self._cache[key]
        import edge_tts
        import miniaudio
        import numpy as np

        async def fetch() -> bytes:
            comm = edge_tts.Communicate(text, self.voice, rate=self.rate, pitch=self.pitch)
            buf = bytearray()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf += chunk["data"]
            return bytes(buf)

        mp3 = asyncio.run(fetch())
        if not mp3:
            raise VoiceError("no audio returned")
        decoded = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1,
                                   sample_rate=24000)
        samples = np.frombuffer(decoded.samples.tobytes(), dtype=np.int16)
        win = 480  # 20 ms windows for the loudness envelope
        n = len(samples) // win
        env = np.sqrt(np.mean((samples[: n * win].reshape(n, win).astype(np.float32) / 32768) ** 2, axis=1))
        env = np.clip(env / (env.max() or 1), 0, 1)
        result = (samples, 24000, env)
        if len(text) < 120:
            self._cache[key] = result
        return result

    def _say_neural(self, text: str) -> None:
        import sounddevice as sd

        samples, rate, env = self._synthesise(text)
        sd.play(samples, rate)
        start, duration = time.time(), len(samples) / rate
        while (elapsed := time.time() - start) < duration + 0.05:
            if self._stop.is_set():
                sd.stop()
                return
            i = int(elapsed / 0.02)
            self.on_level(float(env[i]) if i < len(env) else 0.0)
            time.sleep(0.03)
        sd.wait()

    # ---- offline fallback (pyttsx3) ------------------------------------- #

    def _say_offline(self, text: str) -> None:
        try:
            import pyttsx3

            engine = pyttsx3.init()
        except Exception:  # no speech engine at all: stay silent, the text is still on screen
            return
        engine.setProperty("voice", self._british_offline_voice(engine))
        engine.setProperty("rate", 175)
        stop_pulse = threading.Event()

        def pulse():  # no level data offline; animate gently instead
            t = 0.0
            while not stop_pulse.is_set():
                self.on_level(0.35 + 0.3 * abs(((t * 3.1) % 2) - 1))
                t += 0.05
                time.sleep(0.05)

        threading.Thread(target=pulse, daemon=True).start()
        try:
            engine.say(text)
            engine.runAndWait()
        finally:
            stop_pulse.set()

    @staticmethod
    def _british_offline_voice(engine) -> str:
        """Prefer a British male system voice (Windows 'George', macOS 'Daniel', espeak en-gb)."""
        voices = engine.getProperty("voices")

        def score(v) -> int:
            blob = f"{v.id} {v.name} {getattr(v, 'languages', '')}".lower()
            s = 0
            if any(k in blob for k in ("en-gb", "en_gb", "english (great britain)", "united kingdom", "british")):
                s += 4
            if any(k in blob for k in ("george", "daniel", "ryan", "thomas", "arthur", "oliver", "male")):
                s += 3
            if any(k in blob for k in ("hazel", "susan", "kate", "serena", "female", "zira")):
                s -= 3
            if "english" in blob or "en-" in blob or "en_" in blob:
                s += 1
            return s

        return max(voices, key=score).id if voices else engine.getProperty("voice")


class Listener:
    """Records one phrase from the microphone and returns the text."""

    RATE = 16000
    BLOCK = 480  # 30 ms

    def __init__(self, language: str = "en-GB"):
        self.language = language
        self.on_level: Callable[[float], None] = lambda v: None
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def listen(self, wait: float = 6.0, phrase_limit: float = 12.0) -> str | None:
        """Wait up to `wait` seconds for speech. Returns text, '' if not understood, None if silence."""
        try:
            import numpy as np
            import sounddevice as sd
            import speech_recognition as sr
        except ImportError as e:
            raise VoiceError(f"voice packages missing ({e.name}); run: pip install -r requirements.txt")
        except OSError as e:  # sounddevice without the PortAudio library
            raise VoiceError(f"audio system unavailable ({e})")

        self._cancel.clear()
        q: queue.Queue = queue.Queue()

        def callback(indata, frames, t, status):
            q.put(indata.copy())

        def rms(block) -> float:
            return float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))

        try:
            stream = sd.InputStream(samplerate=self.RATE, channels=1, dtype="int16",
                                    blocksize=self.BLOCK, callback=callback)
        except Exception as e:
            raise VoiceError(f"I can't access a microphone ({e})")

        frames: list = []
        with stream:
            noise = [rms(q.get(timeout=3)) for _ in range(8)]
            threshold = max(260.0, float(np.median(noise)) * 2.8)
            pre: deque = deque(maxlen=12)
            started, silent, t0 = False, 0, time.time()
            while True:
                if self._cancel.is_set():
                    return None
                block = q.get(timeout=3)
                level = rms(block)
                self.on_level(min(1.0, level / (threshold * 6)))
                if not started:
                    pre.append(block)
                    if level > threshold:
                        started, began = True, time.time()
                        frames.extend(pre)
                    elif time.time() - t0 > wait:
                        self.on_level(0.0)
                        return None
                else:
                    frames.append(block)
                    silent = silent + 1 if level < threshold * 0.8 else 0
                    if silent * 0.03 > 0.9 or time.time() - began > phrase_limit:
                        break
        self.on_level(0.0)
        audio = sr.AudioData(np.concatenate(frames).tobytes(), self.RATE, 2)
        try:
            return sr.Recognizer().recognize_google(audio, language=self.language)
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as e:
            raise VoiceError(f"the speech service is unreachable ({e}); check your internet connection")
