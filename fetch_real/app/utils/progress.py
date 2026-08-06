from __future__ import annotations

import sys
import time
from typing import TextIO


class ProgressBar:
    """Single-line terminal progress with percentage (Windows-friendly)."""

    def __init__(self, total: int, *, prefix: str = "Progress", file: TextIO = sys.stderr) -> None:
        self.total = max(0, int(total))
        self.prefix = prefix
        self.file = file
        self.done = 0
        self._started = time.monotonic()
        self._last_len = 0
        self._closed = False

    def update(self, done: int | None = None, *, current: str = "", written: int | None = None) -> None:
        if self._closed:
            return
        if done is not None:
            self.done = done
        self.done = min(self.done, self.total) if self.total else self.done

        pct = 100.0 if self.total == 0 else (100.0 * self.done / self.total)
        filled = 0 if self.total == 0 else int(30 * self.done / self.total)
        bar = "#" * filled + "-" * (30 - filled)

        elapsed = max(0.001, time.monotonic() - self._started)
        rate = self.done / elapsed
        remaining = 0.0 if rate <= 0 or self.total <= self.done else (self.total - self.done) / rate

        parts = [
            f"{self.prefix}",
            f"|{bar}|",
            f"{pct:6.1f}%",
            f"{self.done}/{self.total}",
        ]
        if written is not None:
            parts.append(f"saved={written}")
        parts.append(f"eta={_fmt_seconds(remaining)}")
        if current:
            # keep line short
            slug = current if len(current) <= 40 else current[:37] + "..."
            parts.append(slug)

        line = " ".join(parts)
        pad = max(0, self._last_len - len(line))
        self.file.write("\r" + line + (" " * pad))
        self.file.flush()
        self._last_len = len(line)

    def advance(self, n: int = 1, *, current: str = "", written: int | None = None) -> None:
        self.update(self.done + n, current=current, written=written)

    def close(self, final_msg: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        if final_msg:
            self.file.write("\r" + final_msg + (" " * max(0, self._last_len - len(final_msg))) + "\n")
        else:
            self.file.write("\n")
        self.file.flush()


def _fmt_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"
