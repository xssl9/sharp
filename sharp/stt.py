"""Голосовой ввод: запись с микрофона до тишины и распознавание.

Пишем через sounddevice (pipewire), детектим конец фразы по энергии (VAD),
распознаём через Google Web Speech (онлайн, как в старом voice.py). Оффлайн-режим
(faster-whisper) можно добавить позже.
"""
from __future__ import annotations

import threading

import numpy as np

from .config import CFG

SAMPLE_RATE = 16000
FRAME = 1024
SILENCE_RMS = 0.012      # порог тишины (энергия)
MAX_SECONDS = 12
SILENCE_TAIL = 0.7       # сек тишины = конец фразы
START_TIMEOUT = 5.0      # сколько ждём начала речи


def record_until_silence(cancel_event: threading.Event | None = None) -> bytes:
    """Записать фразу до паузы, вернуть PCM s16le 16кГц mono."""
    import sounddevice as sd

    collected: list[np.ndarray] = []
    silence_frames = 0
    speech_started = False
    elapsed = 0.0
    tail_frames = int(SILENCE_TAIL * SAMPLE_RATE / FRAME)
    start_frames = int(START_TIMEOUT * SAMPLE_RATE / FRAME)
    idle_frames = 0

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=FRAME) as stream:
        while elapsed < MAX_SECONDS:
            if cancel_event is not None and cancel_event.is_set():
                return b""
            block, _ = stream.read(FRAME)
            if cancel_event is not None and cancel_event.is_set():
                return b""
            mono = block[:, 0]
            rms = float(np.sqrt(np.mean(mono ** 2)))
            elapsed += FRAME / SAMPLE_RATE

            if rms >= SILENCE_RMS:
                speech_started = True
                silence_frames = 0
                collected.append(mono.copy())
            elif speech_started:
                silence_frames += 1
                collected.append(mono.copy())
                if silence_frames >= tail_frames:
                    break
            else:
                idle_frames += 1
                if idle_frames >= start_frames:
                    break  # так и не начали говорить

    if not collected:
        return b""
    audio = np.concatenate(collected)
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()


def listen_once(cancel_event: threading.Event | None = None) -> str:
    """Записать фразу и распознать в текст. Пустая строка — если ничего не распознано."""
    import speech_recognition as sr

    pcm = record_until_silence(cancel_event=cancel_event)
    if not pcm:
        return ""
    audio_data = sr.AudioData(pcm, SAMPLE_RATE, 2)
    recognizer = sr.Recognizer()
    try:
        return recognizer.recognize_google(audio_data, language=CFG.stt_lang)
    except sr.UnknownValueError:
        return ""
    except sr.RequestError:
        return ""


if __name__ == "__main__":
    print("Говорите…")
    print("Распознано:", listen_once() or "(тишина)")
