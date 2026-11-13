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

import datetime
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


def test_sweep_runs_every_combination_in_sorted_order():
    events = replay.load_events(FIXTURE)
    grid = {"now": [2000, 1000]}
    results = replay.sweep(events, grid)

    assert len(results) == 2
    assert [r["params"]["now"] for r in results] == [1000, 2000]
    for result in results:
        assert set(result) == {"params", "metrics"}
        assert result["metrics"]["transcript_lines"] == 147
        assert result["metrics"]["pending_questions"] >= 0

    first = json.dumps(results, sort_keys=True, ensure_ascii=False)
    second = json.dumps(replay.sweep(events, grid), sort_keys=True, ensure_ascii=False)
    assert first == second


def test_sweep_derives_metrics_from_reduced_state():
    events = [
        {"ts": 1.0, "type": "suggest_result",
         "questions": ["primera", "segunda"], "topics": [{"name": "tema"}]},
        {"ts": 2.0, "type": "question_asked", "text": "primera"},
        {"ts": 3.0, "type": "question_dismissed", "text": "segunda"},
        {"ts": 4.0, "type": "transcript_segment", "start_ms": 0,
         "end_ms": 1000, "speaker": 0, "text": "hola"},
    ]
    metrics = replay.sweep(events, {})[0]["metrics"]

    assert metrics["pending_questions"] == 0
    assert metrics["asked_questions"] == 1
    assert metrics["dismissed_questions"] == 1
    assert metrics["expired_questions"] == 0
    assert metrics["topic_count"] == 1
    assert metrics["transcript_lines"] == 1
    assert metrics["top_question_score"] == core.score_for("medium", 1)
    assert metrics["last_ts"] == 4.0


def test_sweep_forwards_arbitrary_kwargs_to_runner():
    calls = []
    sentinel = {
        "session": None, "reason": None, "first_ts": None, "last_ts": 9.0,
        "counts": {"batches": 0, "questions": 0, "topics": 0, "segments": 0},
        "transcript": [], "questions": [], "topics": [],
    }

    def runner(events, **kwargs):
        calls.append(dict(kwargs))
        return sentinel

    grid = {"alpha": [1, 2], "beta": ["x", "y"]}
    results = replay.sweep([{"type": "noop"}], grid, runner=runner)

    assert calls == [
        {"alpha": 1, "beta": "x"},
        {"alpha": 1, "beta": "y"},
        {"alpha": 2, "beta": "x"},
        {"alpha": 2, "beta": "y"},
    ]
    assert len(results) == 4
    assert results[0]["metrics"]["last_ts"] == 9.0


def test_sweep_empty_grid_yields_single_empty_combination():
    calls = []

    def runner(events, **kwargs):
        calls.append(dict(kwargs))
        return replay.reduce_events(events)

    results = replay.sweep([], {}, runner=runner)

    assert calls == [{}]
    assert len(results) == 1
    assert results[0]["params"] == {}
    assert results[0]["metrics"]["top_question_score"] is None
    assert results[0]["metrics"]["last_ts"] is None


def test_sweep_takes_injected_runner_and_reads_no_wall_clock():
    params = inspect.signature(replay.sweep).parameters
    assert params["runner"].default is replay.reduce_events

    source = inspect.getsource(replay)
    for token in ("time.time", "time.monotonic", "time.perf_counter",
                  "perf_counter", "datetime.now", "datetime.utcnow"):
        assert token not in source


def test_cli_sweep_emits_json_and_readable_table():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURE), "--sweep",
         '{"now": [1000, 2000]}', "--json"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(proc.stdout)
    assert [result["params"]["now"] for result in data] == [1000, 2000]
    assert all("metrics" in result for result in data)

    table = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURE), "--sweep",
         '{"now": [1000, 2000]}'],
        capture_output=True, text=True, check=True,
    )
    assert "now" in table.stdout
    assert "1000" in table.stdout and "2000" in table.stdout
    assert "pending_questions" in table.stdout
    assert "top_question_score" in table.stdout


def test_cli_rejects_malformed_sweep_grid():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURE), "--sweep", '{"now": 1000}'],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "--sweep" in proc.stderr


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


def _section(body, title):
    """Bullet lines under one `## title` heading, stopping at the next heading."""
    lines = body.splitlines()
    start = lines.index(title)
    bullets = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if line.strip():
            bullets.append(line)
    return bullets


