from rex_agents.text import Span, fit, key_at
from rex_agents.watch import read_keys

LINES = [[Span("title")], [Span("abc "), Span("24h", key="1"), Span(" "), Span("7d", key="2")]]


def test_a_span_says_which_key_a_click_presses():
    assert [key_at(LINES[1], c) for c in (3, 4, 6, 7, 8, 9, 10)] == [None, "1", "1", None, "2", "2", None]
    assert key_at(fit(LINES[1], 6), 5) is None and key_at(fit(LINES[1], 6), 4) == "1"


def test_clicks_read_as_the_key_under_them_and_other_escapes_are_dropped():
    click = "\x1b[<0;{};{}M"
    assert read_keys(click.format(9, 2) + "\x1b[<0;9;2m", LINES) == ["2"]  # the release adds nothing
    assert read_keys("1" + click.format(5, 2) + "q", LINES) == ["1", "1", "q"]
    assert read_keys(click.format(5, 1) + click.format(5, 9) + "\x1b[<2;5;2M", LINES) == []
    assert read_keys("\x1b[A\x1bOP3", LINES) == ["3"]


def test_the_pointer_reverses_the_link_under_it():
    from rex_agents.text import highlight, to_ansi
    from rex_agents.watch import pointer
    assert pointer("\x1b[<35;9;2M" + "\x1b[<35;5;2M") == (1, 4)
    assert pointer("\x1b[<0;5;2M") is None and pointer("x") is None
    lit = highlight(LINES[1], 4)
    assert [s.color for s in lit] == [None, "7", None, None]
    assert highlight(LINES[1], 0) == LINES[1]
    # A reversed blank shows, so a link's gap lights with it.
    assert to_ansi([Span("  ", "7")], True) == "\x1b[7m  \x1b[0m"
    assert read_keys("\x1b[<35;5;2M", LINES) == []  # a move presses nothing
