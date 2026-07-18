"""Голосовой ввод: запись с микрофона до тишины и распознавание.

Пишем через sounddevice (pipewire), детектим конец фразы по энергии (VAD),
распознаём через Google Web Speech (онлайн, как в старом voice.py). Оффлайн-режим
(faster-whisper) можно добавить позже.
"""
from __future__ import annotations

import threading
from collections import deque

import numpy as np

from .config import CFG

SAMPLE_RATE = 16000
FRAME = 480              # 30 мс — формат, поддерживаемый WebRTC VAD
MIN_START_RMS = 0.006
MAX_SPEECH_SECONDS = 6
MAX_WAKE_SECONDS = 1.6  # короткие циклы быстрее находят отдельное «Шарп»
SILENCE_TAIL = 0.48      # возврат к фону = конец фразы
START_TIMEOUT = 30.0     # долго держим поток открытым, чтобы редко калиброваться
PRE_ROLL_SECONDS = 0.50  # не режем тихое начало слова
CALIBRATION_SECONDS = 0.45
TRIGGER_FRAMES = 1       # высокая чувствительность важнее ложного захвата шума
_noise_rms_estimate: float | None = None


def record_until_silence(
    cancel_event: threading.Event | None = None,
    max_speech_seconds: float = MAX_SPEECH_SECONDS,
) -> bytes:
    """Записать фразу до паузы, вернуть PCM s16le 16кГц mono."""
    global _noise_rms_estimate
    import sounddevice as sd
    import webrtcvad

    collected: list[bytes] = []
    pre_roll: deque[bytes] = deque(
        maxlen=max(2, int(PRE_ROLL_SECONDS * SAMPLE_RATE / FRAME))
    )
    recent_voice: deque[bool] = deque(maxlen=TRIGGER_FRAMES)
    quiet_frames = 0
    speech_started = False
    speech_elapsed = 0.0
    tail_frames = int(SILENCE_TAIL * SAMPLE_RATE / FRAME)
    start_frames = int(START_TIMEOUT * SAMPLE_RATE / FRAME)
    calibration_frames = (
        max(3, int(CALIBRATION_SECONDS * SAMPLE_RATE / FRAME))
        if _noise_rms_estimate is None else 0
    )
    idle_frames = 0
    calibration: list[float] = []
    noise_rms = _noise_rms_estimate or MIN_START_RMS / 2
    vad = webrtcvad.Vad(2)

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=FRAME) as stream:
        while not speech_started or speech_elapsed < max_speech_seconds:
            if cancel_event is not None and cancel_event.is_set():
                return b""
            block, _ = stream.read(FRAME)
            if cancel_event is not None and cancel_event.is_set():
                return b""
            mono = block[:, 0]
            rms = float(np.sqrt(np.mean(mono ** 2)))
            pcm = (np.clip(mono, -1, 1) * 32767).astype(np.int16).tobytes()
            if speech_started:
                speech_elapsed += FRAME / SAMPLE_RATE

            # Первая секунда нужна и для пробуждения PipeWire-устройства: на
            # встроенном микрофоне первые кадры почти нулевые, а затем фон резко
            # вырастает. Медиана второй половины даёт честную базовую громкость.
            if len(calibration) < calibration_frames:
                calibration.append(rms)
                pre_roll.append(pcm)
                if len(calibration) == calibration_frames:
                    stable = calibration[len(calibration) // 2 :]
                    noise_rms = float(np.median(stable))
                continue

            sensitivity = max(0.25, min(1.25, CFG.mic_sensitivity))
            start_ratio = max(1.01, 1.50 - 0.40 * sensitivity)
            return_ratio = max(1.01, 1.25 - 0.15 * sensitivity)
            start_threshold = max(MIN_START_RMS, min(0.45, noise_rms * start_ratio + 0.001))
            return_threshold = max(MIN_START_RMS * 0.8, noise_rms * return_ratio)
            is_voice = vad.is_speech(pcm, SAMPLE_RATE) and rms >= start_threshold

            if not speech_started:
                pre_roll.append(pcm)
                recent_voice.append(is_voice)
                if len(recent_voice) == TRIGGER_FRAMES and all(recent_voice):
                    speech_started = True
                    quiet_frames = 0
                    collected.extend(pre_roll)
                    pre_roll.clear()
                else:
                    # Подстраиваемся под музыку/вентилятор, не догоняя резкий
                    # близкий голос пользователя.
                    if rms < start_threshold:
                        noise_rms = noise_rms * 0.985 + rms * 0.015
                    idle_frames += 1
                    if idle_frames >= start_frames:
                        break
            else:
                collected.append(pcm)
                if rms < return_threshold or not vad.is_speech(pcm, SAMPLE_RATE):
                    quiet_frames += 1
                else:
                    quiet_frames = 0
                if quiet_frames >= tail_frames:
                    break

    _noise_rms_estimate = noise_rms
    if not collected:
        return b""
    return b"".join(collected)


def _transcripts_from_google(result: object) -> list[str]:
    """Extract unique transcripts from SpeechRecognition's ``show_all`` result."""
    if isinstance(result, str):
        return [result.strip()] if result.strip() else []
    if not isinstance(result, dict):
        return []
    transcripts: list[str] = []
    for alternative in result.get("alternative", []):
        if not isinstance(alternative, dict):
            continue
        text = str(alternative.get("transcript", "")).strip()
        if text and text not in transcripts:
            transcripts.append(text)
    return transcripts


def listen_candidates(
    cancel_event: threading.Event | None = None,
    max_speech_seconds: float = MAX_SPEECH_SECONDS,
) -> list[str]:
    """Record one phrase and return recognition alternatives, best first."""
    import speech_recognition as sr

    pcm = record_until_silence(
        cancel_event=cancel_event,
        max_speech_seconds=max_speech_seconds,
    )
    if not pcm:
        return []
    audio_data = sr.AudioData(pcm, SAMPLE_RATE, 2)
    recognizer = sr.Recognizer()
    try:
        result = recognizer.recognize_google(
            audio_data,
            language=CFG.stt_lang,
            show_all=True,
        )
        return _transcripts_from_google(result)
    except sr.UnknownValueError:
        return []
    except sr.RequestError:
        return []


def listen_once(cancel_event: threading.Event | None = None) -> str:
    """Compatibility helper returning only the most likely transcript."""
    candidates = listen_candidates(cancel_event=cancel_event)
    return candidates[0] if candidates else ""


if __name__ == "__main__":
    print("Говорите…")
    print("Распознано:", listen_once() or "(тишина)")
