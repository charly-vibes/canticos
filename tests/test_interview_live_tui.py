#!/usr/bin/env python3
"""Purpose: Verify interview-live's TUI layout and core queue/parsing logic.
Responsibilities: Exercise build_layout() (pure, curses-free) for the dashboard
panes — participant count, live transcript, prioritized queue — plus the core
suggestion parsing (JSON vs legacy plain lines) and recurrence ranking.
Rationale: canticos-c02 — the dashboard is hard to test through a real terminal,
so the layout logic is kept pure and pinned here; the curses layer stays thin.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import interview_live_core as core  # noqa: E402
import interview_live_tui as tui  # noqa: E402


# ---------------------------------------------------------------------------
# core parsing
# ---------------------------------------------------------------------------

def test_parse_json_questions_and_topics():
    out = ('{"questions": ["Q1: a", "Q2: b"], '
           '"topics": [{"name": "Proveedor", "priority": "high", "note": "x"}]}')
    qs, ts = core.parse_suggestion_output(out)
    assert [q["text"] for q in qs] == ["Q1: a", "Q2: b"]
    assert all(q["priority"] == "medium" for q in qs)
    assert ts[0]["name"] == "Proveedor" and ts[0]["priority"] == "high"


def test_parse_json_mixed_question_shapes():
    out = '{"questions": ["a", {"text": "b", "priority": "high"}]}'
    qs, ts = core.parse_suggestion_output(out)
    assert qs[0]["priority"] == "medium"
    assert qs[1]["priority"] == "high"
    assert ts == []


def test_parse_json_ignores_markdown_fences():
    out = '```json\n{"questions": ["x"], "topics": []}\n```'
    qs, _ = core.parse_suggestion_output(out)
    assert qs[0]["text"] == "x"


def test_parse_plain_lines_fallback():
    out = "Q1: Tell me about the supplier.\n2) What did you do?\n- A third one"
    qs, ts = core.parse_suggestion_output(out)
    assert qs[0]["text"] == "Tell me about the supplier."
    assert qs[1]["text"] == "What did you do?"
    assert ts == []


def test_parse_garbage_empty():
    assert core.parse_suggestion_output("") == ([], [])
    assert core.parse_suggestion_output("pi: provider error")[0] == []


# ---------------------------------------------------------------------------
# core queue: recurrence ranking
# ---------------------------------------------------------------------------

def test_recurring_topic_outranks_oneoff_high():
    now = 1000.0
    q = core.Queue()
    q.ingest(questions=[{"text": "Qn1", "priority": "medium"}],
             topics=[{"name": "slow-burn", "priority": "medium", "note": ""}], now=now)
    now += 100
    q.ingest(questions=[{"text": "Qn2", "priority": "low"}],
             topics=[{"name": "slow-burn", "priority": "low", "note": ""},
                     {"name": "one-off", "priority": "high", "note": ""}], now=now)
    assert q.topics["slow burn"]["mentions"] == 2
    assert q.topics["one off"]["mentions"] == 1
    # two mentions of a medium topic tie a single high one (60+40 == 100);
    # a third mention pushes it ahead — recurrence is the "prioritized as it goes" signal
    now += 100
    q.ingest(topics=[{"name": "slow-burn", "priority": "medium", "note": ""}], now=now)
    assert q.topics["slow burn"]["mentions"] == 3
    ranked = [t["name"] for t in q.ranked_topics()]
    assert ranked[0] == "slow-burn"
    # questions keep arrival order unless a same-text question recurs
    assert [it["text"] for it in q.ranked_questions()] == ["Qn1", "Qn2"]
    # matched priority bump: topic seen as medium then low stays medium
    assert q.topics["slow burn"]["priority"] == "medium"


def test_question_dedupe_and_ask():
    q = core.Queue()
    q.ingest(questions=[{"text": "same question", "priority": "high"}])
    q.ingest(questions=[{"text": "same question", "priority": "low"}],
             topics=[{"name": "t", "priority": "medium", "note": ""}])
    items = q.ranked_questions()
    assert len(items) == 1 and items[0]["mentions"] == 2
    assert items[0]["priority"] == "high"  # strong priority wins
    top = q.top_pending_question()
    assert top["text"] == "same question"
    q.mark_asked(top["key"])
    assert q.ranked_questions()[0]["status"] == "asked"
    assert q.top_pending_question() is None


def test_snapshot_has_participants():
    q = core.Queue()
    q.ingest(topics=[{"name": "proveedor", "priority": "high", "note": "n"}])
    snap = q.snapshot(participants=3)
    assert snap["participants"] == 3
    assert snap["topics"][0]["mentions"] == 1
    assert "score" in snap["topics"][0]


# ---------------------------------------------------------------------------
# TUI layout (pure)
# ---------------------------------------------------------------------------

def make_ui():
    ui = tui.TuiState()
    ui.participants = 2
    ui.transcript = [
        (1000, 0, "Hola, cuentame sobre tu ultimo proyecto"),
        (5000, 1, "Bueno, el mes pasado tuvimos un problema con el proveedor"),
        (9000, 0, "Y como lo resolviste?"),
    ]
    ui.queue.ingest(
        questions=[{"text": "Cuentame mas del problema del proveedor", "priority": "high"}],
        topics=[{"name": "Proveedor", "priority": "high", "note": "recurring"}])
    ui.started = time.time() - 130  # 2m10s elapsed
    return ui


def test_layout_header_shows_clock_participants_and_status():
    lay = tui.build_layout(make_ui(), 100, 24)
    head = "\n".join(lay["header"])
    assert "02:10" in head          # elapsed clock
    assert "2 participants" in head
    assert "capture:" in head and "suggestions:" in head


def test_layout_transcript_shows_roles_and_text():
    lay = tui.build_layout(make_ui(), 100, 24)
    text = "\n".join(t for _, t in lay["transcript"])
    assert "problema" in text
    assert "You:" in text and "Guest:" in text


def test_layout_shows_participant_cap_hint_for_many():
    ui = make_ui()
    ui.participants = 4
    ui.transcript.append((12000, 1, "otro invitado habla"))
    lay = tui.build_layout(ui, 100, 24)
    head = "\n".join(lay["header"])
    assert "4 participants" in head and "2-speaker" in head
    assert any("Guests(3):" in t for _, t in lay["transcript"])


def test_layout_queue_shows_questions_and_topics_sorted():
    ui = make_ui()
    ui.queue.ingest(topics=[{"name": "proveedor", "priority": "high", "note": ""}])
    lay = tui.build_layout(ui, 100, 24)
    qtext = "\n".join(t for _, t, _, _ in lay["queue"])
    assert "Cuentame mas del problema del proveedor" in qtext
    assert "Proveedor" in qtext and "x2" in qtext


def test_layout_truncates_to_width():
    ui = make_ui()
    ui.transcript.append((13000, 1, "x" * 300))
    lay = tui.build_layout(ui, 60, 12)
    for _, line in lay["transcript"]:
        assert len(line) <= 60
    assert lay["footer"]  # footer present even in small terminal


def test_layout_mark_asked_renders_prefix():
    ui = make_ui()
    top = ui.queue.top_pending_question()
    ui.queue.mark_asked(top["key"])
    lay = tui.build_layout(ui, 100, 24)
    qlines = [t for _, t, s, _ in lay["queue"]]
    assert any(l.startswith("[x]") for l in qlines)


def test_layout_scroll_back_reveals_older_lines():
    ui = make_ui()
    for i in range(40):
        ui.transcript.append((20000 + i, 0, f"line {i}"))
    lay0 = tui.build_layout(ui, 100, 24, scroll=0)
    lay1 = tui.build_layout(ui, 100, 24, scroll=10)
    t0 = [t for _, t in lay0["transcript"]]
    t1 = [t for _, t in lay1["transcript"]]
    assert "line 39" in t0[-1]
    assert "line 39" not in t1[-1] and lay1["scroll"] == 10


# ---------------------------------------------------------------------------
# question lifecycle events (canticos-vvc)
# ---------------------------------------------------------------------------

def event_ui():
    """TuiState with a fake sink: layout/state tests need no EventLog."""
    events = []

    def sink(etype, **payload):
        events.append((etype, payload))

    ui = tui.TuiState(emit=sink)
    return ui, events


def test_question_shown_emitted_once_across_rerenders():
    """A pending question that stays in the rendered view is 'shown' exactly
    once — repeated frames must not re-emit it."""
    ui, events = event_ui()
    ui.queue.ingest(questions=[{"text": "Cuentame del problema del proveedor",
                                "priority": "high"}])
    tui.build_layout(ui, 100, 24)
    tui.build_layout(ui, 100, 24)
    shown = [p for e, p in events if e == "question_shown"]
    assert len(shown) == 1
    q = ui.queue.top_pending_question()
    assert shown[0] == {"key": q["key"], "text": q["text"],
                        "priority": q["priority"], "score": q["score"]}


def test_question_shown_not_emitted_for_asked_or_dismissed():
    ui, events = event_ui()
    ui.queue.ingest(questions=[{"text": "primera pregunta", "priority": "high"},
                               {"text": "segunda pregunta", "priority": "medium"}])
    keys = [q["key"] for q in ui.queue.ranked_questions()]
    ui.queue.mark_asked(keys[0])
    ui.queue.mark_dismissed(keys[1])
    tui.build_layout(ui, 100, 24)
    assert [p for e, p in events if e == "question_shown"] == []


def test_mark_asked_and_dismissed_emit_typed_events():
    """The state stub emits question_asked / question_dismissed with the
    question payload; ask still wins the status field."""
    ui, events = event_ui()
    ui.queue.ingest(questions=[{"text": "Cuentame del proveedor", "priority": "high"},
                               {"text": "Cuanto costo la multa", "priority": "medium"}])
    asked_key = ui.queue.ranked_questions()[0]["key"]
    ui.mark_asked(asked_key)
    dismissed_key = ui.queue.top_pending_question()["key"]
    ui.mark_dismissed(dismissed_key)
    kinds = [e for e, _ in events]
    assert kinds == ["question_asked", "question_dismissed"]
    asked = events[0][1]
    assert asked["key"] == asked_key and "proveedor" in asked["text"]
    assert {"priority", "score"} <= set(asked)
    dismissed = events[1][1]
    assert dismissed["key"] == dismissed_key and "multa" in dismissed["text"]
    assert ui.queue.questions[asked_key]["status"] == "asked"
    assert ui.queue.questions[dismissed_key]["status"] == "dismissed"
    # repeating a transition is a no-op: no duplicate lifecycle events
    ui.mark_asked(asked_key)
    ui.mark_dismissed(dismissed_key)
    assert [e for e, _ in events] == kinds


def test_dismissed_question_never_pending_nor_shown_again():
    """Dismissal removes the question from pending views for good: no
    top_pending_question, no ranked pending entry, no question_shown — even
    when the same text recurs."""
    ui, events = event_ui()
    ui.queue.ingest(questions=[{"text": "unica pregunta", "priority": "high"}])
    key = ui.queue.top_pending_question()["key"]
    ui.mark_dismissed(key)
    assert ui.queue.top_pending_question() is None
    assert [q["key"] for q in ui.queue.pending_questions()] == []
    ui.queue.ingest(questions=[{"text": "unica pregunta", "priority": "high"}])
    assert ui.queue.top_pending_question() is None
    assert [q["key"] for q in ui.queue.pending_questions()] == []
    tui.build_layout(ui, 100, 24)
    assert [p for e, p in events if e == "question_shown"] == []


# ---------------------------------------------------------------------------
# expiry events (canticos-q9j)
# ---------------------------------------------------------------------------

def test_build_layout_expires_stale_questions_and_emits_dismissed():
    """Rendering retires stale pending questions: question_dismissed with
    reason='expired' fires once, and the question leaves the pane."""
    ui, events = event_ui()
    ui.queue.ingest(questions=[{"text": "vieja pregunta", "priority": "high"}],
                    now=1000.0)
    key = ui.queue.top_pending_question()["key"]
    stale_now = core.expires_at(ui.queue.questions[key]) + 1.0
    lay = tui.build_layout(ui, 100, 24, now=stale_now)
    assert not any("vieja pregunta" in text for _, text, _, _ in lay["queue"])
    dismissed = [p for e, p in events if e == "question_dismissed"]
    assert len(dismissed) == 1
    assert dismissed[0]["key"] == key and dismissed[0]["reason"] == "expired"
    assert ui.queue.questions[key]["status"] == "expired"
    # next frame: nothing new to expire, no duplicate event
    tui.build_layout(ui, 100, 24, now=stale_now + 60)
    assert [e for e, _ in events] == ["question_dismissed"]


def test_note_shown_expires_before_shown_emission():
    """A question that went stale is dismissed (reason='expired') instead of
    being marked shown."""
    ui, events = event_ui()
    ui.queue.ingest(questions=[{"text": "pregunta caduca", "priority": "medium"}],
                    now=1000.0)
    q = ui.queue.top_pending_question()
    ui.note_shown(q)  # real clock is far past last_seen
    assert [e for e, _ in events] == ["question_dismissed"]
    assert events[0][1]["reason"] == "expired"
    assert ui.queue.questions[q["key"]]["shown"] is False
