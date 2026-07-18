"""Долговременная память Sharp — факты, которые ассистент помнит между сессиями.

Отличается от истории диалога (history.json): память переживает /clear и
подмешивается в системный промпт, чтобы Шарп «знал» тебя. Хранится в
~/.config/sharp/memory.json как список записей {id, text, ts}.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .config import CONFIG_DIR
from .storage import atomic_write_json

MEMORY_PATH = CONFIG_DIR / "memory.json"
MAX_FACTS = 100


def _read() -> list[dict]:
    try:
        if MEMORY_PATH.exists():
            data = json.loads(MEMORY_PATH.read_text("utf-8"))
            if isinstance(data, list):
                return data
    except Exception:  # noqa: BLE001
        pass
    return []


def _write(facts: list[dict]) -> None:
    atomic_write_json(MEMORY_PATH, facts[-MAX_FACTS:])


def add(text: str) -> str:
    """Запомнить факт. Возвращает сам текст факта (для подтверждения)."""
    text = text.strip()
    if not text:
        return ""
    facts = _read()
    # не дублируем один и тот же факт
    if any(f["text"].lower() == text.lower() for f in facts):
        return text
    facts.append({"id": uuid.uuid4().hex, "text": text, "ts": time.time()})
    _write(facts)
    return text


def all() -> list[dict]:
    return _read()


def texts() -> list[str]:
    return [f["text"] for f in _read()]


def remove(index: int) -> str | None:
    """Удалить факт по 1-based номеру (как показан в /memory). Вернёт текст или None."""
    facts = _read()
    if 1 <= index <= len(facts):
        removed = facts.pop(index - 1)
        _write(facts)
        return removed["text"]
    return None


def clear() -> int:
    n = len(_read())
    _write([])
    return n


def as_prompt() -> str:
    """Блок фактов для вставки в системный промпт (пусто, если памяти нет)."""
    facts = texts()
    if not facts:
        return ""
    lines = "\n".join(f"- {t}" for t in facts)
    return f"\n\nЧто ты помнишь о пользователе (используй при ответах):\n{lines}"
