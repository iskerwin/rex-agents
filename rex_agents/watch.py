"""The full-screen loop: redraw every frame, refresh in the background."""

from __future__ import annotations

import os
import re
import select
import shutil
import signal
import sys
import threading
import time
from typing import Any, Callable, Optional

from rex_agents.text import Line, key_at, to_ansi

FRAME_SECONDS = 0.1
HIDE_CURSOR, SHOW_CURSOR = "\x1b[?25l", "\x1b[?25h"
ENTER_ALT, LEAVE_ALT = "\x1b[?1049h", "\x1b[?1049l"
# Clicks reported as `ESC [ < button ; column ; row M`, 1-based.
MOUSE_ON, MOUSE_OFF = "\x1b[?1000h\x1b[?1006h", "\x1b[?1006l\x1b[?1000l"
MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
OTHER_ESCAPE = re.compile(r"\x1b(\[[0-9;?<]*[ -/]*[@-~]|O.|.)?")

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

    def pressed(self, lines: list[Line]) -> list[str]:
        """The keys pressed since the last call; a click on `lines` counts as
        the key of the span it lands on."""
        if self.saved is None or not select.select([sys.stdin], [], [], 0)[0]:
            return []
        return read_keys(os.read(sys.stdin.fileno(), 1024).decode(errors="ignore"), lines)


def read_keys(data: str, lines: list[Line]) -> list[str]:
    """Plain keys in order, a left click on a span with a key as that key;
    other escape sequences, releases and other buttons are dropped."""
    keys: list[str] = []
    at = 0
    for m in MOUSE.finditer(data):
        keys += OTHER_ESCAPE.sub("", data[at:m.start()])
        at = m.end()
        button, column, row = int(m[1]), int(m[2]), int(m[3])
        if button == 0 and m[4] == "M" and 0 < row <= len(lines):
            key = key_at(lines[row - 1], column - 1)
            if key:
                keys.append(key)
    return keys + list(OTHER_ESCAPE.sub("", data[at:]))


def watch(fetch: Fetch, compose: Compose, color: bool, interval: int,
          on_key: Optional[Callable[[str], None]] = None) -> None:
    """Redraws `compose`'s rows until q, calling `fetch` every `interval` seconds
    and `on_key` with any other key pressed or clicked."""
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
    lines: list[Line] = []
    _write(ENTER_ALT + HIDE_CURSOR + MOUSE_ON + "\x1b[2J")
    try:
        with _CbreakInput() as keys:
            while True:
                for key in keys.pressed(lines):
                    if key in ("q", "Q", "\x03"):
                        return
                    if on_key:
                        on_key(key)
                width, height = terminal_size()
                lines = compose(state["snapshot"], state["error"], width, height)
                rendered = [to_ansi(line, color) for line in lines]
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
        _write(MOUSE_OFF + SHOW_CURSOR + LEAVE_ALT)
