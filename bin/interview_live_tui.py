#!/usr/bin/env python3
"""Purpose: Reactive curses dashboard for interview-live.
Responsibilities:
  - Ask how many participants are in the conversation at session start
  - Render a live transcript pane plus a three-zone NEXT pane: an ASK NOW lead
    card (best pending question + grounds quote + [MM:SS]), a compact backlog of
    the following pending questions with j/k selection, and the topics strip
  - Keep the lead card stable: minimum dwell 8 s and only swap for a challenger
    scoring HYSTERESIS above the current card (pure function of ui state + now)
  - Keys: q quit, a/space ask selected, d dismiss selected, j/k select,
    f ask now, PgUp/PgDn scroll the transcript
  - Emit question lifecycle events (question_shown / question_asked /
    question_dismissed) through an injectable sink (canticos-vvc)
Rationale: canticos-c02 — an interviewing copilot is a dashboard you glance at,
  not a form you fill. canticos-wrl — one mixed ranked backlog forced a
  space-press every ~8.5 s in the baseline session; the NEXT card turns the
  dashboard into a recommendation instead of a triage list. build_layout() is
  kept pure (no curses) so the layout logic is unit-testable; curses is
  imported lazily so plain/pipe mode never depends on ncurses. canticos-vvc —
  the sink defaults to a no-op so the purity of layout tests survives while the
  coordinator wires the real EventLog.
"""

import sys
import time

try:
    import curses
except ImportError:  # pragma: no cover - plain mode must not need ncurses
    curses = None

import interview_live_core as core
from interview_live_core import fmt_ms  # noqa: E402 — single shared definition

# NEXT-card tuning (canticos-wrl). The lead card is a recommendation that must
# not churn: it survives at least LEAD_DWELL_S and a challenger must beat it by
# more than LEAD_HYSTERESIS to take over.
LEAD_DWELL_S = 8.0
LEAD_HYSTERESIS = 0.10
BACKLOG_MAX = 5          # 3-5 rows once enough pending questions exist
PLAIN_BACKLOG = 3        # stdout parity: lead card + three following questions
DWELL_S = LEAD_DWELL_S   # short aliases for tests/callers spelling either name
HYSTERESIS = LEAD_HYSTERESIS


def fmt_mmss(ms):
    """[MM:SS] card timestamp; hours only appear once the session passes 1 h."""
    s = max(0, int(ms)) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def available():
    return curses is not None


def guest_label(n):
    return "Guest" if n <= 2 else f"Guests({n - 1})"


def role_label(speaker, n):
    return "You" if speaker == 0 else guest_label(n)


def _noop_emit(_etype, **_payload):
    """Default sink: pure layout and unit tests need no EventLog."""


def emit_question(emit, etype, q, **extra):
    """Shared emit helper: every question lifecycle event carries the same
    identity payload (key/text/priority/score), so jq filters stay stable.
    Extra fields (e.g. reason='expired') ride along verbatim."""
    emit(etype, key=q["key"], text=q["text"],
         priority=q["priority"], score=q["score"], **extra)


