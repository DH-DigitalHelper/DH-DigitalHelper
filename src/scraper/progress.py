"""Live progress reporting.

Every crawl/extract worker calls :meth:`Progress.update` with its *own* site's
running counts. The reporter keeps the latest snapshot per site (keyed by
``key``) and repaints a single multi-line block on a throttled tick: one row
per site plus a TOTAL row. Two problems this design avoids:

* **Line-stomping.** Previously all sites shared one ``\\r`` status line and
  each worker wrote its own site's counter, so the visible number appeared to
  jump (10 -> 2000 -> 50) as different sites' workers took turns. Now each site
  owns a row and the total is their sum, so the frame is always coherent.
* **Flush storms.** Previously every fetched URL took a lock, wrote, and
  flushed stderr; with hundreds of workers that serialized into real lag. Now
  updates only mutate in-memory state and repaint at most once per ``interval``.
"""

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
        # key -> {"counts": dict, "current": str}, insertion-ordered so site
        # rows keep a stable position across repaints.
        self._tracks: dict[str, dict] = {}
        self._last_paint: float | None = None
        self._live_lines = 0  # newlines occupied by the on-screen live block
        self._rate = 0.0
        self._rate_prev: tuple[float, int] | None = None  # (time, total items)

    # -- public API --------------------------------------------------------

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

    # -- rendering ---------------------------------------------------------

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
            self.stream.write("\r" + move + "\033[J")  # jump to top, clear down
            self.stream.write("\n".join(lines))
            self._live_lines = len(lines) - 1
        else:
            # Piped/CI: no cursor control, just a periodic one-line snapshot.
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
        # With several tracks the throughput lives on the TOTAL row below; with
        # a single track (e.g. the extract phase, key="") there is no TOTAL row,
        # so the rate rides on the sole line instead.
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
                # Light EMA so the rate reads steadily instead of twitching.
                self._rate = (
                    inst if self._rate == 0.0 else 0.5 * self._rate + 0.5 * inst
                )
        self._rate_prev = (now, total)
