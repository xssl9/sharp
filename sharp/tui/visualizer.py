"""Cava-подобный визуализатор для Textual.

Читает уровни полос из audio.levels_queue и рисует вертикальные бары символами
▁▂▃▄▅▆▇█. Как в cava, вертикальные полосы разделены пустым столбцом и растут снизу.
"""
from __future__ import annotations

import math

import numpy as np
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

from .. import audio

BLOCKS = " ▁▂▃▄▅▆▇█"
BAR_STYLE = Style(color="#ffffff")


class Visualizer(Widget):
    DEFAULT_CSS = """
    Visualizer {
        height: 1fr;
        min-height: 6;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._levels = np.zeros(audio.NUM_BANDS, dtype=np.float32)
        self._phase = 0.0

    def on_mount(self) -> None:
        # ~30 fps
        self.set_interval(1 / 30, self._tick)

    def _tick(self) -> None:
        # забираем самые свежие уровни из очереди
        latest = None
        while not audio.levels_queue.empty():
            try:
                latest = audio.levels_queue.get_nowait()
            except Exception:  # noqa: BLE001
                break

        self._phase += 0.15
        if latest is not None:
            self._levels = latest
        elif not audio.is_playing():
            # Тихая симметричная волна сохраняет характер cava в ожидании.
            idle = 0.035 + 0.035 * (
                np.sin(np.linspace(0, math.pi * 2, audio.NUM_BANDS) + self._phase) * 0.5 + 0.5
            )
            self._levels = idle.astype(np.float32)
        else:
            self._levels *= 0.88

        self.refresh()

    def render_line(self, y: int) -> Strip:
        w = self.size.width
        h = self.size.height
        if w <= 0 or h <= 0:
            return Strip.blank(w)

        # Одна белая колонка + один пробел — классическая геометрия cava.
        bar_count = max(1, (w + 1) // 2)
        source_x = np.linspace(0.0, 1.0, audio.NUM_BANDS)
        target_x = np.linspace(0.0, 1.0, bar_count)
        levels = np.interp(target_x, source_x, self._levels)
        segments: list[Segment] = []

        for col in range(w):
            if col % 2:
                segments.append(Segment(" ", BAR_STYLE))
                continue
            level = float(levels[min(bar_count - 1, col // 2)])
            filled = level * h  # высота столбца в строках
            # строки рисуем сверху вниз: верхняя строка y=0
            row_from_bottom = h - 1 - y
            cell_fill = filled - row_from_bottom
            if cell_fill >= 1:
                ch = "█"
            elif cell_fill <= 0:
                ch = " "
            else:
                ch = BLOCKS[max(0, min(8, int(cell_fill * 8)))]
            segments.append(Segment(ch, BAR_STYLE))

        return Strip(segments)
