"""Textual-приложение Sharp: визуализатор + лента диалога + поле ввода.

Поток: ввод (Enter) → assistant.process() в воркере → текст в ленту → синтез Gemini TTS
→ проигрывание, под которое двигается визуализатор. Микрофон — по Ctrl+K.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, RichLog, Static

from .. import audio, commands, config, gemini, memory
from ..assistant import Assistant
from ..config import CFG, VOICES
from ..wake import WAKE_WORD, extract_command
from .visualizer import Visualizer


class SharpApp(App):
    CSS_PATH = Path(__file__).with_name("sharp.tcss")
    BINDINGS = [
        Binding("ctrl+c", "quit", "Выход", priority=True),
        Binding("ctrl+k", "listen", "Микрофон вкл/выкл", priority=True),
        Binding("ctrl+l", "clear", "Очистить", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.assistant = Assistant()
        self.muted = False
        self.mic_on = True      # микрофон включён по умолчанию (классический режим)
        self._busy = threading.Event()
        self._shutdown_event = threading.Event()
        self._mic_worker_started = False
        self.live = None        # LiveSession, если активен реалтайм-режим

    def compose(self) -> ComposeResult:
        with Vertical(id="body"):
            yield Visualizer(id="viz")
        yield RichLog(id="log", wrap=True, markup=True, max_lines=40)
        with Horizontal(id="promptbar"):
            yield Static(">", id="prompt-mark")
            yield Input(id="input", placeholder="message…")
            yield Static("INITIALIZING", id="status")

    def on_mount(self) -> None:
        self.query_one("#input", Input).focus()
        if CFG.live_mode:
            self.set_status("PROBING NET")
            self._ensure_mic_worker()
            self.run_worker(self._probe_and_start, exclusive=False, thread=True)
        else:
            self._ensure_mic_worker()
            self.set_status(f"SAY {WAKE_WORD.upper()}")

    def _probe_and_start(self) -> None:
        """Проба сети: быстрая → Live, медленная → классический режим."""
        from ..live import network_ok
        ok, ms = network_ok()
        shown = f"{ms:.0f}мс" if ms != float("inf") else "нет сети"
        if ok:
            self.call_from_thread(self.log_line, f"[#707070]SYS[/]   Сеть быстрая ({shown}) — Live-режим.")
            self.call_from_thread(self._start_live)
        else:
            self.call_from_thread(self.log_line,
                                  f"[#707070]SYS[/]   Сеть слабая ({shown}) — классический режим. "
                                  "/live — попробовать Live вручную.")
            self.mic_on = True
            self.call_from_thread(self._ensure_mic_worker)
            self.call_from_thread(self.set_status, f"SAY {WAKE_WORD.upper()}")

    def set_status(self, state: str) -> None:
        self.query_one("#status", Static).update(state)

    def set_meta(self) -> None:
        """Метаданные намеренно скрыты: интерфейс остаётся минимальным."""

    def log_message(self, role: str, text: str) -> None:
        labels = {
            "user": "[#a0a0a0]YOU[/]   ",
            "voice": "[#a0a0a0]MIC[/]   ",
            "sharp": "[b #ffffff]SHARP[/] ",
        }
        self.log_line(f"{labels.get(role, '[#707070]SYS[/]   ')}{escape(text)}")

    def _ensure_mic_worker(self) -> None:
        if self._mic_worker_started:
            return
        self._mic_worker_started = True
        self.run_worker(self._mic_loop, exclusive=False, thread=True)

    # --- реалтайм голос↔голос (Gemini Live API) ---
    def _start_live(self) -> None:
        from ..live import LiveSession
        self.mic_on = False  # классический цикл молчит, пока Live активен
        self.live = LiveSession(
            on_user_text=lambda t: self.call_from_thread(self.log_message, "voice", t),
            on_sharp_text=lambda t: self.call_from_thread(self.log_message, "sharp", t),
            on_status=lambda t: self.call_from_thread(self._live_status, t),
            capture_audio=False,
        )
        self.set_status("CONNECTING")
        self.set_meta()
        self.live.start()

    def _live_status(self, text: str) -> None:
        self.log_line(f"[#707070]SYS[/]   {escape(text)}")
        if text.startswith("Live-режим активен"):
            self.mic_on = True
            self._ensure_mic_worker()
            self.set_status(f"SAY {WAKE_WORD.upper()}")
        elif text.startswith("Live лагает"):
            self._fallback_to_classic("Сеть не тянет Live — перешёл в классический режим. "
                                      "/live — вернуться, когда интернет наладится.")
        elif text.startswith(("Обрыв Live", "Live-сессия завершилась")):
            self._fallback_to_classic("Live отключён, включён классический голосовой режим.")

    def _fallback_to_classic(self, message: str) -> None:
        if self.live:
            live = self.live
            self.live = None
            self.run_worker(live.stop, exclusive=False, thread=True)
        self.mic_on = True
        self._ensure_mic_worker()
        self.log_line(f"[#707070]SYS[/]   {escape(message)}")
        self.set_status(f"SAY {WAKE_WORD.upper()}")

    def _stop_live(self) -> None:
        if self.live:
            self.live.stop()
            self.live = None
            self.set_meta()

    def log_line(self, text: str) -> None:
        # Все служебные сообщения, включая старые slash-команды, принудительно
        # приводятся к серому — цветовые акценты в монохромном режиме невозможны.
        text = re.sub(r"#[0-9a-fA-F]{6}", "#b0b0b0", text)
        self.query_one("#log", RichLog).write(text)

    # --- ввод ---
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#input", Input).value = ""
        if not text:
            return
        if text.startswith("/"):
            self._handle_slash(text)
            return
        self.log_message("user", text)
        if self.live:
            if not self.live.send_text(text):
                self.live = None
                self.mic_on = True
                self._ensure_mic_worker()
                self.run_worker(lambda: self._respond(text), exclusive=False, thread=True)
        else:
            if self._busy.is_set():
                self.log_line("[#d0a85c]SYSTEM[/]  Дождитесь завершения текущего ответа.")
                return
            self.run_worker(lambda: self._respond(text), exclusive=False, thread=True)

    def _handle_slash(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "/voice":
            if arg in VOICES:
                CFG.voice = arg
                config.save_config()
                self.set_meta()
                self.log_line(f"[#3fb950]Голос переключён на [b]{arg}[/] (сохранено).")
            else:
                self.log_line(f"[#f85149]Нет голоса «{arg}». /voices — список.")
        elif cmd == "/voices":
            self.log_line("[#8b949e]Голоса: " + ", ".join(VOICES))
        elif cmd == "/style":
            if arg:
                CFG.voice_style = f"Скажи {arg}:"
                config.save_config()
                self.log_line(f"[#3fb950]Стиль речи: {arg} (сохранено).")
            else:
                self.log_line(f"[#8b949e]Текущий стиль: {CFG.voice_style}")
        elif cmd == "/mute":
            self.muted = not self.muted
            self.log_line(f"[#d29922]Озвучка {'выключена' if self.muted else 'включена'}.")
        elif cmd == "/memory":
            facts = memory.all()
            if not facts:
                self.log_line("[#8b949e]Память пуста. Скажи «запомни …» или /remember.")
            else:
                self.log_line("[b #d29922]Память Шарпа:[/]")
                for i, f in enumerate(facts, 1):
                    self.log_line(f"  [#8b949e]{i}.[/] {f['text']}")
        elif cmd == "/remember":
            if arg:
                memory.add(arg)
                self.log_line(f"[#3fb950]Запомнил: {arg}")
            else:
                self.log_line("[#f85149]Что запомнить? /remember <факт>")
        elif cmd == "/forget":
            if arg.isdigit():
                removed = memory.remove(int(arg))
                self.log_line(f"[#d29922]Забыл: {removed}" if removed
                              else "[#f85149]Нет такого номера. /memory — список.")
            elif arg in ("all", "все", "всё"):
                n = memory.clear()
                self.log_line(f"[#d29922]Стёр {n} фактов.")
            else:
                self.log_line("[#f85149]/forget <номер> или /forget all")
        elif cmd == "/agent":
            sub = arg.split(maxsplit=1)
            if len(sub) == 2:
                if commands.normalize_agent(sub[0]):
                    result = self.assistant.begin_delegation(sub[0], sub[1])
                    self.log_message("sharp", result)
                else:
                    result = commands.delegate(sub[0], sub[1])
                    self.log_line(f"[#b0b0b0]AGENT[/] {result}")
            else:
                self.log_line("[#f85149]/agent <claude|codex|vscode> <промпт>")
        elif cmd == "/sessions":
            agent = commands.normalize_agent(arg)
            if not agent:
                self.log_line("[#f85149]/sessions <codex|claude>")
            else:
                sessions = commands.agent_sessions(agent)
                self.log_line(f"[b]Последние сессии {agent}:[/]")
                for index, session in enumerate(sessions, 1):
                    self.log_line(f"{index}. {session.title}  [#707070]{session.session_id[:8]}[/]")
        elif cmd in ("/ym", "/yandex"):
            sub = arg.split(maxsplit=1)
            action = sub[0].lower() if sub else ""
            query = sub[1] if len(sub) > 1 else ""
            aliases = {"волна": "wave", "моя-волна": "wave", "громче": "volumeup",
                       "тише": "volumedown", "исполнитель": "artist"}
            action = aliases.get(action, action)
            result = commands.yandex_music(action, query)
            self.log_line(f"[#b0b0b0]YANDEX[/] {result}")
        elif cmd == "/live":
            if self.live:
                self._stop_live()
                CFG.live_mode = False
                config.save_config()
                self.log_line("[#d29922]Классический режим (STT→chat→TTS). "
                              "Микрофон-цикл включён, Ctrl+K — вкл/выкл.")
                self.mic_on = True
                self.set_status("LISTENING")
            else:
                CFG.live_mode = True
                config.save_config()
                self.mic_on = False  # глушим классический цикл
                self._start_live()
        elif cmd == "/clear":
            self.assistant.clear()
            self.query_one("#log", RichLog).clear()
            self.log_line("[#8b949e]История очищена (память фактов сохранена).")
        elif cmd in ("/help", "/?"):
            self.log_line(
                "[b]Команды:[/] /live (реалтайм↔классика), /voice <имя>, /voices, "
                "/style <как>, /mute, /memory, /remember <факт>, /forget <n|all>, "
                "/sessions <codex|claude>, /agent <агент> <промпт>, "
                "/ym <wave|play|pause|next|prev|volumeup|volumedown|artist имя>, /clear")
        else:
            self.log_line(f"[#f85149]Неизвестная команда: {cmd}. /help — список.")

    # --- обработка запроса + голос (в отдельном потоке) ---
    def _respond(self, text: str) -> None:
        if self._busy.is_set():
            return
        self._busy.set()
        self.call_from_thread(self.set_status, "THINKING")
        try:
            reply = self.assistant.process(text)
            if reply is None:
                self.call_from_thread(self.log_line, "[#8b949e]…остановлено.")
                return
            self.call_from_thread(self.log_message, "sharp", reply)
            if not self.muted:
                try:
                    self.call_from_thread(self.set_status, "SPEAKING")
                    pcm = gemini.synth(reply)
                    audio.play_pcm_blocking(pcm)
                except Exception as e:  # noqa: BLE001
                    self.call_from_thread(self.log_line, f"[#f85149]Озвучка не удалась: {str(e)[:80]}")
        finally:
            self._busy.clear()
            state = f"SAY {WAKE_WORD.upper()}" if self.mic_on else "READY"
            self.call_from_thread(self.set_status, state)

    # --- микрофон: постоянный фоновый цикл (включён по умолчанию) ---
    def _mic_loop(self) -> None:
        try:
            from .. import stt
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.log_line, f"[#f85149]Микрофон недоступен: {str(e)[:80]}")
            self.mic_on = False
            return
        import time
        while not self._shutdown_event.is_set():
            # не слушаем, пока Шарп думает/говорит или микрофон выключен
            if not self.mic_on or self._busy.is_set():
                time.sleep(0.2)
                continue
            live = self.live
            if live and live.speaking_event.is_set():
                time.sleep(0.1)
                continue
            try:
                # Если ответ начался, прерываем уже открытый InputStream, чтобы
                # он не успел записать и затем распознать голос самого Sharp.
                cancel_event = live.speaking_event if live else self._busy
                text = stt.listen_once(cancel_event=cancel_event)
            except Exception as e:  # noqa: BLE001
                self.call_from_thread(self.log_line, f"[#f85149]Ошибка микрофона: {str(e)[:80]}")
                time.sleep(1.0)
                continue
            if not text or self._busy.is_set() or not self.mic_on:
                continue
            command = extract_command(text)
            if command is None:
                continue
            self.call_from_thread(self.log_message, "voice", text)
            if self.live:
                if not self.live.send_text(command):
                    self.call_from_thread(
                        self._fallback_to_classic,
                        "Live не отвечает — включён классический голосовой режим.",
                    )
                    self._respond(command)
            else:
                self._respond(command)

    def action_listen(self) -> None:
        if self.live:
            self.mic_on = not self.mic_on
            on = self.mic_on
            self.set_status(f"SAY {WAKE_WORD.upper()}" if on else "MIC PAUSED")
            self.log_line(f"[#d29922]Микрофон {'включён — говорите' if on else 'на паузе'}.")
            return
        self.mic_on = not self.mic_on
        self.set_status(f"SAY {WAKE_WORD.upper()}" if self.mic_on else "MIC PAUSED")
        self.log_line(f"[#d29922]Микрофон {'включён — говорите' if self.mic_on else 'выключен'}.")

    def action_clear(self) -> None:
        self._handle_slash("/clear")

    def on_unmount(self) -> None:
        self._shutdown_event.set()
        self.mic_on = False
        self._stop_live()


def run() -> None:
    SharpApp().run()
