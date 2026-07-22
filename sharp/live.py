"""Реалтайм голос↔голос через Gemini Live API — один websocket вместо STT→chat→TTS.

Убирает задержку старой цепочки (Google STT → gemini.chat → gemini.synth): здесь
микрофон стримится прямо в модель, а её голос приходит потоком обратно. Серверный
VAD сам определяет конец фразы, поэтому «сказал → услышал» почти мгновенно.

LiveSession крутит собственный asyncio-loop в фоновом потоке (Textual занимает свой),
и общается с TUI через колбэки on_user_text / on_sharp_text / on_status.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Callable
from contextlib import suppress

import numpy as np
from google import genai
from google.genai import types

from . import audio, memory, tools
from .config import CFG

# Модель нативного аудио-диалога (проверено: коннектится и стримит голос по ключу).
LIVE_MODEL = "gemini-2.5-flash-native-audio-latest"

# Форматы PCM: вход в модель — 16кГц, выход из модели — 24кГц, оба mono s16le.
IN_RATE = 16000
OUT_RATE = 24000
IN_BLOCK = 320           # 20 мс: быстрее доходит до серверного VAD
OUT_BLOCK = 1024
STREAM_PREBUFFER_MS = 90
LIVE_MAX_OUTPUT_TOKENS = 80

# Для разговорного стриминга даже четверть секунды на TLS handshake уже много.
# При 200+ мс Live остаётся активным, но ответ сначала загружается целиком.
NET_PROBE_LIMIT_MS = 200.0
# Сколько «спотыканий» (пустая очередь ≥0.3с посреди ответа) терпим в одном ответе.
STALL_LIMIT = 3


def build_live_config(system_instruction: str) -> types.LiveConnectConfig:
    """Fast voice profile with SDK-default Live turn detection."""
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        generation_config=types.GenerationConfig(
            max_output_tokens=LIVE_MAX_OUTPUT_TOKENS,
            temperature=0.35,
        ),
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        system_instruction=system_instruction,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=[tools.gemini_tool()],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=CFG.voice)
            )
        ),
    )


def network_ok(limit_ms: float = NET_PROBE_LIMIT_MS) -> tuple[bool, float]:
    """Быстрая проба: потянет ли сеть реалтайм-аудио. Возвращает (ок, время коннекта в мс).

    Меряем TCP+TLS-хендшейк до эндпоинта Gemini (2 попытки, берём лучшую). На хорошем
    интернете это до 200 мс, на слабом/мобильном — дольше. Ошибка сети = сразу не ок.
    """
    import socket
    import ssl
    import time as _t

    host = "generativelanguage.googleapis.com"
    ctx = ssl.create_default_context()
    best: float | None = None
    for _ in range(2):
        t0 = _t.monotonic()
        try:
            with (
                socket.create_connection((host, 443), timeout=2.0) as sock,
                ctx.wrap_socket(sock, server_hostname=host),
            ):
                pass
        except OSError:
            continue
        ms = (_t.monotonic() - t0) * 1000.0
        best = ms if best is None else min(best, ms)
    if best is None:
        return False, float("inf")
    return best < limit_ms, best


class LiveSession:
    """Живая голосовая сессия. start()/stop()/toggle_mic()/send_text() — потокобезопасны."""

    def __init__(
        self,
        on_user_text: Callable[[str], None],
        on_sharp_text: Callable[[str], None],
        on_status: Callable[[str], None],
        *,
        capture_audio: bool = True,
        buffered_playback: bool = False,
    ) -> None:
        self.on_user_text = on_user_text
        self.on_sharp_text = on_sharp_text
        self.on_status = on_status
        self.capture_audio = capture_audio
        self.buffered_playback = buffered_playback

        self.mic_on = True
        self._running = False
        # Пользовательское состояние микрофона (mic_on) не трогаем во время
        # ответа. Отдельный флаг временно блокирует захват, пока играет голос
        # Sharp, а затем автоматически возвращает прежнее состояние.
        self._speaking = threading.Event()
        self._response_complete = threading.Event()
        self._playback_started = threading.Event()
        self._failure_reported = threading.Event()
        self._last_voice_at = 0.0
        self._first_audio_reported = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_async: asyncio.Event | None = None
        self._session = None
        self._thread: threading.Thread | None = None
        self._play_thread: threading.Thread | None = None
        self._mic_thread: threading.Thread | None = None

        # очереди PCM: микрофон → модель, модель → динамики
        self._mic_q: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self._play_q: queue.Queue[bytes | None] = queue.Queue(maxsize=96)
        # накопители транскрипций (флашим по границам реплик)
        self._in_buf = ""
        self._out_buf = ""

    # --- управление из TUI (главный поток) ---
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._put_stop_marker(self._mic_q)
        self._put_stop_marker(self._play_q)
        if self._stop_async:
            if self._loop:
                self._loop.call_soon_threadsafe(self._stop_async.set)
            else:
                self._stop_async.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        for worker in (self._mic_thread, self._play_thread):
            if worker and worker is not threading.current_thread():
                worker.join(timeout=2.0)

    @staticmethod
    def _put_stop_marker(target: queue.Queue) -> None:
        try:
            target.put_nowait(None)
        except queue.Full:
            try:
                target.get_nowait()
                target.put_nowait(None)
            except queue.Empty:
                pass

    def toggle_mic(self) -> bool:
        self.mic_on = not self.mic_on
        return self.mic_on

    def _can_capture_mic(self) -> bool:
        return self._running and self.mic_on and not self._speaking.is_set()

    @property
    def speaking_event(self) -> threading.Event:
        """Event used by the wake listener to avoid recognizing speaker echo."""
        return self._speaking

    def _begin_speaking(self) -> None:
        """Временно заглушить микрофон и выбросить ещё не отправленное эхо."""
        if not self._speaking.is_set():
            self._response_complete.clear()
            self._speaking.set()
            self._drain_mic_q()

    def _end_speaking(self) -> None:
        """Вернуть микрофон в выбранное пользователем состояние."""
        self._speaking.clear()
        self._response_complete.clear()
        self._playback_started.clear()
        self._first_audio_reported = False

    def _fail(self, message: str) -> None:
        """Остановить все части Live после первой зафиксированной ошибки."""
        if not self._running or self._failure_reported.is_set():
            return
        self._failure_reported.set()
        self.on_status(message)
        self._running = False
        self._put_stop_marker(self._mic_q)
        self._put_stop_marker(self._play_q)
        if self._stop_async:
            if self._loop:
                self._loop.call_soon_threadsafe(self._stop_async.set)
            else:
                self._stop_async.set()

    def send_text(self, text: str) -> bool:
        """Отправить набранный текст в ту же live-сессию (потокобезопасно)."""
        if not (self._running and self._loop and self._session):
            return False

        async def _send():
            await self._session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True,
            )

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except RuntimeError:
            return False
        return True

    # --- фоновый поток с собственным event-loop ---
    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as e:  # noqa: BLE001
            self.on_status(f"Live-сессия завершилась: {str(e)[:100]}")
        finally:
            self._running = False
            self._session = None
            self._loop = None
            self._stop_async = None

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_async = asyncio.Event()
        client = genai.Client(api_key=CFG.require_key())
        cfg = build_live_config(tools.LIVE_SYSTEM_PROMPT + memory.as_prompt())
        async with client.aio.live.connect(model=CFG.live_model, config=cfg) as session:
            self._session = session
            self.on_status(f"Live-режим активен ({CFG.voice}). Говорите, сэр.")
            # воспроизведение — в отдельном потоке (blocking sounddevice)
            self._play_thread = threading.Thread(target=self._player, daemon=True)
            self._play_thread.start()
            # В TUI вход сначала проходит локальный wake-word фильтр. Прямой
            # аудиозахват оставлен для программного использования LiveSession.
            if self.capture_audio:
                self._mic_thread = threading.Thread(target=self._mic_reader, daemon=True)
                self._mic_thread.start()
            sender = asyncio.create_task(self._sender())
            receiver = asyncio.create_task(self._receiver())
            try:
                await self._stop_async.wait()
            finally:
                sender.cancel()
                receiver.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)
                self._session = None
                self._put_stop_marker(self._play_q)

    # --- микрофон: sounddevice-поток пишет PCM в очередь ---
    def _mic_reader(self) -> None:
        import sounddevice as sd

        def cb(indata, frames, time, status):  # noqa: ANN001, ARG001
            if not self._can_capture_mic():
                return
            rms = float(np.sqrt(np.mean(indata[:, 0] ** 2)))
            if rms >= 0.01:
                self._last_voice_at = time.monotonic()
            pcm = (np.clip(indata[:, 0], -1, 1) * 32767).astype(np.int16).tobytes()
            with suppress(queue.Full):
                self._mic_q.put_nowait(pcm)

        try:
            with sd.InputStream(samplerate=IN_RATE, channels=1, dtype="float32",
                                blocksize=IN_BLOCK, callback=cb):
                self.on_status("Live-микрофон открыт.")
                while self._running:
                    sd.sleep(100)
        except Exception as e:  # noqa: BLE001
            self._fail(f"Микрофон недоступен: {str(e)[:80]}")

    async def _sender(self) -> None:
        """Достаём кадры микрофона из очереди и шлём в модель."""
        while self._running:
            try:
                pcm = await asyncio.to_thread(self._mic_q.get, True, 0.5)
            except queue.Empty:
                continue
            if pcm is None:
                break
            # Фрейм мог попасть в очередь непосредственно перед началом ответа.
            if self._speaking.is_set():
                continue
            if not self._session:
                continue
            try:
                await self._session.send_realtime_input(
                    audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={IN_RATE}")
                )
            except Exception as error:  # noqa: BLE001
                self._fail(f"Обрыв отправки Live: {str(error)[:80]}")
                break
        if self._stop_async:
            self._stop_async.set()

    async def _receiver(self) -> None:
        """Читаем ответы: аудио → на воспроизведение, транскрипции → в ленту."""
        while self._running and self._session:
            buffered_audio: list[bytes] = []
            buffering_announced = False
            try:
                turn = self._session.receive()
                async for r in turn:
                    if not self._running:
                        break
                    if r.tool_call:
                        await self._handle_tool_call(r.tool_call)
                    if r.data:
                        if not self._first_audio_reported:
                            self._first_audio_reported = True
                            if self._last_voice_at > 0:
                                delay_ms = (time.monotonic() - self._last_voice_at) * 1000
                                self.on_status(f"LATENCY first_audio={delay_ms:.0f}ms")
                        if self.buffered_playback:
                            buffered_audio.append(r.data)
                            if not buffering_announced:
                                self.on_status("Слабая сеть: загружаю голосовой ответ целиком…")
                                buffering_announced = True
                        else:
                            self._queue_audio(r.data)
                    sc = r.server_content
                    if not sc:
                        continue
                    if sc.input_transcription and sc.input_transcription.text:
                        self._in_buf += sc.input_transcription.text
                    if sc.output_transcription and sc.output_transcription.text:
                        self._out_buf += sc.output_transcription.text
                    if sc.interrupted:
                        self._drain_play_q()          # barge-in: сброс недоигранного
                        buffered_audio.clear()
                        self._response_complete.set()
                    if sc.turn_complete:
                        if buffered_audio:
                            self._queue_audio(b"".join(buffered_audio))
                            buffered_audio.clear()
                            self.on_status("Голосовой ответ загружен — воспроизвожу.")
                        self._response_complete.set()
                        self._flush_transcripts()
            except Exception as e:  # noqa: BLE001
                self._fail(f"Обрыв Live: {str(e)[:80]}")
                break

    def _queue_audio(self, chunk: bytes) -> None:
        """Queue PCM for playback, dropping stale audio only as a last resort."""
        self._begin_speaking()
        try:
            self._play_q.put_nowait(chunk)
        except queue.Full:
            self._drain_play_q()
            self._play_q.put_nowait(chunk)

    async def _handle_tool_call(self, tool_call) -> None:
        """Gemini попросил вызвать инструмент(ы): выполняем и шлём результаты назад."""
        responses = []
        for fc in tool_call.function_calls:
            args = dict(fc.args) if fc.args else {}
            shown = ", ".join(f"{k}={v}" for k, v in args.items())
            self.on_status(f"⚙ {fc.name}({shown})")
            result = await asyncio.to_thread(tools.execute, fc.name, args)
            responses.append(
                types.FunctionResponse(id=fc.id, name=fc.name, response=result)
            )
        if responses and self._session:
            with suppress(Exception):  # noqa: BLE001
                await self._session.send_tool_response(function_responses=responses)

    def _flush_transcripts(self) -> None:
        if self._in_buf.strip():
            self.on_user_text(self._in_buf.strip())
        if self._out_buf.strip():
            self.on_sharp_text(self._out_buf.strip())
        self._in_buf = ""
        self._out_buf = ""

    def _drain_play_q(self) -> None:
        try:
            while True:
                self._play_q.get_nowait()
        except queue.Empty:
            pass

    def _drain_mic_q(self) -> None:
        try:
            while True:
                self._mic_q.get_nowait()
        except queue.Empty:
            pass

    # --- воспроизведение ответа + кормёжка визуализатора ---
    def _player(self) -> None:
        import sounddevice as sd

        try:
            stream = sd.OutputStream(samplerate=OUT_RATE, channels=1, dtype="float32",
                                     blocksize=OUT_BLOCK)
            stream.start()
        except Exception as e:  # noqa: BLE001
            self._fail(f"Вывод звука недоступен: {str(e)[:80]}")
            return

        stalls = 0  # «спотыкания»: очередь пуста посреди ответа = сеть не успевает
        while self._running:
            try:
                chunk = self._play_q.get(timeout=0.3)
            except queue.Empty:
                # turn_complete приходит после последнего аудиофрагмента, но
                # ждём ещё и фактического опустошения очереди воспроизведения.
                # Таймаут даёт короткий запас против акустического хвоста.
                if self._speaking.is_set() and self._response_complete.is_set():
                    self._end_speaking()
                    stalls = 0
                elif self._speaking.is_set() and not self.buffered_playback:
                    # ответ ещё идёт, а играть нечего — аудио застряло в сети
                    stalls += 1
                    if stalls >= STALL_LIMIT:
                        # Не рвём сессию. Остаток этого ответа и все следующие
                        # receiver соберёт целиком перед воспроизведением.
                        self.buffered_playback = True
                        self.on_status("Сеть просела: включаю загрузку ответа целиком.")
                        stalls = 0
                if audio.is_playing():
                    audio._playing.clear()
                    audio._push_levels(np.zeros(audio.NUM_BANDS, dtype=np.float32))
                continue
            if chunk is None:
                break
            stalls = 0
            chunks = [chunk]
            # Первый звук придерживаем максимум на 240 мс: этого достаточно,
            # чтобы сгладить сетевой jitter, но короткий завершённый ответ идёт сразу.
            if not self.buffered_playback and not self._playback_started.is_set():
                target_bytes = int(OUT_RATE * 2 * STREAM_PREBUFFER_MS / 1000)
                buffered_bytes = len(chunk)
                deadline = time.monotonic() + STREAM_PREBUFFER_MS / 1000
                while (
                    buffered_bytes < target_bytes
                    and not self._response_complete.is_set()
                    and time.monotonic() < deadline
                    and self._running
                ):
                    try:
                        extra = self._play_q.get(timeout=0.04)
                    except queue.Empty:
                        continue
                    if extra is None:
                        self._running = False
                        break
                    chunks.append(extra)
                    buffered_bytes += len(extra)
                self._playback_started.set()
            audio._playing.set()
            try:
                # Пишем по блокам: большой буфер воспроизводится плавно, а
                # визуализатор остаётся синхронным со звуком.
                for pcm in chunks:
                    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                    for i in range(0, samples.size, OUT_BLOCK):
                        block = samples[i : i + OUT_BLOCK]
                        audio._push_levels(audio._bands_from_block(block))
                        stream.write(block.reshape(-1, 1))
            except Exception as error:  # noqa: BLE001
                self._fail(f"Ошибка воспроизведения Live: {str(error)[:80]}")
                break

        audio._playing.clear()
        self._end_speaking()
        audio._push_levels(np.zeros(audio.NUM_BANDS, dtype=np.float32))
        with suppress(Exception):  # noqa: BLE001
            stream.stop()
            stream.close()
