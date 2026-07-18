"""Инструменты Gemini (function calling) — чтобы Шарп сам управлял компьютером.

В Live-режиме модель вызывает эти функции напрямую (голосом: «открой dolphin» →
модель зовёт run_shell). Здесь и декларации для Gemini, и диспетчер execute(),
который маршрутизирует вызов в commands.py / memory.py / config.

Один источник правды: TOOLS — список FunctionDeclaration, execute() — исполнитель.
"""
from __future__ import annotations

from google.genai import types

from . import commands, config, memory
from .config import CFG, VOICES

# Системный промпт для Live-режима: здесь модель управляет компьютером ТОЛЬКО через
# вызовы функций (инструменты ниже), а не текстовыми метками RUN:/OPEN: как в классике.
LIVE_SYSTEM_PROMPT = """Ты — голосовой ассистент «Шарп» (Sharp) в терминале Linux (CachyOS).
Отвечай ОЧЕНЬ кратко, 1-2 предложения, живым разговорным языком. Обращайся «сэр».

Пользователь управляет тобой ГОЛОСОМ. Никогда не требуй slash-команды и не предлагай
что-либо печатать. Задавай уточняющие вопросы голосом и понимай ответы «первая»,
«вторая», «новая», «отмена».

Ты управляешь компьютером ТОЛЬКО через вызовы инструментов (function calling).
НИКОГДА не пиши команды текстом (никаких RUN:, OPEN: и т.п.) — вместо этого ВЫЗЫВАЙ функцию.
- Открыть любую программу/файл/папку → вызови run_shell с полной командой
  (пример: run_shell("dolphin ~/Рабочий стол"), run_shell("libreoffice --writer")).
- Сайт → open_url, поиск → search_web, игра в Steam → open_steam_game.
- Музыка/громкость → media_control. Запомнить факт → remember_fact.
- Сменить свой голос → set_voice. Перед делегированием вызови list_agent_sessions,
  голосом перечисли варианты с номерами и спроси, в какую сессию отправить задачу.
  После голосового выбора → delegate_agent с session_id;
  для новой сессии передай пустой session_id.
- Яндекс Музыка → yandex_music (Моя волна, треки, громкость, исполнитель).
  Понимай естественные фразы: «включи Мою волну», «поставь на паузу», «следующий
  трек», «сделай тише», «включи исполнителя Кино».
- Прочитать файл → read_file, содержимое папки → list_dir.
Сначала коротко подтверди голосом («Открываю, сэр»), затем вызови нужный инструмент."""


def _schema(props: dict, required: list[str]) -> types.Schema:
    return types.Schema(
        type="OBJECT",
        properties={k: types.Schema(**v) for k, v in props.items()},
        required=required,
    )


