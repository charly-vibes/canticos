#!/usr/bin/env python3
"""Purpose: Repeatable health metrics for interview-live sessions (canticos-e7a).
Responsibilities:
  - Load events.jsonl tolerantly (blank/malformed/non-object lines are skipped)
  - Compute pure metrics from event dicts: batch and payload counts,
    questions:topics ratios, no-question and topic-only shares, questions/min,
    median cadence, acted-on and repair ratios, time-to-first-question,
    over-contract batches
  - Render a human report or stable-key JSON through a thin CLI
Rationale: tuning the suggest pipeline's parameters needs a measurement loop,
  not a one-off heredoc. Metrics stay pure over event dicts so replay tests can
  assert them directly; lifecycle/repair events are counted by `type` even
  though their emitters land in separate tickets, so this tool does not depend
  on interview-live. Ratios are None when their denominator is absent, and the
  human report spells out why instead of printing NaN or infinity. Per-batch
  questions:topics means only batches that produced a topic (a zero-topic batch
  has no finite ratio); overall ratio uses the session totals.
"""

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

CONTRACT_MAX_QUESTIONS = 4


def load_events(path):
    """Read a JSONL event log, skipping blank, malformed, and non-object lines."""
    events = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def _timestamp(event):
    """Finite numeric ts, else None. Bools, strings, and huge ints are malformed."""
    ts = event.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        ts = float(ts)
    except (OverflowError, ValueError):
        return None
    return ts if math.isfinite(ts) else None


def _payload_count(event, key):
    value = event.get(key)
    return len(value) if isinstance(value, list) else 0


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def compute_metrics(events, path=None):
    """Pure metrics over event dicts; `path` is echoed only for JSON provenance."""
    batches = []
    session_starts = []
    session_ends = []
    lifecycle = {
        "question_shown": 0,
        "question_asked": 0,
        "question_dismissed": 0,
        "suggest_reject": 0,
        "suggest_repair": 0,
    }

    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "suggest_result":
            batches.append((
                _timestamp(event),
                _payload_count(event, "questions"),
                _payload_count(event, "topics"),
            ))
        elif etype == "session_start":
            ts = _timestamp(event)
            if ts is not None:
                session_starts.append(ts)
        elif etype == "session_end":
            ts = _timestamp(event)
            if ts is not None:
                session_ends.append(ts)
        elif etype in lifecycle:
            lifecycle[etype] += 1

    questions = sum(q for _, q, _ in batches)
    # Real baseline (20261113-084517): 631 questions over 222 batches. The older
    # 628 figure came from a different parser; never hard-code it here.
    topics = sum(t for _, _, t in batches)
    batch_count = len(batches)

    ratio_batches = [q / t for _, q, t in batches if t]
    question_topic_ratio_mean = (
        statistics.fmean(ratio_batches) if ratio_batches else None
    )
    question_topic_ratio_overall = _ratio(questions, topics)

    no_question = sum(1 for _, q, _ in batches if q == 0)
    no_question_pct = (no_question / batch_count * 100.0) if batch_count else 0.0
    topic_only = sum(1 for _, q, t in batches if q == 0 and t > 0)
    topic_only_pct = (topic_only / batch_count * 100.0) if batch_count else 0.0
    over_contract = sum(1 for _, q, _ in batches if q == 0 or q > CONTRACT_MAX_QUESTIONS)

    start_ts = min(session_starts) if session_starts else None
    end_ts = max(session_ends) if session_ends else None
    duration = None
    if start_ts is not None and end_ts is not None and end_ts > start_ts:
        duration = end_ts - start_ts
    questions_per_min = _ratio(questions, duration / 60.0) if duration else None

    batch_ts = sorted(ts for ts, _, _ in batches if ts is not None)
    deltas = [b - a for a, b in zip(batch_ts, batch_ts[1:])]
    median_cadence = statistics.median(deltas) if deltas else None

    first_question = batch_ts[0] if batch_ts else None
    time_to_first_question = (
        first_question - start_ts
        if first_question is not None and start_ts is not None
        else None
    )

    acted_on_ratio = _ratio(
        lifecycle["question_asked"] + lifecycle["question_dismissed"],
        lifecycle["question_shown"],
    )
    repair_rate = _ratio(lifecycle["suggest_repair"], lifecycle["suggest_reject"])

    return {
        "events": str(path) if path is not None else None,
        "batches": batch_count,
        "questions": questions,
        "topics": topics,
        "question_topic_ratio_batches": len(ratio_batches),
        "question_topic_ratio_mean": question_topic_ratio_mean,
        "question_topic_ratio_overall": question_topic_ratio_overall,
        "no_question_batches": no_question,
        "no_question_pct": no_question_pct,
        "topic_only_batches": topic_only,
        "topic_only_pct": topic_only_pct,
        "questions_per_min": questions_per_min,
        "median_cadence_seconds": median_cadence,
        "question_shown": lifecycle["question_shown"],
        "question_asked": lifecycle["question_asked"],
        "question_dismissed": lifecycle["question_dismissed"],
        "acted_on_ratio": acted_on_ratio,
        "suggest_reject": lifecycle["suggest_reject"],
        "suggest_repair": lifecycle["suggest_repair"],
        "repair_rate": repair_rate,
        "time_to_first_question_seconds": time_to_first_question,
        "over_contract_batches": over_contract,
        "session_duration_seconds": duration,
    }


