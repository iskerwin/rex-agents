"""The full-screen loop: redraw every frame, refresh in the background."""

from __future__ import annotations

import select
import shutil
import signal
import sys
import threading
import time
from typing import Any, Callable, Optional

from rex_agents.text import Line, to_ansi

FRAME_SECONDS = 0.1
HIDE_CURSOR, SHOW_CURSOR = "\x1b[?25l", "\x1b[?25h"
ENTER_ALT, LEAVE_ALT = "\x1b[?1049h", "\x1b[?1049l"

Fetch = Callable[[], dict[str, Any]]
# What a frame shows: the snapshot, the last refresh's error, width, height.
Compose = Callable[[Optional[dict[str, Any]], Optional[str], int, int], list[Line]]


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def terminal_size() -> tuple[int, int]:
    size = shutil.get_terminal_size((40, 40))
    return size.columns, size.lines


class _CbreakInput:
    """Single key presses without echo, so typing cannot garble the screen."""

    def __enter__(self) -> "_CbreakInput":
        self.saved = None
        if sys.stdin.isatty():
            import termios
            import tty
            self.saved = termios.tcgetattr(sys.stdin)
            # TCSANOW: keep pending input, ^C still raises SIGINT in cbreak.
            tty.setcbreak(sys.stdin, termios.TCSANOW)
        return self

    def __exit__(self, *_: Any) -> None:
        if self.saved is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.saved)

    def pressed(self) -> Optional[str]:
        if self.saved is None or not select.select([sys.stdin], [], [], 0)[0]:
            return None
        return sys.stdin.read(1)


def watch(fetch: Fetch, compose: Compose, color: bool, interval: int) -> None:
    """Redraws `compose`'s rows until q, calling `fetch` every `interval` seconds."""
    state: dict[str, Any] = {"snapshot": None, "error": None}
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                state["snapshot"] = fetch()
                state["error"] = None
            except Exception as exc:
                # Keep showing the last good data, and say that it is old.
                state["error"] = str(exc) or type(exc).__name__
            stop.wait(interval)

    def on_term(*_: Any) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, on_term)
    threading.Thread(target=worker, daemon=True).start()
    shown: list[str] = []
    _write(ENTER_ALT + HIDE_CURSOR + "\x1b[2J")
    try:
        with _CbreakInput() as keys:
            while True:
                if keys.pressed() in ("q", "Q", "\x03"):
                    return
                width, height = terminal_size()
                rendered = [to_ansi(line, color) for line in compose(state["snapshot"], state["error"], width, height)]
                if len(rendered) != len(shown):
                    shown = [""] * len(rendered)
                    _write("\x1b[2J")
                # Rewrite changed lines only: no flicker, little output. The
                # line is cleared before it is written: clearing after would
                # erase the last column of a line that fills the width.
                out = [f"\x1b[{i + 1};1H\x1b[0m\x1b[2K{text}" for i, text in enumerate(rendered) if text != shown[i]]
                if out:
                    _write("".join(out))
                shown = rendered
                time.sleep(FRAME_SECONDS)
    except KeyboardInterrupt:
        return
    finally:
        stop.set()
        signal.signal(signal.SIGTERM, previous)
        _write(SHOW_CURSOR + LEAVE_ALT)
