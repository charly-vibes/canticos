#!/usr/bin/env python3
"""Purpose: Pin the interview-live metrics CLI to the measured baseline session.
Responsibilities:
  - Assert golden values on the 20261113 slice fixture (batch/question/topic
    counts, topic-only share, median cadence, time-to-first-question)
  - Exercise lifecycle-derived acted-on and repair ratios, including the exact
    n/a strings when their source events are absent
  - Pin the --json key set and tolerate malformed/missing event lines
Rationale: canticos-e7a — tuning 17 suggestion parameters needs repeatable
  measurement, not ad-hoc heredocs. These tests make the metric contract
  (and the golden session slice) the regression anchor for replay tuning.
"""

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
SCRIPT = BIN / "interview_live_metrics.py"
FIXTURE = ROOT / "tests" / "fixtures" / "interview-20261113-slice.jsonl"

sys.path.insert(0, str(BIN))
import interview_live_metrics as m  # noqa: E402


def _write_events(tmp_path, events, extra_lines=()):
    path = tmp_path / "events.jsonl"
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    lines.extend(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_golden_fixture_values():
    events = m.load_events(FIXTURE)
    metrics = m.compute_metrics(events)

    assert metrics["batches"] == 31
    assert metrics["questions"] == 101
    assert metrics["topics"] == 44
    # Mean over the 27 batches that emitted at least one topic.
    assert metrics["question_topic_ratio_mean"] == pytest.approx(2.3333333333333335)
    assert metrics["question_topic_ratio_overall"] == pytest.approx(101 / 44)
    assert metrics["topic_only_batches"] == 0
    assert metrics["topic_only_pct"] == pytest.approx(0.0)
    assert metrics["median_cadence_seconds"] == pytest.approx(21.13028860092163)
    assert metrics["time_to_first_question_seconds"] == pytest.approx(27.433213472366333)
    assert metrics["questions_per_min"] == pytest.approx(1.209053399436663)
    assert metrics["session_duration_seconds"] == pytest.approx(5012.185568332672)
    assert metrics["over_contract_batches"] == 4  # one 5-question batch + three empty batches
    # No lifecycle / repair events exist in this slice.
    assert metrics["acted_on_ratio"] is None
    assert metrics["repair_rate"] is None


def test_human_output_golden_and_na_strings(tmp_path):
    events = m.load_events(FIXTURE)
    out = m.format_metrics(m.compute_metrics(events))

    assert "batches: 31" in out
    assert "questions: 101" in out
    assert "topics: 44" in out
    assert "questions:topics per batch (mean): 2.33" in out
    assert "questions:topics overall: 2.30" in out
    assert "topic-only batches: 0 (0.0%)" in out
    assert "questions/min: 1.21" in out
    assert "median batch cadence: 21.13 s" in out
    assert "time-to-first-question: 27.43 s" in out
    assert "over-contract batches: 4" in out
    assert "acted-on ratio: n/a (no lifecycle events)" in out
    assert "repair rate: n/a (no suggest_reject events)" in out


def test_acted_on_and_repair_ratios_from_lifecycle_events(tmp_path):
    events = [
        {"ts": 100.0, "seq": 1, "type": "session_start"},
        {"ts": 160.0, "seq": 2, "type": "suggest_result", "questions": ["a", "b", "c", "d"],
         "topics": [{"name": "t", "priority": "high", "note": ""}]},
        {"ts": 170.0, "seq": 3, "type": "question_shown", "key": "q1", "text": "a"},
        {"ts": 171.0, "seq": 4, "type": "question_shown", "key": "q2", "text": "b"},
        {"ts": 172.0, "seq": 5, "type": "question_asked", "key": "q1", "text": "a"},
        {"ts": 173.0, "seq": 6, "type": "question_dismissed", "key": "q2", "text": "b"},
        {"ts": 200.0, "seq": 7, "type": "suggest_reject", "reason": "parse"},
        {"ts": 201.0, "seq": 8, "type": "suggest_reject", "reason": "parse"},
        {"ts": 202.0, "seq": 9, "type": "suggest_repair", "reason": "retry"},
        {"ts": 260.0, "seq": 10, "type": "session_end"},
    ]
    metrics = m.compute_metrics(events)

    assert metrics["question_shown"] == 2
    assert metrics["question_asked"] == 1
    assert metrics["question_dismissed"] == 1
    assert metrics["acted_on_ratio"] == pytest.approx(1.0)
    assert metrics["suggest_reject"] == 2
    assert metrics["suggest_repair"] == 1
    assert metrics["repair_rate"] == pytest.approx(0.5)

    out = m.format_metrics(metrics)
    assert "acted-on ratio: 1.00" in out
    assert "repair rate: 0.50" in out

    no_repair = m.compute_metrics(
        [e for e in events if e["type"] not in ("suggest_reject", "suggest_repair")]
    )
    assert no_repair["acted_on_ratio"] == pytest.approx(1.0)
    assert no_repair["repair_rate"] is None
    assert "repair rate: n/a (no suggest_reject events)" in m.format_metrics(no_repair)


def test_json_mode_is_valid_and_keys_are_stable():
    run = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURE), "--json"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(run.stdout)
    expected_keys = {
        "events",
        "batches",
        "questions",
        "topics",
        "question_topic_ratio_batches",
        "question_topic_ratio_mean",
        "question_topic_ratio_overall",
        "topic_only_batches",
        "topic_only_pct",
        "questions_per_min",
        "median_cadence_seconds",
        "question_shown",
        "question_asked",
        "question_dismissed",
        "acted_on_ratio",
        "suggest_reject",
        "suggest_repair",
        "repair_rate",
        "time_to_first_question_seconds",
        "over_contract_batches",
        "session_duration_seconds",
    }
    assert set(data) == expected_keys
    assert data["batches"] == 31
    assert data["questions"] == 101
    assert data["question_topic_ratio_mean"] == pytest.approx(2.3333333333333335)
    assert data["acted_on_ratio"] is None
    assert data["repair_rate"] is None
    assert all(
        v is None or isinstance(v, (int, float, str)) for v in data.values()
    )


