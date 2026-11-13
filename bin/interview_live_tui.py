#!/usr/bin/env python3
"""Purpose: Reactive curses dashboard for interview-live.
Responsibilities:
  - Ask how many participants are in the conversation at session start
  - Render a live transcript pane and a prioritized queue pane (pi questions
    + important topics with recurrence counts)
  - Observe-only by default: nothing required from the user; optional keys
    (q quit, space mark top question asked, f ask now, PgUp/PgDn scroll transcript)
Rationale: canticos-c02 — an interviewing copilot is a dashboard you glance at,
  not a form you fill. build_layout() is kept pure (no curses) so the layout
  logic is unit-testable; curses is imported lazily so plain/pipe mode never
  depends on ncurses.
"""

import sys
import time

try:
    import curses
except ImportError:  # pragma: no cover - plain mode must not need ncurses
    curses = None

import interview_live_core as core
from interview_live_core import fmt_ms  # noqa: E402 — single shared definition


def available():
    return curses is not None


def guest_label(n):
    return "Guest" if n <= 2 else f"Guests({n - 1})"


def role_label(speaker, n):
    return "You" if speaker == 0 else guest_label(n)


class TuiState:
    """Shared state mutated by the worker thread, rendered by the TUI thread."""

    def __init__(self, queue=None, speaker_names=None):
        self.participants = 2
        self.transcript = []          # [(start_ms, speaker, text), ...]
        self.queue = queue or core.Queue()
        self.capture = "starting"     # starting | live | replay | stopped
        self.started = time.time()
        self.last_offset_ms = 0
        self.suggestion = "idle"      # idle | thinking | done | error
        self.force_request = False    # canticos-2iv: 'f' asks pi for a round now
        self.speaker_names = speaker_names or {}  # canticos-uzv: id -> {"name"}


def _clip(text, width):
    if width <= 0:
        return ""
    text = text.replace("\t", " ").replace("\r", "").replace("\n", " ")
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def build_layout(ui, width, height, scroll=0, now=None):
    """Pure layout: no curses, no I/O — unit-testable.

    Returns a dict with header / transcript / queue / footer line lists and the
    transcript scroll position. Rendering (colors, cursor) is an orthagonal,
    untested thin layer in draw().
    """
    now = now if now is not None else time.time()
    width = max(width, 30)
    height = max(height, 10)

    elapsed = int(max(0, now - ui.started))
    clock = f"{elapsed // 3600:02d}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}"
    n = ui.participants
    npart = f"{n} participants" + (" (2-speaker diarization cap)" if n > 2 else "")
    header = [
        f"interview-live   {clock}   {npart}   capture: {ui.capture}",
        f"transcript: {len(ui.transcript)} lines   whisper: {fmt_ms(ui.last_offset_ms)}   suggestions: {ui.suggestion}",
    ]
    header = [_clip(h, width) for h in header]

    main_h = max(1, height - 3)          # panes area (header 2 + footer 1)
    left_w = max(20, width * 2 // 3)

    # --- transcript pane (bottom-anchored, scroll back with PgUp) ----------
    cap = max(1, len(ui.transcript) - scroll)
    start = max(0, cap - main_h)
    view = ui.transcript[start:cap]
    transcript = [
        (spk, _clip(f"[{fmt_ms(ms)}] "
                    + (core.speaker_label(spk, ui.speaker_names)
                       if str(spk) in ui.speaker_names else role_label(spk, n))
                    + f": {text}", left_w))
        for ms, spk, text in view
    ]
    scroll = len(ui.transcript) - cap  # clamp scroll to available history

    # --- priority queue pane ----------------------------------------------
    cap_q = max(0, main_h - 1)
    entries = []  # (kind, priority_mark, text)
    for q in ui.queue.ranked_questions():
        mark = "*" if q["priority"] == "high" else ("+" if q["priority"] == "medium" else "-")
        prefix = "[x] " if q["status"] == "asked" else f"{mark} "
        entries.append(("Q", prefix + q["text"], q["status"], q["priority"]))
    for t in ui.queue.ranked_topics():
        mark = "*" if t["priority"] == "high" else ("+" if t["priority"] == "medium" else "-")
        entries.append(("T", f"{mark} {t['name']} x{t['mentions']}", "topic", t["priority"]))
    queue = [e for e in entries[:cap_q]]

    footer = _clip(
        "q quit | space: mark top question asked | f: ask now | PgUp/PgDn: scroll",
        width,
    )

    return {
        "width": width,
        "height": height,
        "header": header,
        "transcript": transcript,
        "queue": queue,
        "footer": footer,
        "scroll": scroll,
    }


# ---------------------------------------------------------------------------
# curses layer (thin, not unit-tested)
# ---------------------------------------------------------------------------

def _use_colors():
    try:
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_WHITE, -1)   # normal
            curses.init_pair(2, curses.COLOR_GREEN, -1)   # interviewer
            curses.init_pair(3, curses.COLOR_CYAN, -1)    # guest
            curses.init_pair(4, curses.COLOR_YELLOW, -1)  # header
            curses.init_pair(5, curses.COLOR_RED, -1)     # high priority
            curses.init_pair(6, curses.COLOR_BLACK, -1)   # dim/asked
    except curses.error:
        pass


