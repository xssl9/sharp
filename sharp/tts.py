"""TTS-обёртка: текст → PCM через Gemini, сохранение WAV, CLI-проверка.

Запуск изолированной проверки «звук сразу по ключу Gemini»:
    python -m sharp.tts "Привет, сэр. Я Шарп."
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

from . import gemini


def pcm_to_wav(pcm: bytes, path: str | Path) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(gemini.TTS_CHANNELS)
        wf.setsampwidth(gemini.TTS_SAMPLE_WIDTH)
        wf.setframerate(gemini.TTS_SAMPLE_RATE)
        wf.writeframes(pcm)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    text = " ".join(argv) or "Привет, сэр. Я Шарп, ваш ассистент."

    print(f"[tts] синтез: {text!r}")
    pcm = gemini.synth(text)
    print(f"[tts] получено {len(pcm)} байт PCM ({gemini.TTS_SAMPLE_RATE} Гц)")

    out = Path("out.wav")
    pcm_to_wav(pcm, out)
    print(f"[tts] сохранено в {out.resolve()}")

    # Пробуем сразу проиграть через audio.py (если аудио-стек доступен)
    try:
        from . import audio
        audio.play_pcm_blocking(pcm)
        print("[tts] воспроизведено")
    except Exception as e:  # noqa: BLE001
        print(f"[tts] воспроизвести не удалось ({e}); открой out.wav вручную")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
