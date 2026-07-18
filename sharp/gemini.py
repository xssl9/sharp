"""Клиент Gemini: текст/команды (chat) и голос (tts) по одному API-ключу.

Оба вызова используют один и тот же genai.Client — то есть по твоему ключу Gemini
работают и ответы, и озвучка, без отдельного TTS-сервиса.
"""
from __future__ import annotations

from google import genai
from google.genai import types

from .config import CFG

# Формат PCM, который отдаёт Gemini TTS
TTS_SAMPLE_RATE = 24000
TTS_CHANNELS = 1
TTS_SAMPLE_WIDTH = 2  # s16le

_client: genai.Client | None = None

# Запасные chat-модели: если основная вернёт 503/429/404 — пробуем по очереди.
CHAT_FALLBACKS = [
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=CFG.require_key())
    return _client


def chat(history: list[types.Content], system_prompt: str) -> str:
    """Отправить историю диалога, получить текстовый ответ (может содержать команды).

    Перебираем модели: основную из конфига, затем запасные — на случай 503/429/404.
    """
    models: list[str] = [CFG.chat_model] + [m for m in CHAT_FALLBACKS if m != CFG.chat_model]
    cfg = types.GenerateContentConfig(
        system_instruction=system_prompt,
        max_output_tokens=400,
        temperature=0.7,
    )
    last_err: Exception | None = None
    for model in models:
        try:
            resp = client().models.generate_content(
                model=model, contents=history, config=cfg
            )
            return (resp.text or "").strip()
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("нет доступной chat-модели")


def synth(text: str) -> bytes:
    """Текст → сырое PCM-аудио (24 кГц, mono, s16le) прямо из Gemini API."""
    styled = f"{CFG.voice_style} {text}".strip() if CFG.voice_style else text
    resp = client().models.generate_content(
        model=CFG.tts_model,
        contents=styled,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=CFG.voice,
                    )
                )
            ),
        ),
    )
    return resp.candidates[0].content.parts[0].inline_data.data


def make_user(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def make_model(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])
