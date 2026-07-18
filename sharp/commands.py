"""Системные команды под Linux — порт логики из старого assistant.js (Windows).

Медиа → playerctl, громкость → pactl, открытие URL/приложений → xdg-open/exec,
Steam-игры → xdg-open steam://rungameid/ID. Работа с файлами — на pathlib.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .agent_sessions import AgentSession, list_sessions

# Название игры (рус/англ) -> Steam App ID. 0 = не в Steam.
STEAM_GAMES: dict[str, int] = {
    "cs": 730, "кс": 730, "counter-strike": 730, "cs2": 730, "контру": 730, "csgo": 730,
    "dota": 570, "дота": 570, "dota 2": 570,
    "pubg": 578080, "пабг": 578080,
    "rust": 252490, "раст": 252490,
    "gta": 271590, "гта": 271590, "gta 5": 271590, "gta v": 271590,
    "terraria": 105600, "террария": 105600,
    "apex": 1172470, "апекс": 1172470,
    "elden ring": 1245620, "элден ринг": 1245620,
    "cyberpunk": 1091500, "киберпанк": 1091500,
    "witcher": 292030, "ведьмак": 292030, "witcher 3": 292030,
    "skyrim": 489830, "скайрим": 489830,
    "hollow knight": 367520, "холлоу найт": 367520,
    "hades": 1145360, "хейдес": 1145360,
    "baldurs gate": 1086940, "bg3": 1086940, "балдурс гейт": 1086940,
    "factorio": 427520, "факторио": 427520,
    "stardew valley": 413150, "стардью": 413150,
}

SITES: dict[str, str] = {
    "яндекс музык": "music.yandex.ru", "yandex music": "music.yandex.ru",
    "youtube music": "music.youtube.com", "ютуб музык": "music.youtube.com",
    "youtube": "youtube.com", "ютуб": "youtube.com",
    "google": "google.com", "гугл": "google.com",
    "github": "github.com", "гитхаб": "github.com",
    "telegram": "web.telegram.org", "телеграм": "web.telegram.org", "телега": "web.telegram.org",
    "twitch": "twitch.tv", "твич": "twitch.tv",
    "discord": "discord.com", "дискорд": "discord.com",
    "spotify": "open.spotify.com", "спотифай": "open.spotify.com",
}

# Псевдонимы приложений -> исполняемый файл в Linux
APPS: dict[str, str] = {
    "браузер": "firefox", "chrome": "google-chrome-stable", "хром": "google-chrome-stable",
    "google chrome": "google-chrome-stable", "гугл хром": "google-chrome-stable",
    "firefox": "firefox", "файрфокс": "firefox",
    "терминал": "kitty", "terminal": "kitty",
    "код": "code", "vscode": "code", "vs code": "code", "visual studio code": "code",
    "калькулятор": "kcalc", "calculator": "kcalc",
    "проводник": "dolphin", "файлы": "dolphin", "файловый менеджер": "dolphin",
    "телеграм": "telegram-desktop", "telegram": "telegram-desktop",
    "дискорд": "discord", "discord": "discord",
}


def _run(cmd: list[str]) -> str:
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "ok"
    except FileNotFoundError:
        return f"нет команды: {cmd[0]}"
    except Exception as e:  # noqa: BLE001
        return str(e)


# --- Медиа (playerctl) ---
def media(action: str) -> str:
    mapping = {
        "play": ["playerctl", "play"],
        "pause": ["playerctl", "pause"],
        "playpause": ["playerctl", "play-pause"],
        "next": ["playerctl", "next"],
        "prev": ["playerctl", "previous"],
        "previous": ["playerctl", "previous"],
    }
    if action in ("volumeup", "volumedown", "mute"):
        return volume(action)
    cmd = mapping.get(action)
    if not cmd:
        return f"неизвестное действие: {action}"
    return _run(cmd)


def _player_names() -> list[str]:
    try:
        result = subprocess.run(
            ["playerctl", "--list-all"], capture_output=True, text=True, timeout=2, check=False
        )
        return [name.strip() for name in result.stdout.splitlines() if name.strip()]
    except (FileNotFoundError, subprocess.SubprocessError):
        return []


def _yandex_player() -> str | None:
    """Найти MPRIS-плеер вкладки/приложения Яндекс Музыки."""
    for name in _player_names():
        if "yandex" in name.lower():
            return name
        try:
            result = subprocess.run(
                ["playerctl", f"--player={name}", "metadata", "xesam:url"],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except subprocess.SubprocessError:
            continue
        if "music.yandex." in result.stdout.lower():
            return name
    return None


def yandex_music(action: str, query: str = "") -> str:
    """Управлять именно Яндекс Музыкой через MPRIS или открыть нужный раздел."""
    from urllib.parse import quote_plus

    action = action.lower().strip()
    if action == "wave":
        player = _yandex_player()
        if player:
            return _run(["playerctl", f"--player={player}", "play"])
        return open_url("https://music.yandex.ru/home")
    if action in ("artist", "search"):
        if not query.strip():
            return "не указан исполнитель"
        return open_url(f"https://music.yandex.ru/search?text={quote_plus(query.strip())}")

    player = _yandex_player()
    if not player:
        open_url("https://music.yandex.ru/home")
        return "Яндекс Музыка открыта; повторите команду после запуска плеера"
    mapping = {
        "play": "play", "pause": "pause", "playpause": "play-pause",
        "next": "next", "prev": "previous", "previous": "previous",
    }
    if action in mapping:
        return _run(["playerctl", f"--player={player}", mapping[action]])
    if action in ("up", "volumeup"):
        return _run(["playerctl", f"--player={player}", "volume", "0.05+"])
    if action in ("down", "volumedown"):
        return _run(["playerctl", f"--player={player}", "volume", "0.05-"])
    return f"неизвестная команда Яндекс Музыки: {action}"


# --- Громкость (pactl) ---
def volume(action: str) -> str:
    sink = "@DEFAULT_SINK@"
    if action in ("volumeup", "up"):
        return _run(["pactl", "set-sink-volume", sink, "+5%"])
    if action in ("volumedown", "down"):
        return _run(["pactl", "set-sink-volume", sink, "-5%"])
    if action in ("mute",):
        return _run(["pactl", "set-sink-mute", sink, "toggle"])
    return f"неизвестное действие громкости: {action}"


# --- Открытие URL / приложений / Steam ---
def open_url(url: str) -> str:
    if not (url.startswith("http") or url.startswith("steam://")):
        url = "https://" + url
    return _run(["xdg-open", url])


def open_app(name: str, arguments: str = "") -> str:
    """Open a GUI application without granting arbitrary shell access."""
    key = name.lower().strip()
    exe = APPS.get(key, key)
    if not exe:
        return "не указана программа"
    if not shutil.which(exe):
        return f"программа не установлена: {exe}"
    try:
        args = shlex.split(arguments) if arguments.strip() else []
    except ValueError as error:
        return f"не разобрал аргументы: {error}"
    return _run([exe, *args])


def open_steam(app_id: int | str) -> str:
    return _run(["xdg-open", f"steam://rungameid/{app_id}"])


def search(query: str) -> str:
    from urllib.parse import quote_plus

    return open_url(f"google.com/search?q={quote_plus(query)}")


# --- Файлы (pathlib, кроссплатформенно) ---
def _expand(p: str) -> Path:
    return Path(p).expanduser()


def read_file(path: str, limit: int = 50_000) -> str:
    fp = _expand(path)
    if not fp.exists():
        return f"Файл не найден: {fp}"
    if fp.stat().st_size > limit:
        return f"Файл слишком большой ({fp.stat().st_size // 1024}KB)."
    return fp.read_text(encoding="utf-8", errors="replace")[:limit]


def list_dir(path: str, limit: int = 50) -> str:
    dp = _expand(path)
    if not dp.exists():
        return f"Папка не найдена: {dp}"
    items = sorted(dp.iterdir())[:limit]
    return "\n".join(
        f"{'📁' if it.is_dir() else '📄'} {it.name}" for it in items
    ) or "Папка пуста"


def create_file(path: str, content: str) -> str:
    fp = _expand(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return f"Файл создан: {fp}"


# --- Универсальный запуск команды в системе (term:команда) ---
# Gemini может сам сформировать любую команду Linux, чтобы открыть программу/файл.
# Разбираем строку в список аргументов через shlex (без shell=True — не даём инъекций
# и подстановок вроде `$(...)`, `;`, `&&`). Поддерживаем ~ и переменные окружения.

# Опасные операции блокируем: пользователь голосом не должен случайно снести систему.
_BLOCKED = (
    "rm ", "rm\t", "mkfs", "dd ", ":(){", "shutdown", "reboot", "poweroff",
    "> /dev", "chmod -r 000", "chown -r", "mv / ", "> /etc",
)

_MAX_TERMINAL_OUTPUT = 8_000


def _command_is_blocked(command: str) -> bool:
    low = " ".join(command.lower().split())
    if any(blocked in low for blocked in _BLOCKED):
        return True
    # Установку делаем только отдельной функцией с проверенным именем пакета.
    return bool(re.search(r"(?:^|[;&|]\s*)(?:sudo\s+)?pacman\s+-\s*s\b", low))


def run_terminal(command: str, timeout: int = 30) -> str:
    """Run a terminal command and return exit code plus bounded stdout/stderr."""
    from .config import CFG

    cmd = command.strip()
    if not cmd:
        return "пустая команда"
    if not CFG.allow_shell_commands:
        return "терминальные команды отключены в config.toml"
    if _command_is_blocked(cmd):
        return "команда заблокирована; для установки используйте install_package"
    try:
        result = subprocess.run(
            ["bash", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=max(1, min(timeout, 120)),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        values = []
        for value in (error.stdout, error.stderr):
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if value:
                values.append(value)
        output = "\n".join(values)
        return f"тайм-аут команды\n{output[-_MAX_TERMINAL_OUTPUT:]}".strip()
    except Exception as error:  # noqa: BLE001
        return f"ошибка запуска: {error}"

    sections = [f"exit_code={result.returncode}"]
    if result.stdout.strip():
        sections.append("stdout:\n" + result.stdout.strip())
    if result.stderr.strip():
        sections.append("stderr:\n" + result.stderr.strip())
    return "\n".join(sections)[-_MAX_TERMINAL_OUTPUT:]


_PACKAGE_RE = re.compile(r"^[A-Za-z0-9@._+:-]+$")
_install_lock = threading.Lock()
_pending_install: tuple[str, float] | None = None


def prepare_package_install(package: str) -> str:
    """Remember a proposed package; a separate voice turn must confirm it."""
    global _pending_install
    name = package.strip()
    if not _PACKAGE_RE.fullmatch(name):
        return "некорректное имя пакета"
    with _install_lock:
        _pending_install = (name, time.monotonic() + 120.0)
    return f"нужно подтверждение: спросите пользователя, установить пакет {name}?"


def install_package(package: str) -> str:
    """Open pacman installation in a visible terminal after strict name validation."""
    global _pending_install
    name = package.strip()
    if not _PACKAGE_RE.fullmatch(name):
        return "некорректное имя пакета"
    with _install_lock:
        pending = _pending_install
        _pending_install = None
    if not pending or pending[0] != name or pending[1] < time.monotonic():
        return "установка не подтверждена; сначала вызовите prepare_package_install"
    if not shutil.which("pacman"):
        return "pacman не найден"
    term = _term()
    if not term:
        return "не найден терминал (kitty/alacritty/konsole)"
    result = _run_in_terminal(term + ["sudo", "pacman", "-S", "--needed", name])
    if result != "ok":
        return result
    return f"открыт терминал установки {name}; pacman покажет вывод и запросит подтверждение"


def run_shell(command: str) -> str:
    """Backward-compatible alias for terminal command execution."""
    return run_terminal(command)


# --- Делегирование промптов внешним AI-агентам ---
# Открываем новый терминал (kitty) с запущенным агентом и переданным промптом,
# либо отправляем задачу в уже открытый VS Code.

def _term() -> list[str]:
    """Команда терминала-обёртки: kitty > alacritty > konsole."""
    for term, prefix in (
        ("kitty", ["kitty", "--hold", "-e"]),
        ("alacritty", ["alacritty", "--hold", "-e"]),
        ("konsole", ["konsole", "--hold", "-e"]),
    ):
        if shutil.which(term):
            return prefix
    return []


def ask_claude(prompt: str, cwd: str | None = None) -> str:
    """Запустить Claude Code в новом окне терминала с этим промптом."""
    import shutil
    if not shutil.which("claude"):
        return "нет claude в PATH"
    term = _term()
    if not term:
        return "не найден терминал (kitty/alacritty/konsole)"
    try:
        subprocess.Popen(
            term + ["claude", prompt],
            cwd=_expand(cwd) if cwd else None,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return "ok"
    except Exception as e:  # noqa: BLE001
        return str(e)


def ask_codex(prompt: str, cwd: str | None = None) -> str:
    """Запустить Codex CLI в новом окне терминала с этим промптом."""
    import shutil
    if not shutil.which("codex"):
        return "нет codex в PATH"
    term = _term()
    if not term:
        return "не найден терминал (kitty/alacritty/konsole)"
    try:
        subprocess.Popen(
            term + ["codex", prompt],
            cwd=_expand(cwd) if cwd else None,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return "ok"
    except Exception as e:  # noqa: BLE001
        return str(e)


def normalize_agent(target: str) -> str | None:
    target = target.lower().strip()
    if target in ("claude", "claude code", "клод", "клоду", "клауд", "клауду", "клауде", "клоуд"):
        return "claude"
    if target in ("codex", "кодекс", "кодексу", "кодэкс", "codo"):
        return "codex"
    return None


def agent_sessions(target: str, limit: int = 8) -> list[AgentSession]:
    agent = normalize_agent(target)
    return list_sessions(agent, limit) if agent else []


def delegate_to_session(
    target: str,
    prompt: str,
    session_id: str | None = None,
    cwd: str | None = None,
) -> str:
    """Создать сессию агента или продолжить выбранную в новом терминале."""
    agent = normalize_agent(target)
    if not agent:
        return f"неизвестный агент: {target}"
    if not session_id:
        return ask_codex(prompt, cwd) if agent == "codex" else ask_claude(prompt, cwd)
    executable = "codex" if agent == "codex" else "claude"
    import shutil
    if not shutil.which(executable):
        return f"нет {executable} в PATH"
    term = _term()
    if not term:
        return "не найден терминал (kitty/alacritty/konsole)"
    args = (["codex", "resume", session_id, prompt] if agent == "codex"
            else ["claude", "--resume", session_id, prompt])
    return _run_in_terminal(term + args, cwd)


def _run_in_terminal(args: list[str], cwd: str | None = None) -> str:
    try:
        subprocess.Popen(
            args,
            cwd=_expand(cwd) if cwd else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return "ok"
    except Exception as e:  # noqa: BLE001
        return str(e)


def open_vscode(target: str | None = None) -> str:
    """Открыть VS Code (папку/файл). Сам промпт кладём в буфер обмена для вставки в чат."""
    import shutil
    if not shutil.which("code"):
        return "нет code в PATH"
    args = ["code"]
    if target:
        args.append(str(_expand(target)))
    return _run(args)


def clipboard_set(text: str) -> str:
    """Положить текст в буфер обмена (wl-copy для Wayland, xclip для X11)."""
    import shutil
    if shutil.which("wl-copy"):
        cmd = ["wl-copy"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard"]
    else:
        return "нет wl-copy/xclip"
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        p.communicate(text.encode("utf-8"))
        return "ok"
    except Exception as e:  # noqa: BLE001
        return str(e)


# Маршрутизатор: цель -> функция. Используется префиксом AGENT:target|prompt.
def delegate(target: str, prompt: str, cwd: str | None = None) -> str:
    target = target.lower().strip()
    if normalize_agent(target) == "claude":
        return ask_claude(prompt, cwd)
    if normalize_agent(target) == "codex":
        return ask_codex(prompt, cwd)
    if target in ("vscode", "vs code", "code", "вскод", "код"):
        # промпт в буфер + открыть редактор
        clipboard_set(prompt)
        return open_vscode(cwd)
    return f"неизвестный агент: {target}"
