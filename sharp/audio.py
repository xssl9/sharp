"""Воспроизведение PCM-аудио от Gemini + выдача уровней частотных полос.

Проигрываем через sounddevice.OutputStream и по ходу считаем спектр (FFT) блоками,
раскладывая на N полос. Полосы кладём в потокобезопасную очередь — её читает
cava-подобный визуализатор в TUI, поэтому бары двигаются под реальный голос ответа.

Если sounddevice/устройство недоступны — откат на ffplay через stdin (без визуализации).
"""
from __future__ import annotations

import queue
import subprocess
import threading

import numpy as np

from .gemini import TTS_SAMPLE_RATE

# Число полос визуализатора и глобальная очередь уровней (0..1)
NUM_BANDS = 24
levels_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=64)

# Флаг «идёт воспроизведение» — визуализатор гасит бары в простое
_playing = threading.Event()


def is_playing() -> bool:
    return _playing.is_set()


def _bands_from_block(block: np.ndarray) -> np.ndarray:
    """Из блока PCM (float32 -1..1) → NUM_BANDS уровней 0..1 по лог-шкале частот."""
    if block.size == 0:
        return np.zeros(NUM_BANDS, dtype=np.float32)
    windowed = block * np.hanning(block.size)
    spectrum = np.abs(np.fft.rfft(windowed))
    if spectrum.size < NUM_BANDS:
        spectrum = np.pad(spectrum, (0, NUM_BANDS - spectrum.size))
    # логарифмические границы полос — низкие частоты детальнее
    edges = np.logspace(0, np.log10(spectrum.size), NUM_BANDS + 1).astype(int)
    edges = np.clip(edges, 0, spectrum.size)
    bands = np.empty(NUM_BANDS, dtype=np.float32)
    for i in range(NUM_BANDS):
        lo, hi = edges[i], max(edges[i] + 1, edges[i + 1])
        bands[i] = spectrum[lo:hi].mean()
    # сжатие динамики + нормировка
    bands = np.log1p(bands)
    peak = bands.max()
    if peak > 0:
        bands = bands / peak
    return bands.astype(np.float32)


def _push_levels(bands: np.ndarray) -> None:
    try:
        levels_queue.put_nowait(bands)
    except queue.Full:
        pass


def play_pcm_blocking(pcm: bytes) -> None:
    """Проиграть PCM (s16le, 24кГц, mono) до конца, попутно наполняя levels_queue."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    _playing.set()
    try:
        _play_sounddevice(samples)
    except Exception:
        _play_ffplay(pcm)
    finally:
        _playing.clear()
        _push_levels(np.zeros(NUM_BANDS, dtype=np.float32))


def _play_sounddevice(samples: np.ndarray) -> None:
    import sounddevice as sd

    blocksize = 1024
    idx = 0
    total = samples.size
    done = threading.Event()

    def callback(outdata, frames, time, status):  # noqa: ANN001, ARG001
        nonlocal idx
        chunk = samples[idx : idx + frames]
        n = chunk.size
        outdata[:n, 0] = chunk
        if n < frames:
            outdata[n:, 0] = 0.0
            raise sd.CallbackStop()
        _push_levels(_bands_from_block(chunk))
        idx += frames

    stream = sd.OutputStream(
        samplerate=TTS_SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=blocksize,
        callback=callback,
        finished_callback=done.set,
    )
    with stream:
        done.wait()


def _play_ffplay(pcm: bytes) -> None:
    """Откат: проигрываем сырой PCM через ffplay (без спектра для визуализатора)."""
    proc = subprocess.Popen(
        [
            "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
            "-f", "s16le", "-ar", str(TTS_SAMPLE_RATE), "-ch_layout", "mono", "-i", "pipe:0",
        ],
        stdin=subprocess.PIPE,
    )
    proc.communicate(pcm)
