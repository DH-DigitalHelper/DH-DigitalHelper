"""Live progress reporting."""

from __future__ import annotations

import sys
import threading
import time


def _fmt(counts: dict) -> str:
    return " | ".join(f"{k} {v}" for k, v in counts.items())


class Progress:
    def __init__(
        self,
        stream=None,
        is_tty: bool | None = None,
        clock=time.monotonic,
        interval: float = 0.1,
    ) -> None:
        self.stream = stream or sys.stderr
        self.is_tty = self.stream.isatty() if is_tty is None else is_tty
        self._clock = clock
        self._interval = interval
        self._lock = threading.Lock()
        self._tracks: dict[str, dict] = {}
        self._last_paint: float | None = None
        self._live_lines = 0
        self._rate = 0.0
        self._rate_prev: tuple[float, int] | None = None

    def header(self, text: str) -> None:
        with self._lock:
            self._erase_live()
            self.stream.write(f"\n── {text} ──\n")
            self.stream.flush()

    def update(
        self, counts: dict, current: str, key: str = "", queued: int | None = None
    ) -> None:
        with self._lock:
            self._tracks[key] = {"counts": counts, "current": current, "queued": queued}
            self._maybe_paint(force=False)

    def note(self, text: str) -> None:
        with self._lock:
            self._erase_live()
            self.stream.write(f"  ↳ {text}\n")
            self.stream.flush()

    def summary(self, title: str, counts: dict) -> None:
        with self._lock:
            self._erase_live()
            self.stream.write(f"\n{title}: {_fmt(counts)}\n")
            self.stream.flush()

    def _maybe_paint(self, force: bool) -> None:
        now = self._clock()
        if (
            not force
            and self._last_paint is not None
            and now - self._last_paint < self._interval
        ):
            return
        self._update_rate(now)
        self._last_paint = now
        lines = self._build_lines()
        if not lines:
            return
        if self.is_tty:
            move = f"\033[{self._live_lines}A" if self._live_lines else ""
            self.stream.write("\r" + move + "\033[J")
            self.stream.write("\n".join(lines))
            self._live_lines = len(lines) - 1
        else:
            self.stream.write(lines[-1] + "\n")
        self.stream.flush()

    def _erase_live(self) -> None:
        if self.is_tty and self._live_lines:
            self.stream.write(f"\r\033[{self._live_lines}A\033[J")
        elif self.is_tty:
            self.stream.write("\r\033[J")
        self._live_lines = 0

    def _build_lines(self) -> list[str]:
        if not self._tracks:
            return []
        width = max(len(k) for k in self._tracks)
        lines = []
        solo_rate = f" | {self._rate:.0f}/s" if len(self._tracks) == 1 else ""
        for key, track in self._tracks.items():
            label = f"{key:<{width}}  " if key else ""
            queued = track.get("queued")
            q = f" | {queued} queued" if queued is not None else ""
            lines.append(
                f"{label}{_fmt(track['counts'])}{q}{solo_rate} → {track['current'][:50]}"
            )
        if len(self._tracks) > 1:
            total: dict = {}
            for track in self._tracks.values():
                for k, v in track["counts"].items():
                    total[k] = total.get(k, 0) + v
            queued_vals = [
                t["queued"]
                for t in self._tracks.values()
                if t.get("queued") is not None
            ]
            q = f" | {sum(queued_vals)} queued" if queued_vals else ""
            label = f"{'TOTAL':<{width}}  "
            lines.append(f"{label}{_fmt(total)}{q} | {self._rate:.0f}/s")
        return lines

    def _update_rate(self, now: float) -> None:
        total = sum(v for t in self._tracks.values() for v in t["counts"].values())
        if self._rate_prev is not None:
            prev_t, prev_total = self._rate_prev
            dt = now - prev_t
            if dt > 0:
                inst = (total - prev_total) / dt
                self._rate = (
                    inst if self._rate == 0.0 else 0.5 * self._rate + 0.5 * inst
                )
        self._rate_prev = (now, total)