class TuiState:
    """Shared state mutated by the worker thread, rendered by the TUI thread."""

    def __init__(self, queue=None, speaker_names=None, emit=None):
        self.participants = 2
        self.transcript = []          # [(start_ms, speaker, text), ...]
        self.queue = queue or core.Queue()
        self.capture = "starting"     # starting | live | replay | stopped
        self.started = time.time()
        self.last_offset_ms = 0
        self.suggestion = "idle"      # idle | thinking | done | error
        self.force_request = False    # canticos-2iv: 'f' asks pi for a round now
        self.speaker_names = speaker_names or {}  # canticos-uzv: id -> {"name"}
        self.emit = emit or _noop_emit  # canticos-vvc: event sink (EventLog.emit)
        # NEXT card (canticos-wrl): lead-bookkeeping lives here so the layout
        # stays deterministic across frames, plus the backlog selection index.
        self.lead_key = None
        self.lead_since = None
        self.selection = 0

    def expire_stale(self, now=None):
        """Retire stale pending questions (canticos-q9j) and emit one
        question_dismissed(reason='expired') per transition. Idempotent."""
        expired = self.queue.expire_pending(now)
        for q in expired:
            emit_question(self.emit, "question_dismissed", q, reason="expired")
        return expired

    def note_shown(self, q, now=None):
        """Render hook: expire stale questions first, then emit question_shown
        the first time a still-pending question appears in the view; later
        frames are no-ops (queue owns the flag)."""
        self.expire_stale(now)
        shown = self.queue.mark_shown(q["key"])
        if shown:
            emit_question(self.emit, "question_shown", shown)

    def mark_asked(self, key):
        """Ask transition: queue status plus the question_asked event."""
        q = self.queue.mark_asked(key)
        if q:
            emit_question(self.emit, "question_asked", q)
        return q

    def mark_dismissed(self, key):
        """Dismiss transition for the 'd' binding: queue status plus the
        question_dismissed event (negative feedback for the ranker)."""
        q = self.queue.mark_dismissed(key)
        if q:
            emit_question(self.emit, "question_dismissed", q)
        return q

    # -- NEXT card ---------------------------------------------------------
    def select_lead(self, now=None, pending=None):
        """Stable ASK NOW question: highest-score pending, kept for at least
        LEAD_DWELL_S and replaced only when a challenger beats it by more than
        LEAD_HYSTERESIS. Mutates only the lead bookkeeping, so repeated calls
        with the same state + clock are deterministic."""
        now = now if now is not None else time.time()
        pending = self.queue.pending_questions() if pending is None else pending
        if not pending:
            self.lead_key = None
            self.lead_since = now
            return None
        by_key = {q["key"]: q for q in pending}
        lead = by_key.get(self.lead_key)
        if lead is None:
            lead = pending[0]
            self.lead_key = lead["key"]
            self.lead_since = now
            return lead
        challenger = next((q for q in pending if q["key"] != lead["key"]), None)
        dwelled = now - (self.lead_since if self.lead_since is not None else now) >= LEAD_DWELL_S
        if (dwelled and challenger is not None
                and challenger["score"] > lead["score"] + LEAD_HYSTERESIS):
            self.lead_key = challenger["key"]
            self.lead_since = now
            return challenger
        return lead

    def backlog_questions(self, pending=None):
        """The 3-5 pending questions following the lead card (the lead itself is
        the ASK NOW card, not a backlog row)."""
        pending = self.queue.pending_questions() if pending is None else pending
        lead_key = self.lead_key
        if lead_key is None and pending:
            lead_key = pending[0]["key"]
        return [q for q in pending if q["key"] != lead_key][:BACKLOG_MAX]

    def selectable_questions(self, pending=None):
        """Selection domain: the lead card first (index 0, so a/space without
        moving asks the ASK NOW recommendation, matching v1 space) followed by
        the backlog rows."""
        pending = self.queue.pending_questions() if pending is None else pending
        if not pending:
            return []
        lead_key = self.lead_key
        lead = next((q for q in pending if q["key"] == lead_key), pending[0])
        return [lead] + [q for q in pending if q["key"] != lead["key"]][:BACKLOG_MAX]

    def clamp_selection(self, pending=None):
        n = len(self.selectable_questions(pending=pending))
        self.selection = min(self.selection, n - 1) if n else 0
        if self.selection < 0:
            self.selection = 0
        return self.selection

    def selected_question(self, pending=None):
        """Selected question (lead at index 0, then backlog rows); None when
        nothing is pending."""
        selectable = self.selectable_questions(pending=pending)
        if not selectable:
            self.selection = 0
            return None
        self.selection = min(max(0, self.selection), len(selectable) - 1)
        return selectable[self.selection]

    def move_selection(self, delta):
        """j/k selection across the lead card and backlog; clamped, a no-op
        when nothing is pending."""
        selectable = self.selectable_questions()
        if not selectable:
            self.selection = 0
            return 0
        self.selection = min(max(0, self.selection + delta), len(selectable) - 1)
        return self.selection

    def ask_selected(self):
        """Ask the selected backlog question (a/space). Empty backlog -> None."""
        q = self.selected_question()
        if not q:
            return None
        asked = self.mark_asked(q["key"])
        self.clamp_selection()
        return asked

    def dismiss_selected(self):
        """Dismiss the selected backlog question (d). Empty backlog -> None."""
        q = self.selected_question()
        if not q:
            return None
        dismissed = self.mark_dismissed(q["key"])
        self.clamp_selection()
        return dismissed