def prompt_participants(stdscr, default):
    """Numeric input box; Enter accepts, Esc/empty keeps default."""
    if curses is None:
        raise RuntimeError("curses unavailable")
    stdscr.erase()
    stdscr.refresh()
    h, w = stdscr.getmaxyx()
    msg = "How many participants in this conversation? (whisper diarizes max 2 speakers)"
    buf = ""
    while True:
        label = f"Participants [{buf or default}]: "
        stdscr.erase()
        stdscr.addstr(max(0, h // 2 - 1), max(0, (w - len(msg)) // 2), msg)
        stdscr.addstr(max(0, h // 2), max(0, (w - len(label)) // 2), label)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("\n"), ord("\r"), curses.KEY_ENTER):
            break
        elif ch == 27 or ch == ord("q") or ch == 3:  # Esc / q / ^C -> keep default
            buf = ""
            break
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]
        elif 48 <= ch <= 57:
            buf += chr(ch)
    return int(buf) if buf.isdigit() else default


def draw(stdscr, layout):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    left_w = w * 2 // 3
    try:
        for y, line in enumerate(layout["header"]):
            stdscr.addnstr(y, 0, line, layout["width"], curses.color_pair(4) | curses.A_BOLD)
        y = len(layout["header"])
        # transcript (left column)
        for spk, line in layout["transcript"]:
            if y >= h - 1:
                break
            pair = curses.color_pair(2 if spk == 0 else 3)
            stdscr.addnstr(y, 0, line, left_w, pair)
            y += 1
        # priority queue (right column)
        x = left_w + 1
        if x < w:
            try:
                stdscr.addnstr(2, x, "PRIORITY QUEUE", w - x, curses.color_pair(4) | curses.A_BOLD)
            except curses.error:
                pass
            qy = 3
            for kind, text, status, prio in layout["queue"]:
                if qy >= h - 1:
                    break
                if status == "asked":
                    pair = curses.color_pair(6)
                elif kind == "Q" and prio == "high":
                    pair = curses.color_pair(5)
                else:
                    pair = curses.color_pair(1)
                try:
                    stdscr.addnstr(qy, x, text, w - x - 1, pair)
                except curses.error:
                    pass
                qy += 1
        stdscr.addnstr(h - 1, 0, layout["footer"], w, curses.color_pair(4))
    except curses.error:
        pass
    stdscr.refresh()


def run_dashboard(stdscr, ui, participants_default, stop_check=lambda: False):
    """Curses main loop. Returns when the user quits or stop_check() turns true."""
    if curses is None:
        raise RuntimeError("curses unavailable")
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    _use_colors()
    ui.participants = prompt_participants(stdscr, participants_default)
    stdscr.timeout(200)
    scroll = 0
    while not stop_check():
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1
        if ch in (ord("q"), 3):
            break
        elif ch == ord(" "):
            top = ui.queue.top_pending_question()
            if top:
                ui.queue.mark_asked(top["key"])
        elif ch == ord("f"):
            ui.force_request = True   # trigger a suggestion round now
        elif ch in (curses.KEY_PPAGE, ord("u")):
            scroll += 6
        elif ch in (curses.KEY_NPAGE, ord("d")):
            scroll = max(0, scroll - 6)
        elif ch == curses.KEY_RESIZE:
            pass
        h, w = stdscr.getmaxyx()
        layout = build_layout(ui, w, h, scroll=scroll)
        draw(stdscr, layout)
    return scroll


def main(argv=None):  # pragma: no cover - interactive only
    print("interview-live TUI module — run 'interview-live' for the dashboard", file=sys.stderr)


if __name__ == "__main__":
    main()