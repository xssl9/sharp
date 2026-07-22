from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sharp import commands, config
from sharp.agent_sessions import AgentSession, list_codex_sessions
from sharp.assistant import Assistant
from sharp.commands import (
    install_package,
    open_app,
    prepare_package_install,
    run_shell,
    run_terminal,
)
from sharp.live import (
    IN_BLOCK,
    IN_RATE,
    LIVE_MAX_OUTPUT_TOKENS,
    STREAM_PREBUFFER_MS,
    LiveSession,
    build_live_config,
)
from sharp.storage import atomic_write_json
from sharp.stt import _transcripts_from_google, listen_candidates
from sharp.tools import LIVE_SYSTEM_PROMPT
from sharp.wake import extract_command


class StorageTests(unittest.TestCase):
    def test_atomic_json_replaces_complete_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_write_json(path, {"ready": True})
            self.assertEqual(path.read_text("utf-8"), '{\n  "ready": true\n}\n')


class ConfigTests(unittest.TestCase):
    def test_save_migrates_legacy_key_out_of_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "config.toml"
            credentials = root / "credentials.toml"
            settings.write_text('api_key = "legacy-secret"\nvoice = "Kore"\n', "utf-8")

            with (
                patch.object(config, "CONFIG_PATH", settings),
                patch.object(config, "CREDENTIALS_PATH", credentials),
            ):
                config.save_config()

            self.assertNotIn("legacy-secret", settings.read_text("utf-8"))
            self.assertIn("legacy-secret", credentials.read_text("utf-8"))


