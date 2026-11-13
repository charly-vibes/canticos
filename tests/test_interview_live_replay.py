#!/usr/bin/env python3
"""Purpose: Pin interview-live's deterministic replay reducer (canticos-qes).
Responsibilities:
  - Assert replay of the 20261113 slice is byte-identical across runs
  - Pin golden counts and transcript order on the fixture
  - Exercise lifecycle transitions (shown/asked/dismissed) on synthetic events
  - Tolerate empty/malformed event lines and an empty log
  - Require an injected-clock parameter and verify the CLI emits the same state
Rationale: parameter sweeps need the event log to be the single source of truth.
  If replay is deterministic and lifecycle transitions reuse core.Queue, a
  recorded session can be re-scored without re-running capture or trusting the
  system clock.
"""

import inspect
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
SCRIPT = BIN / "interview_live_replay.py"
FIXTURE = ROOT / "tests" / "fixtures" / "interview-20261113-slice.jsonl"

sys.path.insert(0, str(BIN))
import interview_live_core as core  # noqa: E402
import interview_live_replay as replay  # noqa: E402


def _reduce_fixture():
    return replay.reduce_events(replay.load_events(FIXTURE))


def test_fixture_replay_is_byte_identical():
    first = replay.dump_state(_reduce_fixture(), compact=True)
    second = replay.dump_state(_reduce_fixture(), compact=True)
    assert first == second


def test_fixture_counts_and_transcript_order():
    state = _reduce_fixture()
    assert state["counts"] == {
        "batches": 31,
        "questions": 101,
        "topics": 44,
        "segments": 147,
    }
    assert len(state["transcript"]) == 147

    expected = [
        event["text"]
        for event in replay.load_events(FIXTURE)
        if event.get("type") == "transcript_segment"
    ]
    assert [line["text"] for line in state["transcript"]] == expected
    assert state["first_ts"] == 1794570317.8505032
    assert state["last_ts"] > state["first_ts"]
    assert state["reason"] == "interrupted"
    assert state["session"]["id"] == "interview-20261113-084517"


def test_lifecycle_events_flip_question_status():
    events = [
        {"ts": 1.0, "type": "suggest_result",
         "questions": ["primera pregunta", "segunda pregunta", "tercera pregunta"],
         "topics": []},
        {"ts": 2.0, "type": "question_shown",
         "key": core.normalize_topic("primera pregunta"), "text": "primera pregunta"},
        {"ts": 3.0, "type": "question_asked",
         "key": core.normalize_topic("primera pregunta"), "text": "primera pregunta"},
        {"ts": 4.0, "type": "question_dismissed", "key": "", "text": "segunda pregunta"},
    ]
    by_key = {q["key"]: q for q in replay.reduce_events(events)["questions"]}

    assert by_key[core.normalize_topic("primera pregunta")]["status"] == "asked"
    assert by_key[core.normalize_topic("primera pregunta")]["shown"] is True
    assert by_key[core.normalize_topic("segunda pregunta")]["status"] == "dismissed"
    assert by_key[core.normalize_topic("tercera pregunta")]["status"] == "pending"


def test_empty_and_malformed_lines_are_tolerated(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n"
        "not json\n"
        "[]\n"
        "null\n"
        '{"ts": 10.0, "type": "transcript_segment", "start_ms": 0, '
        '"end_ms": 1000, "speaker": 0, "text": "hola"}\n'
        '{"ts": "bad", "type": "suggest_result", "questions": ["q"], "topics": null}\n',
        encoding="utf-8",
    )
    events = replay.load_events(path)
    assert len(events) == 2

    state = replay.reduce_events(events, now=1234.0)
    assert state["counts"] == {
        "batches": 1, "questions": 1, "topics": 0, "segments": 1,
    }
    assert state["transcript"][0]["line"] == "[00:00:00] (speaker 0) hola"
    # No ts on the batch: the injected clock supplies the freshness stamp.
    assert state["questions"][0]["added_at"] == 1234.0


def test_empty_log_yields_empty_state():
    state = replay.reduce_events([])
    assert state["counts"] == {
        "batches": 0, "questions": 0, "topics": 0, "segments": 0,
    }
    assert state["transcript"] == []
    assert state["questions"] == []
    assert state["topics"] == []
    assert state["session"] is None
    assert state["reason"] is None
    assert state["first_ts"] is None and state["last_ts"] is None


def test_reducer_takes_injected_clock():
    params = inspect.signature(replay.reduce_events).parameters
    assert "now" in params
    assert params["now"].default is None


def test_cli_matches_reducer_and_is_stable():
    state = _reduce_fixture()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURE), "--json"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(proc.stdout) == json.loads(replay.dump_state(state, compact=True))

    again = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURE), "--json"],
        capture_output=True, text=True, check=True,
    )
    assert proc.stdout == again.stdout

    pretty = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURE)],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(pretty.stdout)["counts"]["segments"] == 147