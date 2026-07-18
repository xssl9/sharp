"""Конфигурация Sharp: читаем из ~/.config/sharp/config.toml и переменных окружения.

Приоритет: переменные окружения > config.toml > значения по умолчанию.
Ключ Gemini берётся из GEMINI_API_KEY / GOOGLE_API_KEY или поля api_key в toml.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .storage import atomic_write_text

CONFIG_DIR = Path.home() / ".config" / "sharp"
CONFIG_PATH = CONFIG_DIR / "config.toml"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.toml"
HISTORY_PATH = CONFIG_DIR / "history.json"

# 30 голосов Gemini TTS (для валидации /voice и подсказок)
VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]


@dataclass
class Config:
    api_key: str = ""
    chat_model: str = "gemini-3-flash-preview"
    tts_model: str = "gemini-2.5-flash-preview-tts"
    voice: str = "Charon"
    voice_style: str = "Скажи спокойно и уверенно, как вежливый дворецкий:"
    stt_lang: str = "ru-RU"
    mic_sensitivity: float = 1.0
    live_mode: bool = True
    live_model: str = "gemini-2.5-flash-native-audio-latest"
    allow_shell_commands: bool = False

    def require_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "Не задан ключ Gemini. Укажи api_key в ~/.config/sharp/config.toml "
                "или экспортируй GEMINI_API_KEY."
            )
        return self.api_key


def load_config() -> Config:
    data: dict = {}
    credentials: dict = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    if CREDENTIALS_PATH.exists():
        with CREDENTIALS_PATH.open("rb") as f:
            credentials = tomllib.load(f)

    cfg = Config(
        api_key=credentials.get("api_key", data.get("api_key", "")),
        chat_model=data.get("chat_model", Config.chat_model),
        tts_model=data.get("tts_model", Config.tts_model),
        voice=data.get("voice", Config.voice),
        voice_style=data.get("voice_style", Config.voice_style),
        stt_lang=data.get("stt_lang", Config.stt_lang),
        mic_sensitivity=float(data.get("mic_sensitivity", Config.mic_sensitivity)),
        live_mode=data.get("live_mode", Config.live_mode),
        live_model=data.get("live_model", Config.live_model),
        allow_shell_commands=data.get("allow_shell_commands", Config.allow_shell_commands),
    )

    # env перекрывает toml
    env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if env_key:
        cfg.api_key = env_key

    return cfg


# Единый инстанс на процесс
CFG = load_config()


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def save_config() -> None:
    """Записать несекретные настройки CFG обратно в config.toml.

    Старый ключ из config.toml при первой записи переносится в credentials.toml.
    Ключ из окружения никогда не копируется на диск.
    """
    existing: dict = {}
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("rb") as stream:
                existing = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError):
            existing = {}
    legacy_key = existing.get("api_key", "")
    if legacy_key and not CREDENTIALS_PATH.exists():
        atomic_write_text(
            CREDENTIALS_PATH,
            "# Секрет Sharp. Не публикуйте этот файл.\n"
            f'api_key = "{_toml_escape(legacy_key)}"\n',
        )
    lines = [
        "# Настройки Sharp. API-ключ задавайте через GEMINI_API_KEY.",
        "# Модель для текста/команд",
        f'chat_model = "{_toml_escape(CFG.chat_model)}"',
        "# Модель для голоса (нативный Gemini TTS)",
        f'tts_model = "{_toml_escape(CFG.tts_model)}"',
        "",
        "# Голос (30 вариантов: Charon, Kore, Puck, Zephyr, Fenrir, Aoede, ...)",
        f'voice = "{_toml_escape(CFG.voice)}"',
        "# Стиль-префикс, добавляется к тексту перед озвучкой",
        f'voice_style = "{_toml_escape(CFG.voice_style)}"',
        "",
        "# Язык распознавания речи",
        f'stt_lang = "{_toml_escape(CFG.stt_lang)}"',
        "# Чувствительность микрофона: 0.25–1.25 (выше = чувствительнее)",
        f"mic_sensitivity = {max(0.25, min(1.25, CFG.mic_sensitivity)):.2f}",
        "",
        "# Реалтайм голос↔голос (Gemini Live API). false = классический STT→chat→TTS",
        f"live_mode = {'true' if CFG.live_mode else 'false'}",
        f'live_model = "{_toml_escape(CFG.live_model)}"',
        "",
        "# Разрешить модели произвольные команды Linux. По умолчанию отключено.",
        f"allow_shell_commands = {'true' if CFG.allow_shell_commands else 'false'}",
        "",
    ]
    atomic_write_text(CONFIG_PATH, "\n".join(lines))