class AssistantTests(unittest.TestCase):
    def test_forget_everything_clears_memory_not_history_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.json"
            with (
                patch("sharp.assistant.HISTORY_PATH", history),
                patch("sharp.assistant.memory.clear", return_value=3) as clear_memory,
            ):
                reply = Assistant().process("забудь всё")

            clear_memory.assert_called_once_with()
            self.assertEqual(reply, "Стёр 3 фактов из памяти, сэр.")

    def test_shell_commands_are_disabled_by_default(self) -> None:
        with patch.object(config.CFG, "allow_shell_commands", False):
            result = run_shell("echo should-not-run")
        self.assertIn("отключены", result)

    def test_terminal_output_is_returned_to_assistant(self) -> None:
        with patch.object(config.CFG, "allow_shell_commands", True):
            result = run_terminal("printf sharp-output")
        self.assertIn("exit_code=0", result)
        self.assertIn("sharp-output", result)

    def test_open_app_does_not_require_shell_permission(self) -> None:
        with (
            patch.object(config.CFG, "allow_shell_commands", False),
            patch("sharp.commands.shutil.which", return_value="/usr/bin/dolphin"),
            patch("sharp.commands._run", return_value="ok") as run,
        ):
            result = open_app("проводник")
        self.assertEqual(result, "ok")
        run.assert_called_once_with(["dolphin"])

    def test_install_package_validates_name_and_opens_pacman_terminal(self) -> None:
        self.assertIn("некорректное", install_package("firefox; reboot"))
        self.assertIn("не подтверждена", install_package("firefox"))
        self.assertIn("подтверждение", prepare_package_install("firefox"))
        with (
            patch("sharp.commands.shutil.which", return_value="/usr/bin/pacman"),
            patch("sharp.commands._term", return_value=["kitty", "--hold", "-e"]),
            patch("sharp.commands._run_in_terminal", return_value="ok") as run,
        ):
            result = install_package("firefox")
        self.assertIn("firefox", result)
        run.assert_called_once_with([
            "kitty", "--hold", "-e", "sudo", "pacman", "-S", "--needed", "firefox"
        ])

    def test_delegation_asks_for_session_and_uses_selected_one(self) -> None:
        sessions = [
            AgentSession("codex", "first-id", "Первая задача", 2.0),
            AgentSession("codex", "second-id", "Вторая задача", 1.0, "/tmp"),
        ]
        assistant = Assistant()
        with patch("sharp.assistant.commands.agent_sessions", return_value=sessions):
            question = assistant.begin_delegation("codex", "Исправь тесты")
        self.assertIn("1. Первая задача", question)
        self.assertIn("0. Новая сессия", question)

        with patch("sharp.assistant.commands.delegate_to_session", return_value="ok") as delegate:
            reply = assistant._finish_delegation("2")
        delegate.assert_called_once_with("codex", "Исправь тесты", "second-id", "/tmp")
        self.assertIn("Вторая задача", reply)

    def test_spoken_ordinal_selects_session(self) -> None:
        assistant = Assistant()
        assistant.pending_delegation = {
            "agent": "codex",
            "prompt": "Задача",
            "sessions": [AgentSession("codex", "one", "Один", 1.0)],
        }
        with patch("sharp.assistant.commands.delegate_to_session", return_value="ok") as delegate:
            assistant._finish_delegation("выбираю первую сессию")
        delegate.assert_called_once_with("codex", "Задача", "one", "")

    def test_yandex_voice_command_targets_yandex_handler(self) -> None:
        assistant = Assistant()
        with patch("sharp.assistant.commands.yandex_music", return_value="ok") as yandex:
            reply = assistant._quick_yandex("следующий трек в Яндекс Музыке", "следующий трек в яндекс музыке")
        yandex.assert_called_once_with("next")
        self.assertEqual(reply, "Следующий трек, сэр.")

    def test_yandex_context_keeps_followup_commands_on_yandex(self) -> None:
        assistant = Assistant()
        with patch("sharp.assistant.commands.yandex_music", return_value="ok") as yandex:
            assistant._quick_yandex("включи Мою волну", "включи мою волну")
            assistant._quick_yandex("а теперь сделай тише", "а теперь сделай тише")
        self.assertEqual(yandex.call_args_list[-1].args, ("volumedown",))

    def test_voice_session_list_can_ask_agent_as_followup(self) -> None:
        assistant = Assistant()
        question = assistant._voice_session_control("покажи список сессий")
        self.assertEqual(question, "Сессии Codex или Claude Code, сэр?")
        self.assertTrue(assistant.pending_session_list)

        with patch("sharp.assistant.commands.agent_sessions", return_value=[
            AgentSession("codex", "id", "Рабочая сессия", 1.0)
        ]):
            answer = assistant.process("Codex")
        self.assertIn("1. Рабочая сессия", answer)
        self.assertFalse(assistant.pending_session_list)

    def test_voice_delegation_phrase_starts_selection(self) -> None:
        assistant = Assistant()
        with patch("sharp.assistant.commands.agent_sessions", return_value=[
            AgentSession("codex", "id", "Sharp", 1.0)
        ]):
            answer = assistant._local_control(
                "передай Кодексу исправить визуализатор",
                "передай кодексу исправить визуализатор",
            )
        self.assertIn("В какую сессию Codex", answer)
        self.assertIsNotNone(assistant.pending_delegation)


class AgentSessionTests(unittest.TestCase):
    def test_codex_index_is_sorted_by_update_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session_index.jsonl"
            rows = [
                {"id": "old", "thread_name": "Старая", "updated_at": "2026-01-01T00:00:00Z"},
                {"id": "new", "thread_name": "Новая", "updated_at": "2026-02-01T00:00:00Z"},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows), "utf-8")
            sessions = list_codex_sessions(path)
        self.assertEqual([session.session_id for session in sessions], ["new", "old"])


class YandexMusicTests(unittest.TestCase):
    def test_next_targets_yandex_player(self) -> None:
        with (
            patch("sharp.commands._yandex_player", return_value="firefox.instance"),
            patch("sharp.commands._run", return_value="ok") as run,
        ):
            result = commands.yandex_music("next")
        self.assertEqual(result, "ok")
        run.assert_called_once_with(
            ["playerctl", "--player=firefox.instance", "next"]
        )

    def test_artist_opens_yandex_search(self) -> None:
        with patch("sharp.commands.open_url", return_value="ok") as open_url:
            result = commands.yandex_music("artist", "Кино")
        self.assertEqual(result, "ok")
        open_url.assert_called_once_with("https://music.yandex.ru/search?text=%D0%9A%D0%B8%D0%BD%D0%BE")


class LiveSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_audio_uses_low_latency_frames_and_small_jitter_buffer(self) -> None:
        self.assertEqual(IN_BLOCK / IN_RATE, 0.04)
        self.assertLessEqual(STREAM_PREBUFFER_MS, 100)

    async def test_live_config_keeps_server_default_turn_detection(self) -> None:
        cfg = build_live_config("system")

        self.assertIsNone(cfg.realtime_input_config)
        self.assertEqual(cfg.thinking_config.thinking_budget, 0)
        self.assertEqual(cfg.generation_config.max_output_tokens, LIVE_MAX_OUTPUT_TOKENS)
        self.assertGreaterEqual(LIVE_MAX_OUTPUT_TOKENS, 140)

    async def test_runtime_failure_is_reported_only_once(self) -> None:
        statuses: list[str] = []
        live = LiveSession(lambda _: None, lambda _: None, statuses.append)
        live._running = True

        live._fail("first")
        live._fail("second")

        self.assertEqual(statuses, ["first"])
        self.assertFalse(live._running)

    async def test_live_prompt_requires_wake_word_for_new_commands(self) -> None:
        self.assertIn("Если во входной реплике нет", LIVE_SYSTEM_PROMPT)
        self.assertIn("обращения", LIVE_SYSTEM_PROMPT)
        self.assertIn("не вызывай инструменты", LIVE_SYSTEM_PROMPT)
        self.assertIn("даже «молчу»", LIVE_SYSTEM_PROMPT)
        self.assertIn("«шарк»", LIVE_SYSTEM_PROMPT)
        self.assertIn("не обрывай речь", LIVE_SYSTEM_PROMPT)

    async def test_stop_joins_audio_workers(self) -> None:
        class Worker:
            def __init__(self) -> None:
                self.joined = False

            def join(self, timeout: float) -> None:
                self.joined = timeout == 2.0

        live = LiveSession(lambda _: None, lambda _: None, lambda _: None)
        live._mic_thread = Worker()
        live._play_thread = Worker()

        live.stop()

        self.assertTrue(live._mic_thread.joined)
        self.assertTrue(live._play_thread.joined)

    async def test_mic_is_temporarily_suspended_while_sharp_speaks(self) -> None:
        live = LiveSession(lambda _: None, lambda _: None, lambda _: None)
        live._running = True
        live.mic_on = True
        live._mic_q.put_nowait(b"queued-before-reply")

        live._begin_speaking()

        self.assertFalse(live._can_capture_mic())
        self.assertTrue(live.mic_on)  # пользовательское состояние не потеряно
        self.assertTrue(live._mic_q.empty())

        live._response_complete.set()
        live._end_speaking()

        self.assertTrue(live._can_capture_mic())

    async def test_auto_unmute_does_not_override_manual_mute(self) -> None:
        live = LiveSession(lambda _: None, lambda _: None, lambda _: None)
        live._running = True
        live.mic_on = False

        live._begin_speaking()
        live._end_speaking()

        self.assertFalse(live._can_capture_mic())
        self.assertFalse(live.mic_on)

    async def test_receiver_keeps_session_after_normal_turn_boundary(self) -> None:
        class EmptyResponse:
            tool_call = None
            data = None
            server_content = None

        class EmptyTurn:
            def __init__(self, response_count: int) -> None:
                self.response_count = response_count

            async def __aiter__(self):
                for _ in range(self.response_count):
                    yield EmptyResponse()

        class FakeSession:
            def __init__(self) -> None:
                self.calls = 0

            def receive(self) -> EmptyTurn:
                self.calls += 1
                if self.calls == 1:
                    return EmptyTurn(1)
                raise RuntimeError("stop")

        session = FakeSession()
        live = LiveSession(lambda _: None, lambda _: None, lambda _: None)
        live._running = True
        live._session = session
        live._stop_async = asyncio.Event()

        await live._receiver()

        self.assertEqual(session.calls, 2)
        self.assertTrue(live._stop_async.is_set())

    async def test_buffered_playback_waits_for_complete_turn(self) -> None:
        class Content:
            input_transcription = None
            output_transcription = None
            interrupted = False
            turn_complete = False

        class Response:
            tool_call = None

            def __init__(self, data: bytes | None, complete: bool = False) -> None:
                self.data = data
                self.server_content = Content()
                self.server_content.turn_complete = complete

        class Turn:
            async def __aiter__(self):
                yield Response(b"first")
                yield Response(b"second", complete=True)

        class Session:
            def __init__(self) -> None:
                self.called = False

            def receive(self):
                if self.called:
                    raise RuntimeError("stop")
                self.called = True
                return Turn()

        statuses: list[str] = []
        live = LiveSession(
            lambda _: None,
            lambda _: None,
            statuses.append,
            buffered_playback=True,
        )
        live._running = True
        live._session = Session()
        live._stop_async = asyncio.Event()

        await live._receiver()

        self.assertEqual(live._play_q.get_nowait(), b"firstsecond")
        self.assertTrue(live.speaking_event.is_set())
        self.assertTrue(any("загружен" in status for status in statuses))