def _clip(text, width):
    if width <= 0:
        return ""
    text = text.replace("\t", " ").replace("\r", "").replace("\n", " ")
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _prio_mark(priority):
    return "*" if priority == "high" else ("+" if priority == "medium" else "-")


def _lead_stamp(ui, q):
    """[MM:SS] arrival timestamp of the question relative to session start."""
    return fmt_mmss((q.get("added_at", ui.started) - ui.started) * 1000)


def build_layout(ui, width, height, scroll=0, now=None):
    """No curses and no direct I/O — unit-testable.

    Returns header / transcript / lead / backlog / topics / footer zones plus
    the transcript scroll position and backlog selection. The right pane is the
    canticos-wrl three-zone NEXT card: ASK NOW (one stable lead question with
    its grounds quote and [MM:SS]), BACKLOG (the following pending questions
    with a j/k selection index) and TOPICS (unchanged rendering).

    Side effects, both through ui.emit (no-op by default): stale pending
    questions are expired (question_dismissed with reason='expired',
    canticos-q9j) and pending questions rendered in the lead/backlog emit
    question_shown. Rendering (colors, cursor) is an orthogonal, untested thin
    layer in draw().
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

    # --- NEXT card: ASK NOW lead + backlog + topics ------------------------
    ui.expire_stale(now)  # canticos-q9j: retire stale questions before rendering
    pending = ui.queue.pending_questions()
    lead = ui.select_lead(now, pending)
    backlog_qs = ui.backlog_questions(pending=pending)
    ui.clamp_selection(pending=pending)

    lead_zone = {"key": None, "text": "", "grounds": "", "timestamp": "",
                 "priority": None, "score": None, "lines": []}
    shown_qs = []
    if lead is None:
        lead_zone["lines"] = [_clip("ASK NOW - no pending question", width)]
    else:
        stamp = _lead_stamp(ui, lead)
        grounds = (lead.get("grounds") or "").strip()
        cursor = ">" if ui.selection == 0 else " "
        lines = [f"ASK NOW [{stamp}]",
                 _clip(f"{cursor} {_prio_mark(lead['priority'])} {lead['text']}",
                       width)]
        if grounds:
            lines.append(_clip(f"  > {grounds}", width))
        lead_zone = {
            "key": lead["key"], "text": lead["text"], "grounds": grounds,
            "timestamp": stamp, "priority": lead["priority"],
            "score": lead["score"], "lines": [_clip(l, width) for l in lines],
        }
        shown_qs.append(lead)

    backlog = []
    for i, q in enumerate(backlog_qs):
        selected = i + 1 == ui.selection  # index 0 is the lead card
        text = _clip(("> " if selected else "  ") + f"{i + 1}. "
                     f"{_prio_mark(q['priority'])} {q['text']}", width)
        backlog.append({"key": q["key"], "text": q["text"], "index": i,
                        "selected": selected, "priority": q["priority"],
                        "score": q["score"], "status": q["status"], "line": text})
        shown_qs.append(q)

    topics = []
    for t in ui.queue.ranked_topics():
        topics.append({"name": t["name"], "mentions": t["mentions"],
                       "priority": t["priority"],
                       "line": _clip(f"{_prio_mark(t['priority'])} {t['name']} "
                                     f"x{t['mentions']}", width)})

    right = [("lead" if i == 0 else "card", line)
             for i, line in enumerate(lead_zone["lines"])]
    if backlog:
        right.append(("title", "BACKLOG"))
        right.extend(("selected" if e["selected"] else "backlog", e["line"])
                     for e in backlog)
    if topics:
        right.append(("title", "TOPICS"))
        right.extend(("topic", t["line"]) for t in topics)
    right = right[:max(0, main_h - 1)]

    # The lead card and the visible backlog rows are exactly the rendered
    # pending questions; asked/dismissed/expired never reach these zones.
    for q in shown_qs:
        ui.note_shown(q, now)

    hint = ("q quit | j/k select | a/space ask selected | d dismiss selected "
            "| f ask now | PgDn scrolls (not dismiss)")
    if width < 70:  # keep the d/PgDn hint visible in a narrow terminal
        hint = "j/k sel a/space ask d dismiss PgDn scroll q quit"
    footer = _clip(hint, width)

    return {
        "width": width,
        "height": height,
        "header": header,
        "transcript": transcript,
        "lead": lead_zone,
        "backlog": backlog,
        "topics": topics,
        "right": right,
        "selection": ui.selection,
        "footer": footer,
        "scroll": scroll,
    }


def render_plain_board(ui, now=None, backlog_limit=PLAIN_BACKLOG):
    """Plain stdout parity for the NEXT card (canticos-wrl): lead question with
    its grounds quote and [MM:SS], then a compact following backlog. No JSON,
    no raw pi dump. Shares TuiState lead bookkeeping so dwell/hysteresis apply
    across batches exactly as in the dashboard."""
    now = now if now is not None else time.time()
    pending = ui.queue.pending_questions()
    lead = ui.select_lead(now, pending)
    following = ui.backlog_questions(pending=pending)[:backlog_limit]
    lines = ["", "--- next question ---"]
    if lead is None:
        lines.append("ASK NOW - no pending question")
    else:
        stamp = _lead_stamp(ui, lead)
        lines.append(f"ASK NOW [{stamp}] {lead['text']}")
        grounds = (lead.get("grounds") or "").strip()
        if grounds:
            lines.append(f"  > {grounds}")
    lines.append("Backlog:")
    if following:
        lines.extend(f"  {i}. {q['text']}" for i, q in enumerate(following, 1))
    else:
        lines.append("  (empty)")
    return "\n".join(lines) + "\n"


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
        # NEXT card zones (right column): ASK NOW lead, backlog, topics
        x = left_w + 1
        if x < w:
            qy = 2
            for kind, text in layout["right"]:
                if qy >= h - 1:
                    break
                if kind == "title":
                    pair = curses.color_pair(4) | curses.A_BOLD
                elif kind == "lead":
                    pair = curses.color_pair(5) | curses.A_BOLD
                elif kind == "selected":
                    pair = curses.color_pair(5) | curses.A_BOLD
                elif kind == "topic":
                    pair = curses.color_pair(1)  # topics unchanged (canticos-6ic)
                elif kind == "card" and layout["lead"]["priority"] == "high":
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


def handle_key(ui, ch, scroll):
    """One dashboard key, curses-free so it is unit-testable. Returns the new
    transcript scroll. All backlog actions are no-ops when the backlog is
    empty; 'd' is dismissal now, never PgDn (PgDn still scrolls)."""
    if ch in (ord("a"), ord(" ")):
        ui.ask_selected()
    elif ch == ord("d"):
        ui.dismiss_selected()
    elif ch == ord("j"):
        ui.move_selection(1)
    elif ch == ord("k"):
        ui.move_selection(-1)
    elif ch == ord("f"):
        ui.force_request = True   # trigger a suggestion round now
    elif ch in (getattr(curses, "KEY_PPAGE", -1), ord("u")):
        scroll += 6
    elif ch == getattr(curses, "KEY_NPAGE", -1):
        scroll = max(0, scroll - 6)
    return scroll


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
        scroll = handle_key(ui, ch, scroll)
        h, w = stdscr.getmaxyx()
        layout = build_layout(ui, w, h, scroll=scroll)
        draw(stdscr, layout)
    return scroll


def main(argv=None):  # pragma: no cover - interactive only
    print("interview-live TUI module — run 'interview-live' for the dashboard", file=sys.stderr)


if __name__ == "__main__":
    main()