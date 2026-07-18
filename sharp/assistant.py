"""Оркестратор Sharp: системный промпт, история диалога, обработка запроса.

process(text) -> ответ (строка). Сначала быстрые локальные команды (медиа, время,
файлы), иначе запрос в Gemini, затем разбор командных префиксов из ответа
(OPEN:/RUN:/STEAM:/SEARCH:/MEDIA:...). Возвращаемый текст TUI показывает и озвучивает.
"""
from __future__ import annotations

import json
import re

from . import commands, config, gemini, memory
from .config import CFG, HISTORY_PATH, VOICES
from .storage import atomic_write_json

MAX_HISTORY = 20

SYSTEM_PROMPT = """Ты — ИИ-ассистент «Шарп» (Sharp), голосовой помощник в терминале Linux.
Отвечай ОЧЕНЬ кратко, 1-2 предложения, живым разговорным языком. Обращайся «сэр».
Твоё имя Шарп. Помнишь контекст разговора.

Ты можешь управлять компьютером — вставь команду в любом месте ответа:
- MEDIA:play|pause|next|prev|volumeup|volumedown|mute — музыка и громкость
- YANDEX:wave|play|pause|next|prev|volumeup|volumedown — Яндекс Музыка
- YANDEX:artist|имя — найти и включить страницу исполнителя в Яндекс Музыке
- OPEN:домен.com — открыть сайт
- RUN:программа — запустить приложение
- STEAM:ID — запустить игру в Steam по App ID
- SEARCH:запрос — поискать в Google
- TERM:команда — выполнить ЛЮБУЮ команду в терминале Linux (открыть программу с
  аргументами, например TERM:dolphin ~/Рабочий стол  или  TERM:libreoffice --writer)
- INSTALL_REQUEST:пакет — подготовить установку и спросить отдельное подтверждение
- INSTALL:пакет — только в следующей реплике после явного подтверждения открыть pacman
- READFILE:путь — прочитать файл
- LISTDIR:путь — показать содержимое папки
- REMEMBER:факт — запомнить факт о пользователе навсегда
- SETVOICE:Имя — сменить свой голос (Charon, Kore, Puck, Zephyr, Fenrir, Aoede …)
- SETSTYLE:описание — сменить стиль своей речи
- AGENT:claude|промпт — делегировать задачу Claude Code в новом терминале
- AGENT:codex|промпт — делегировать задачу Codex в новом терминале
- AGENT:vscode|промпт — открыть VS Code, промпт кладётся в буфер обмена

Если пользователь просит тебя что-то запомнить — используй REMEMBER:.
Если просит написать код/большую задачу для другого агента — используй AGENT:.
Если не знаешь точного ответа — используй SEARCH:запрос.
Команды пиши латиницей ровно как показано, значение — после двоеточия без пробела."""