class WakeWordTests(unittest.TestCase):
    def test_ambient_speech_is_ignored(self) -> None:
        self.assertIsNone(extract_command("включи музыку погромче"))

    def test_command_after_russian_wake_word(self) -> None:
        self.assertEqual(extract_command("Шарп, включи мою волну"), "включи мою волну")

    def test_stt_english_spelling_and_wake_at_end(self) -> None:
        self.assertEqual(extract_command("следующий трек, Sharp"), "следующий трек")

    def test_name_alone_wakes_assistant(self) -> None:
        self.assertEqual(extract_command("Шарп!"), "")

    def test_common_stt_variant_is_accepted_as_whole_word(self) -> None:
        self.assertEqual(extract_command("Шар включи музыку"), "включи музыку")
        self.assertEqual(extract_command("Шарк открой браузер"), "открой браузер")
        self.assertIsNone(extract_command("возьми шарик"))


class SpeechRecognitionTests(unittest.TestCase):
    def test_google_alternatives_are_preserved_for_wake_matching(self) -> None:
        result = {
            "alternative": [
                {"transcript": "включи музыку", "confidence": 0.8},
                {"transcript": "шарп включи музыку", "confidence": 0.7},
            ]
        }
        self.assertEqual(
            _transcripts_from_google(result),
            ["включи музыку", "шарп включи музыку"],
        )

    def test_wake_capture_window_is_short(self) -> None:
        from sharp.stt import MAX_WAKE_SECONDS

        self.assertLessEqual(MAX_WAKE_SECONDS, 1.0)

    def test_capture_states_are_reported_before_recognition_result(self) -> None:
        states: list[str] = []

        def fake_record(**kwargs) -> bytes:
            kwargs["on_speech_start"]()
            return b"\0" * 960

        with (
            patch("sharp.stt.record_until_silence", side_effect=fake_record),
            patch(
                "speech_recognition.Recognizer.recognize_google",
                return_value={"alternative": [{"transcript": "шарп"}]},
            ),
        ):
            result = listen_candidates(on_state=states.append)

        self.assertEqual(states, ["HEARING", "RECOGNIZING"])
        self.assertEqual(result, ["шарп"])


class NetworkProbeTests(unittest.TestCase):
    def test_250_ms_is_not_considered_fast(self) -> None:
        from sharp.live import NET_PROBE_LIMIT_MS

        self.assertLess(NET_PROBE_LIMIT_MS, 250.0)

    def test_probe_limit_is_strict(self) -> None:
        from sharp.live import NET_PROBE_LIMIT_MS

        self.assertEqual(NET_PROBE_LIMIT_MS, 200.0)


if __name__ == "__main__":
    unittest.main()