def test_malformed_and_missing_lines_do_not_crash(tmp_path):
    events = [
        {"ts": 100.0, "type": "session_start"},
        {"ts": 130.0, "type": "suggest_result", "questions": ["only one"], "topics": []},
        {"ts": "not-a-number", "type": "suggest_result", "questions": ["ignored ts"]},
        {"type": "suggest_result"},  # no ts, no payload fields
        {"ts": 140.0, "type": "suggest_result", "questions": "not-a-list", "topics": None},
        {"ts": 150.0, "type": "suggest_result", "questions": ["a", "b"], "topics": ["x"]},
        {"ts": 160.0, "type": "question_shown", "key": "q", "text": "a"},
        {"ts": 170.0, "type": "question_asked", "key": "q", "text": "a"},
        {"ts": 180.0, "type": "session_end"},
        {"ts": 10**1000, "type": "session_end"},  # float overflow, ignored
    ]
    extra = [
        "",
        "   ",
        "this is not json",
        "[1, 2, 3]",
        '"just a string"',
        "null",
    ]
    path = _write_events(tmp_path, events, extra_lines=extra)

    metrics = m.compute_metrics(m.load_events(path))
    assert metrics["batches"] == 5
    assert metrics["questions"] == 4
    assert metrics["topics"] == 1
    assert metrics["acted_on_ratio"] == pytest.approx(1.0)
    assert metrics["session_duration_seconds"] == pytest.approx(80.0)
    # Zero-duration / absent endpoints must degrade to None, never raise.
    assert m.compute_metrics([{"type": "session_start", "ts": 10.0}])["questions_per_min"] is None
    assert not math.isnan(metrics["topic_only_pct"])

    run = subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True, text=True, check=True,
    )
    assert "batches: 5" in run.stdout
    assert "n/a (no suggest_reject events)" in run.stdout

    empty = m.compute_metrics([])
    assert empty["batches"] == 0
    assert empty["questions_per_min"] is None
    assert empty["median_cadence_seconds"] is None
    assert empty["question_topic_ratio_mean"] is None
    assert "batches: 0" in m.format_metrics(empty)


def test_missing_file_exits_nonzero(tmp_path):
    run = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "nope.jsonl")],
        capture_output=True, text=True,
    )
    assert run.returncode != 0
    assert run.stderr