# Декларации инструментов, которые видит Gemini.
TOOLS: list[types.FunctionDeclaration] = [
    types.FunctionDeclaration(
        name="run_shell",
        description="Выполнить команду в терминале Linux: открыть программу, файл или папку "
                    "с любыми аргументами. Примеры: 'dolphin ~/Рабочий стол', "
                    "'libreoffice --writer', 'code ~/project'. Так открывается ВСЁ.",
        parameters=_schema(
            {"command": {"type": "STRING",
                         "description": "Полная команда с аргументами, как в терминале"}},
            ["command"],
        ),
    ),
    types.FunctionDeclaration(
        name="open_url",
        description="Открыть сайт в браузере по умолчанию.",
        parameters=_schema(
            {"url": {"type": "STRING", "description": "Домен или ссылка, напр. youtube.com"}},
            ["url"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_web",
        description="Найти что-то в Google (открывает результаты в браузере).",
        parameters=_schema(
            {"query": {"type": "STRING", "description": "Поисковый запрос"}},
            ["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="open_steam_game",
        description="Запустить игру в Steam по её App ID (число).",
        parameters=_schema(
            {"app_id": {"type": "STRING", "description": "Steam App ID, напр. 730 для CS2"}},
            ["app_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="media_control",
        description="Управление музыкой и громкостью системы.",
        parameters=_schema(
            {"action": {"type": "STRING",
                        "description": "Одно из: play, pause, next, prev, "
                                       "volumeup, volumedown, mute"}},
            ["action"],
        ),
    ),
    types.FunctionDeclaration(
        name="remember_fact",
        description="Запомнить факт о пользователе навсегда (переживает перезапуск).",
        parameters=_schema(
            {"fact": {"type": "STRING", "description": "Что запомнить"}},
            ["fact"],
        ),
    ),
    types.FunctionDeclaration(
        name="set_voice",
        description=f"Сменить голос Шарпа. Доступны: {', '.join(VOICES[:12])} и другие.",
        parameters=_schema(
            {"voice": {"type": "STRING", "description": "Имя голоса, напр. Kore, Puck, Charon"}},
            ["voice"],
        ),
    ),
    types.FunctionDeclaration(
        name="yandex_music",
        description="Управление Яндекс Музыкой через её активный плеер.",
        parameters=_schema(
            {"action": {"type": "STRING", "description": "wave, play, pause, next, prev, volumeup, volumedown или artist"},
             "query": {"type": "STRING", "description": "Имя исполнителя для action=artist"}},
            ["action"],
        ),
    ),
    types.FunctionDeclaration(
        name="list_agent_sessions",
        description="Показать последние сессии Codex или Claude Code перед делегированием.",
        parameters=_schema(
            {"agent": {"type": "STRING", "description": "codex или claude"}},
            ["agent"],
        ),
    ),
    types.FunctionDeclaration(
        name="delegate_agent",
        description="Делегировать задачу другому AI-агенту: claude или codex в новом "
                    "терминале, vscode — открыть редактор с промптом в буфере.",
        parameters=_schema(
            {"agent": {"type": "STRING", "description": "claude, codex или vscode"},
             "prompt": {"type": "STRING", "description": "Задача/промпт для агента"},
             "session_id": {"type": "STRING", "description": "ID выбранной сессии; пусто для новой"}},
            ["agent", "prompt"],
        ),
    ),
    types.FunctionDeclaration(
        name="read_file",
        description="Прочитать текстовый файл и вернуть его содержимое.",
        parameters=_schema(
            {"path": {"type": "STRING", "description": "Путь к файлу"}},
            ["path"],
        ),
    ),
    types.FunctionDeclaration(
        name="list_dir",
        description="Показать содержимое папки.",
        parameters=_schema(
            {"path": {"type": "STRING", "description": "Путь к папке"}},
            ["path"],
        ),
    ),
]


def gemini_tool() -> types.Tool:
    """Собрать все декларации в один Tool для LiveConnectConfig / GenerateContentConfig."""
    return types.Tool(function_declarations=TOOLS)


def execute(name: str, args: dict) -> dict:
    """Выполнить вызов инструмента, вернуть {result: ...} для send_tool_response."""
    a = args or {}
    try:
        if name == "run_shell":
            return {"result": commands.run_shell(a.get("command", ""))}
        if name == "open_url":
            return {"result": commands.open_url(a.get("url", ""))}
        if name == "search_web":
            return {"result": commands.search(a.get("query", ""))}
        if name == "open_steam_game":
            return {"result": commands.open_steam(a.get("app_id", ""))}
        if name == "media_control":
            return {"result": commands.media(a.get("action", "").lower())}
        if name == "yandex_music":
            return {"result": commands.yandex_music(a.get("action", ""), a.get("query", ""))}
        if name == "remember_fact":
            fact = memory.add(a.get("fact", ""))
            return {"result": f"запомнил: {fact}" if fact else "нечего запоминать"}
        if name == "set_voice":
            want = a.get("voice", "").strip()
            match = next((v for v in VOICES if v.lower() == want.lower()), None)
            if match:
                CFG.voice = match
                config.save_config()
                return {"result": f"голос сменён на {match}"}
            return {"result": f"нет голоса {want}"}
        if name == "list_agent_sessions":
            sessions = commands.agent_sessions(a.get("agent", ""))
            return {"result": [
                {"number": i, "id": session.session_id, "title": session.title}
                for i, session in enumerate(sessions, 1)
            ]}
        if name == "delegate_agent":
            session_id = a.get("session_id") or None
            return {"result": commands.delegate_to_session(
                a.get("agent", ""), a.get("prompt", ""), session_id
            )}
        if name == "read_file":
            return {"result": commands.read_file(a.get("path", ""))[:1500]}
        if name == "list_dir":
            return {"result": commands.list_dir(a.get("path", ""))}
    except Exception as e:  # noqa: BLE001
        return {"result": f"ошибка: {str(e)[:120]}"}
    return {"result": f"неизвестный инструмент: {name}"}