def _iso_utc(ts):
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_knowledge_entry_from_fixture_is_turu_shaped():
    state = _reduce_fixture()
    entries = replay.knowledge_entries(state)

    assert len(entries) == 1
    entry = entries[0]
    assert set(entry) == {"format", "timestamp", "task", "body", "text"}
    assert entry["format"] == 1
    assert entry["timestamp"] == _iso_utc(state["last_ts"])
    assert entry["task"] == "interview-live interview-20261113-084517"
    assert entry["text"].startswith(
        f"Format: 1\nTimestamp: {entry['timestamp']}\nTask: {entry['task']}\n"
    )
    assert entry["body"] in entry["text"]

    pending = [q for q in state["questions"] if q["status"] == "pending"]
    expected = [
        f"- {q['text']} (mentions: {q['mentions']}, "
        f"priority: {q['priority']}, score: {q['score']})"
        for q in pending[:replay.KNOWLEDGE_TOP_QUESTIONS]
    ]
    assert _section(entry["body"], "## Pending questions") == expected
    assert _section(entry["body"], "## Session") == ["- reason: interrupted"]


def test_knowledge_ranks_topics_and_pending_questions():
    events = [
        {"ts": 100.0, "type": "session_start", "session": "s1", "mode": "live"},
        {"ts": 101.0, "type": "suggest_result",
         "questions": [
             {"text": "low q", "priority": "low"},
             {"text": "high q", "priority": "high"},
         ],
         "topics": [
             {"name": "beta", "priority": "low"},
             {"name": "alpha", "priority": "high"},
         ]},
        {"ts": 102.0, "type": "suggest_result", "questions": [],
         "topics": [{"name": "alpha", "priority": "high"}]},
        {"ts": 103.0, "type": "session_end", "reason": "completed"},
    ]
    entry = replay.knowledge_entries(replay.reduce_events(events))[0]

    topics = _section(entry["body"], "## Topics")
    assert topics[0].startswith("- alpha")
    assert topics[1].startswith("- beta")
    assert _section(entry["body"], "## Pending questions")[0].startswith("- high q")
    assert _section(entry["body"], "## Session") == ["- reason: completed"]
    assert entry["timestamp"] == _iso_utc(103.0)
    assert entry["task"] == "interview-live s1"


def test_knowledge_entries_are_byte_identical_across_runs():
    first = json.dumps(
        replay.knowledge_entries(_reduce_fixture()),
        sort_keys=True, ensure_ascii=False,
    )
    second = json.dumps(
        replay.knowledge_entries(_reduce_fixture()),
        sort_keys=True, ensure_ascii=False,
    )
    assert first == second
    assert replay.format_knowledge(replay.knowledge_entries(_reduce_fixture())) == \
        replay.format_knowledge(replay.knowledge_entries(_reduce_fixture()))


def test_knowledge_entries_read_only_state():
    params = inspect.signature(replay.knowledge_entries).parameters
    assert list(params) == ["state"]


def test_cli_knowledge_json_array_and_human_markdown():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURE), "--knowledge", "--json"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(proc.stdout)
    assert isinstance(data, list) and len(data) == 1
    assert set(data[0]) == {"format", "timestamp", "task", "body", "text"}
    assert data[0]["format"] == 1
    assert proc.stdout.strip().startswith("[") and proc.stdout.strip().endswith("]")

    human = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURE), "--knowledge"],
        capture_output=True, text=True, check=True,
    )
    assert human.stdout.startswith("Format: 1\nTimestamp: ")
    assert "Task: interview-live interview-20261113-084517" in human.stdout
    assert "## Topics" in human.stdout
    assert "## Pending questions" in human.stdout
    assert "reason: interrupted" in human.stdout


def test_cli_knowledge_empty_log_yields_no_entries(tmp_path):
    assert replay.knowledge_entries(replay.reduce_events([])) == []

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    as_json = subprocess.run(
        [sys.executable, str(SCRIPT), str(empty), "--knowledge", "--json"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(as_json.stdout) == []

    human = subprocess.run(
        [sys.executable, str(SCRIPT), str(empty), "--knowledge"],
        capture_output=True, text=True, check=True,
    )
    assert human.stdout == ""


def test_cli_knowledge_rejects_sweep_combination():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURE), "--knowledge", "--sweep", "{}"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "--knowledge" in proc.stderr