class Assistant:
    def __init__(self) -> None:
        self.history: list[dict] = []
        self.pending_delegation: dict | None = None
        self.pending_session_list = False
        self.media_context: str | None = None
        self._load()

    # --- история (JSON, роль/текст) ---
    def _load(self) -> None:
        try:
            if HISTORY_PATH.exists():
                self.history = json.loads(HISTORY_PATH.read_text("utf-8"))[-MAX_HISTORY:]
        except Exception:  # noqa: BLE001
            self.history = []

    def _save(self) -> None:
        try:
            atomic_write_json(HISTORY_PATH, self.history[-MAX_HISTORY:])
        except Exception:  # noqa: BLE001
            pass

    def _add(self, role: str, text: str) -> None:
        self.history.append({"role": role, "text": text})
        self.history = self.history[-MAX_HISTORY:]
        self._save()

    def clear(self) -> None:
        self.history = []
        self._save()

    def _contents(self) -> list:
        out = []
        for m in self.history:
            role = "model" if m["role"] == "assistant" else "user"
            out.append(gemini.make_user(m["text"]) if role == "user"
                       else gemini.make_model(m["text"]))
        return out

    # --- главная точка входа ---
    def process(self, text: str) -> str | None:
        t = text.lower().strip()
        self._add("user", text)

        if self.pending_delegation:
            return self._reply(self._finish_delegation(t))

        if self.pending_session_list:
            agent = self._agent_from_text(t)
            if not agent:
                return self._reply("Уточните: Codex или Claude Code, сэр?")
            self.pending_session_list = False
            return self._reply(self._describe_sessions(agent))

        if t in ("стоп", "хватит", "остановись", "stop"):
            return None

        # время
        if "врем" in t or ("час" in t and "стоп" not in t):
            from datetime import datetime
            now = datetime.now()
            return self._reply(f"Сейчас {now:%H:%M}, сэр.")

        yandex = self._quick_yandex(text, t)
        if yandex:
            return self._reply(yandex)

        sessions = self._voice_session_control(t)
        if sessions:
            return self._reply(sessions)

        # быстрые медиа-команды (без обращения к Gemini)
        quick = self._quick_media(t)
        if quick:
            return self._reply(quick)

        # долговременная память проверяется раньше истории: «забудь всё» не должно
        # случайно превращаться в простой сброс контекста.
        if any(k in t for k in ("забудь всё", "забудь все", "очисти память", "сотри память")):
            n = memory.clear()
            return self._reply(f"Стёр {n} фактов из памяти, сэр.")

        # очистка истории диалога (память фактов при этом НЕ трогаем)
        if any(k in t for k in ("забудь разговор", "очисти историю", "сбрось контекст")):
            self.clear()
            return "Историю очистил, сэр. Факты о вас я помню."

        # локальные команды настроек/памяти/делегирования (без обращения к Gemini)
        local = self._local_control(text, t)
        if local:
            return self._reply(local)

        # иначе — Gemini (с подмешанной долговременной памятью)
        try:
            answer = gemini.chat(self._contents(), SYSTEM_PROMPT + memory.as_prompt())
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                return "Превышена квота Gemini, сэр. Подождите минуту."
            if "503" in msg or "UNAVAILABLE" in msg:
                return "Модель сейчас перегружена, сэр. Повторите запрос."
            return f"Ошибка связи, сэр: {msg[:120]}"
        if not answer:
            return self._reply("Не понял, сэр.")

        return self._reply(self._handle_commands(answer))

    def _reply(self, text: str) -> str:
        self._add("assistant", text)
        return text

    def _quick_media(self, t: str) -> str | None:
        if any(k in t for k in ("пауз", "останови музык")):
            commands.media("pause"); return "Пауза."
        if "продолж" in t or ("играй" in t and "музык" in t):
            commands.media("play"); return "Воспроизвожу."
        if any(k in t for k in ("следующ", "дальше", "некст")):
            commands.media("next"); return "Следующий трек."
        if any(k in t for k in ("предыдущ", "прошл трек")):
            commands.media("prev"); return "Предыдущий трек."
        if any(k in t for k in ("громче", "прибавь")):
            commands.volume("up"); return "Громче."
        if any(k in t for k in ("тише", "убавь", "потише")):
            commands.volume("down"); return "Тише."
        return None

    def _quick_yandex(self, text: str, t: str) -> str | None:
        explicit_yandex = "яндекс" in t or "мою волн" in t or "моя волн" in t
        is_yandex = explicit_yandex or self.media_context == "yandex"
        if not is_yandex and "исполнител" not in t and "артист" not in t:
            return None
        self.media_context = "yandex"
        if "волн" in t:
            result = commands.yandex_music("wave")
            return "Включаю Мою волну, сэр." if result == "ok" else result
        if is_yandex and any(phrase in t for phrase in (
            "включи яндекс музыку", "запусти яндекс музыку", "открой яндекс музыку"
        )):
            result = commands.yandex_music("play")
            return "Включаю Яндекс Музыку, сэр." if result == "ok" else result
        actions = (
            (("пауз", "останов"), "pause", "Ставлю на паузу, сэр."),
            (("продолж", "включи музыку", "играй"), "play", "Продолжаю, сэр."),
            (("следующ", "дальше"), "next", "Следующий трек, сэр."),
            (("предыдущ",), "prev", "Предыдущий трек, сэр."),
            (("громче", "прибав"), "volumeup", "Прибавил громкость, сэр."),
            (("тише", "убав", "потише"), "volumedown", "Убавил громкость, сэр."),
        )
        for keys, action, reply in actions:
            if any(key in t for key in keys):
                result = commands.yandex_music(action)
                return reply if result == "ok" else result

        match = re.search(
            r"(?:включи|поставь|запусти|найди)\s+(?:в\s+яндекс\s+музыке\s+)?"
            r"(?:(?:исполнителя|артиста)\s+)?(.+?)(?:\s+в\s+яндекс\s+музыке)?$",
            text, re.IGNORECASE,
        )
        if match:
            artist = match.group(1).strip()
            result = commands.yandex_music("artist", artist)
            return f"Открываю {artist} в Яндекс Музыке, сэр." if result == "ok" else result
        return None

    @staticmethod
    def _agent_from_text(text: str) -> str | None:
        if any(name in text for name in ("codex", "кодекс", "кодэкс", "codo")):
            return "codex"
        if any(name in text for name in ("claude", "claude code", "клод", "клауд", "клоуд")):
            return "claude"
        return None

    def _voice_session_control(self, text: str) -> str | None:
        """Естественные голосовые команды просмотра сохранённых сессий."""
        if "сесси" not in text:
            return None
        wants_list = any(word in text for word in (
            "покажи", "покажите", "перечисли", "назови", "какие", "список", "доступн",
        ))
        if not wants_list:
            return None
        agent = self._agent_from_text(text)
        if not agent:
            self.pending_session_list = True
            return "Сессии Codex или Claude Code, сэр?"
        return self._describe_sessions(agent)

    @staticmethod
    def _describe_sessions(agent: str) -> str:
        sessions = commands.agent_sessions(agent, limit=6)
        who = "Codex" if agent == "codex" else "Claude Code"
        if not sessions:
            return f"Сохранённых сессий {who} не найдено, сэр."
        lines = [f"Последние сессии {who}:"]
        lines.extend(f"{index}. {session.title}" for index, session in enumerate(sessions, 1))
        return "\n".join(lines)

    def begin_delegation(self, target: str, prompt: str) -> str:
        agent = commands.normalize_agent(target)
        if not agent:
            return f"Неизвестный агент: {target}."
        sessions = commands.agent_sessions(agent, limit=6)
        self.pending_delegation = {"agent": agent, "prompt": prompt, "sessions": sessions}
        who = "Codex" if agent == "codex" else "Claude Code"
        lines = [f"В какую сессию {who} отправить задачу?"]
        lines.extend(f"{index}. {session.title}" for index, session in enumerate(sessions, 1))
        lines.append("0. Новая сессия")
        lines.append("Ответьте номером или скажите «отмена».")
        return "\n".join(lines)

    def _finish_delegation(self, choice: str) -> str:
        pending = self.pending_delegation
        if not pending:
            return "Нет ожидающего делегирования, сэр."
        if choice in ("отмена", "отмени", "cancel"):
            self.pending_delegation = None
            return "Делегирование отменено, сэр."
        words = {"ноль": 0, "новая": 0, "новую": 0, "первая": 1, "первую": 1,
                 "вторая": 2, "вторую": 2, "третья": 3, "третью": 3,
                 "четвёртая": 4, "четвертая": 4, "пятая": 5, "шестая": 6}
        digit = re.search(r"\d+", choice)
        number = int(digit.group()) if digit else next(
            (value for word, value in words.items() if word in choice), None
        )
        sessions = pending["sessions"]
        if number is None or number < 0 or number > len(sessions):
            return f"Выберите номер от 0 до {len(sessions)} или скажите «отмена»."
        self.pending_delegation = None
        session = sessions[number - 1] if number else None
        result = commands.delegate_to_session(
            pending["agent"], pending["prompt"],
            session.session_id if session else None,
            session.cwd if session else None,
        )
        if result != "ok":
            return f"Не удалось делегировать: {result}"
        destination = session.title if session else "новую сессию"
        return f"Отправил задачу в {destination}, сэр."

    def _local_control(self, text: str, t: str) -> str | None:
        """Голосовое/текстовое управление памятью, голосом и делегированием — без Gemini."""
        # --- запомнить факт ---
        m = re.search(r"(?:запомни|запиши|заметь)[,:]?\s+(.+)", text, re.IGNORECASE)
        if m:
            fact = memory.add(m.group(1).strip())
            return f"Запомнил, сэр: {fact}" if fact else "Нечего запоминать, сэр."

        # --- что помнишь ---
        if any(k in t for k in ("что ты помнишь", "что помнишь", "что ты знаешь обо мне")):
            facts = memory.texts()
            if not facts:
                return "Пока ничего не запомнил, сэр."
            return "Я помню, сэр: " + "; ".join(facts[:8]) + "."

        # --- сменить голос ---
        m = re.search(r"(?:смени голос|поменяй голос|голос)\s+(?:на\s+)?([a-zA-Zа-яА-Я]+)",
                      text, re.IGNORECASE)
        if m and ("голос" in t):
            name = m.group(1).strip().capitalize()
            match = next((v for v in VOICES if v.lower() == name.lower()), None)
            if match:
                CFG.voice = match
                config.save_config()
                return f"Голос сменил на {match}, сэр."
            return f"Нет голоса «{name}», сэр. Есть: {', '.join(VOICES[:8])} и другие."

        # --- сменить стиль речи ---
        m = re.search(r"(?:говори|веди себя|стиль речи|разговаривай)\s+(.+)", text, re.IGNORECASE)
        if m and any(k in t for k in ("говори", "стиль", "веди себя", "разговаривай")):
            style = m.group(1).strip()
            CFG.voice_style = f"Скажи {style}:"
            config.save_config()
            return f"Буду говорить {style}, сэр."

        # --- делегирование агентам ---
        m = re.search(r"(?:спроси|попроси|отправь|передай|скажи|делегируй)\s+(?:в\s+)?"
                      r"(клод[ауе]?|клауд[ауе]?|claude(?:\s+code)?|codex|кодекс(?:у)?|codo|vs\s*code|вскод)\b[,:]?\s*(.+)",
                      text, re.IGNORECASE)
        if m:
            target, prompt = m.group(1), m.group(2).strip()
            if commands.normalize_agent(target):
                return self.begin_delegation(target, prompt)
            res = commands.delegate(target, prompt)
            who = "Claude" if "кл" in target.lower() or "cl" in target.lower() else \
                  ("Codex" if "codex" in target.lower() or "кодекс" in target.lower() else "VS Code")
            if res == "ok":
                return f"Отправил {who}, сэр."
            return f"Не вышло отправить {who}: {res}"

        return None

    def _handle_commands(self, answer: str) -> str:
        """Найти командные префиксы в ответе Gemini, выполнить, вернуть чистый текст."""
        execution_notes: list[str] = []
        # MEDIA
        m = re.search(r"MEDIA:\s*(\w+)", answer)
        if m:
            commands.media(m.group(1).lower())
        # YANDEX:action или YANDEX:artist|имя
        m = re.search(r"YANDEX:\s*(\w+)(?:\|([^\n]+))?", answer)
        if m:
            result = commands.yandex_music(m.group(1).lower(), (m.group(2) or "").strip())
            if result != "ok":
                execution_notes.append(result)
        # OPEN
        m = re.search(r"OPEN:\s*([^\s]+)", answer)
        if m:
            commands.open_url(m.group(1))
        # RUN
        m = re.search(r"RUN:\s*([^\n]+)", answer)
        if m:
            commands.open_app(m.group(1).strip())
        # STEAM
        m = re.search(r"STEAM:\s*(\d+)", answer)
        if m:
            commands.open_steam(m.group(1))
        # SEARCH
        m = re.search(r"SEARCH:\s*([^\n]+)", answer)
        if m:
            commands.search(m.group(1).strip())
        # TERM — выполнить произвольную команду в системе
        m = re.search(r"TERM:\s*([^\n]+)", answer)
        if m:
            result = commands.run_shell(m.group(1).strip())
            execution_notes.append(f"Вывод команды: {result[:1200]}")
        # INSTALL_REQUEST / INSTALL — установка всегда состоит из двух голосовых ходов
        m = re.search(r"INSTALL_REQUEST:\s*([^\s]+)", answer)
        if m:
            execution_notes.append(commands.prepare_package_install(m.group(1).strip()))
        m = re.search(r"INSTALL:\s*([^\s]+)", answer)
        if m:
            execution_notes.append(commands.install_package(m.group(1).strip()))
        # REMEMBER — запомнить факт навсегда
        m = re.search(r"REMEMBER:\s*([^\n]+)", answer)
        if m:
            memory.add(m.group(1).strip())
        # SETVOICE — сменить голос и сохранить
        m = re.search(r"SETVOICE:\s*([A-Za-z]+)", answer)
        if m:
            name = next((v for v in VOICES if v.lower() == m.group(1).strip().lower()), None)
            if name:
                CFG.voice = name
                config.save_config()
        # SETSTYLE — сменить стиль речи и сохранить
        m = re.search(r"SETSTYLE:\s*([^\n]+)", answer)
        if m:
            CFG.voice_style = f"Скажи {m.group(1).strip()}:"
            config.save_config()
        # AGENT:target|prompt — делегировать другому агенту
        m = re.search(r"AGENT:\s*([^|\n]+)\|(.+)", answer)
        if m:
            execution_notes.append(self.begin_delegation(m.group(1).strip(), m.group(2).strip()))
        # READFILE / LISTDIR — подставляем результат в ответ
        m = re.search(r"READFILE:\s*([^\n]+)", answer)
        if m:
            content = commands.read_file(m.group(1).strip())
            answer = answer.replace(m.group(0), "").strip() + f"\n{content[:500]}"
        m = re.search(r"LISTDIR:\s*([^\n]+)", answer)
        if m:
            content = commands.list_dir(m.group(1).strip())
            answer = answer.replace(m.group(0), "").strip() + f"\n{content}"

        # вычищаем все командные префиксы из текста, который увидит/озвучит пользователь
        clean = re.sub(
            r"(MEDIA|YANDEX|OPEN|RUN|STEAM|SEARCH|TERM|INSTALL_REQUEST|INSTALL|READFILE|LISTDIR|REMEMBER|SETVOICE|SETSTYLE):\s*[^\n]*",
            "", answer,
        )
        clean = re.sub(r"AGENT:\s*[^|\n]+\|[^\n]*", "", clean).strip()
        if execution_notes:
            return " ".join(filter(None, (clean, *execution_notes)))
        return clean or "Готово, сэр."
