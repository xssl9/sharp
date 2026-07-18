"""Поиск сохранённых интерактивных сессий Codex и Claude Code."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class AgentSession:
    agent: str
    session_id: str
    title: str
    updated_at: float
    cwd: str = ""


def _short(text: str, limit: int = 56) -> str:
    clean = " ".join(text.split())
    return clean[: limit - 1] + "…" if len(clean) > limit else clean


def list_codex_sessions(path: Path | None = None) -> list[AgentSession]:
    path = path or Path.home() / ".codex" / "session_index.jsonl"
    sessions: list[AgentSession] = []
    try:
        lines = path.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
            stamp = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).timestamp()
            sessions.append(
                AgentSession("codex", item["id"], _short(item.get("thread_name") or "Без названия"), stamp)
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return sorted(sessions, key=lambda session: session.updated_at, reverse=True)


def list_claude_sessions(root: Path | None = None) -> list[AgentSession]:
    root = root or Path.home() / ".claude" / "projects"
    sessions: list[AgentSession] = []
    for path in root.glob("*/*.jsonl") if root.exists() else ():
        session_id = path.stem
        title = "Без названия"
        cwd = ""
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    session_id = item.get("sessionId") or session_id
                    cwd = item.get("cwd") or cwd
                    if item.get("type") != "user" or item.get("isMeta"):
                        continue
                    content = item.get("message", {}).get("content")
                    if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
                        title = _short(content)
                        break
            sessions.append(AgentSession("claude", session_id, title, path.stat().st_mtime, cwd))
        except OSError:
            continue
    return sorted(sessions, key=lambda session: session.updated_at, reverse=True)


def list_sessions(agent: str, limit: int = 8) -> list[AgentSession]:
    found = list_codex_sessions() if agent == "codex" else list_claude_sessions()
    return found[:limit]