def _num(value, places=2):
    return "n/a" if value is None else f"{value:.{places}f}"


def format_metrics(metrics):
    """Human report; n/a strings name the missing source rather than lying."""
    lines = []
    if metrics.get("events") is not None:
        lines.append(f"events: {metrics['events']}")
    lines.extend([
        f"batches: {metrics['batches']}",
        f"questions: {metrics['questions']}",
        f"topics: {metrics['topics']}",
        f"questions:topics per batch (mean): {_num(metrics['question_topic_ratio_mean'])}"
        f" ({metrics['question_topic_ratio_batches']} batches with topics)",
        f"questions:topics overall: {_num(metrics['question_topic_ratio_overall'])}",
        f"no-question batches: {metrics['no_question_batches']} "
        f"({metrics['no_question_pct']:.1f}%)",
        f"topic-only batches: {metrics['topic_only_batches']} "
        f"({metrics['topic_only_pct']:.1f}%)",
    ])
    if metrics["questions_per_min"] is None:
        lines.append("questions/min: n/a (missing session_start/session_end)")
    else:
        lines.append(f"questions/min: {_num(metrics['questions_per_min'])}")
    if metrics["median_cadence_seconds"] is None:
        lines.append("median batch cadence: n/a (fewer than 2 timestamped batches)")
    else:
        lines.append(f"median batch cadence: {_num(metrics['median_cadence_seconds'])} s")
    if metrics["acted_on_ratio"] is None:
        lines.append("acted-on ratio: n/a (no lifecycle events)")
    else:
        lines.append(f"acted-on ratio: {_num(metrics['acted_on_ratio'])}")
    if metrics["repair_rate"] is None:
        lines.append("repair rate: n/a (no suggest_reject events)")
    else:
        lines.append(f"repair rate: {_num(metrics['repair_rate'])}")
    if metrics["time_to_first_question_seconds"] is None:
        lines.append("time-to-first-question: n/a (missing session_start or batches)")
    else:
        lines.append(
            f"time-to-first-question: "
            f"{_num(metrics['time_to_first_question_seconds'])} s"
        )
    lines.append(f"over-contract batches: {metrics['over_contract_batches']}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="interview-live-metrics",
        description="Report interview-live session health metrics from events.jsonl.",
    )
    parser.add_argument("events", help="path to the session events.jsonl")
    parser.add_argument("--json", action="store_true", help="emit stable-key JSON")
    args = parser.parse_args(argv)

    try:
        events = load_events(args.events)
    except OSError as exc:
        print(f"interview-live-metrics: {exc}", file=sys.stderr)
        return 1

    metrics = compute_metrics(events, path=Path(args.events))
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(format_metrics(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())