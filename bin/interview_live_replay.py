#!/usr/bin/env python3
"""Purpose: Deterministic replay of an interview-live events.jsonl into final state.
Responsibilities:
  - Load the append-only event log tolerantly (blank/malformed/non-object lines
    are skipped), like the metrics CLI does
  - Fold transcript segments, suggestion batches, and question lifecycle events
    into one JSON-serializable state; queue state is delegated to core.Queue
  - Expose a pure reducer with an injected clock: no wall-clock reads, so two
    replays of the same log are byte-identical
  - Render the final state through a thin CLI (pretty JSON by default, compact
    canonical JSON with --json)
  - Sweep a parameter grid over the reducer, forwarding each combination as
    keyword arguments and reporting state-derived metrics in sorted order
  - Derive one turu-format knowledge entry per session from the reduced state
    (no clock reads, no file writes), rendered by --knowledge / --knowledge
    --json
Rationale: canticos-qes — tuning suggestion parameters requires replaying a
  recorded session and comparing final state, not eyeballing a live dashboard.
  All freshness timestamps come from event ts values (or the caller-supplied
  `now` fallback); the reducer never reads the system clock, which is what makes
  the state a deterministic function of the log. Lifecycle replay uses the same
  Queue transitions as the TUI, so asked/dismissed/shown statuses cannot drift
  between live rendering and replay.
"""

import argparse
from datetime import datetime, timezone
import itertools
import json
import math
import sys
from pathlib import Path

import interview_live_core as core

LIFECYCLE_TYPES = ("question_shown", "question_asked", "question_dismissed")

# Caps keep one ledger entry readable; ranking is already deterministic, so the
# truncation point is stable across replays.
KNOWLEDGE_TOP_TOPICS = 5
KNOWLEDGE_TOP_QUESTIONS = 3


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
    """Finite numeric ts, else None. Bools, strings, and non-finite values are
    malformed, matching the metrics CLI's definition."""
    ts = event.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        ts = float(ts)
    except (OverflowError, ValueError):
        return None
    return ts if math.isfinite(ts) else None


def _payload(event, key):
    value = event.get(key)
    return value if isinstance(value, list) else []


def _question_key(event):
    """Lifecycle identity: the emitted key, else the normalized text (the same
    key Queue._ingest derives)."""
    key = event.get("key")
    if isinstance(key, str) and key.strip():
        return key
    text = event.get("text")
    return core.normalize_topic(text) if isinstance(text, str) else ""


def _transcript_entry(event):
    """One transcript line, preserving the event's fields and display order."""
    start_ms = event.get("start_ms")
    speaker = event.get("speaker")
    text = event.get("text")
    text = text if isinstance(text, str) else ""
    line = None
    if isinstance(start_ms, (int, float)) and not isinstance(start_ms, bool) \
            and speaker is not None:
        line = core.render_segment_line(start_ms, speaker, text, {})
    return {
        "start_ms": start_ms,
        "end_ms": event.get("end_ms"),
        "speaker": speaker,
        "text": text,
        "line": line,
    }


def reduce_events(events, now=None):
    """Fold event dicts into a deterministic final state.

    `now` is the injected clock used only when an event that feeds the queue has
    no usable ts. It is a parameter (not a wall-clock read) so callers can pass
    a frozen value; omitted, it falls back to the last timestamped event (then
    to 0.0), keeping repeated replays byte-identical. Events are valid dicts;
    anything else is ignored.
    """
    queue = core.Queue()
    transcript = []
    counts = {"batches": 0, "questions": 0, "topics": 0, "segments": 0}
    first_ts = None
    last_ts = None
    session = {}
    reason = None

    for event in events:
        if not isinstance(event, dict):
            continue

        ts = _timestamp(event)
        if ts is not None:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        etype = event.get("type")
        if etype == "transcript_segment":
            counts["segments"] += 1
            transcript.append(_transcript_entry(event))
        elif etype == "suggest_result":
            questions = _payload(event, "questions")
            topics = _payload(event, "topics")
            counts["batches"] += 1
            counts["questions"] += len(questions)
            counts["topics"] += len(topics)
            stamp = ts if ts is not None else (now if now is not None else (last_ts or 0.0))
            queue.ingest(questions=questions, topics=topics, now=stamp)
        elif etype in LIFECYCLE_TYPES:
            key = _question_key(event)
            if not key:
                continue
            if etype == "question_shown":
                queue.mark_shown(key)
            elif etype == "question_asked":
                queue.mark_asked(key)
            else:
                queue.mark_dismissed(key)
        elif etype == "session_start":
            for name in ("mode", "participants", "pi_model", "out"):
                if name in event:
                    session[name] = event[name]
            session["id"] = event.get("session")
        elif etype == "session_end":
            reason = event.get("reason")

    return {
        "session": session or None,
        "reason": reason,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "counts": counts,
        "transcript": transcript,
        "questions": queue.ranked_questions(),
        "topics": queue.ranked_topics(),
    }


SWEEP_METRICS = (
    "pending_questions",
    "expired_questions",
    "dismissed_questions",
    "asked_questions",
    "topic_count",
    "transcript_lines",
    "top_question_score",
    "last_ts",
)


def state_metrics(state):
    """Per-run metrics derived only from the reducer's returned state."""
    questions = state.get("questions")
    questions = questions if isinstance(questions, list) else []
    statuses = [q.get("status") for q in questions if isinstance(q, dict)]

    def count(status):
        return sum(1 for value in statuses if value == status)

    top = questions[0] if questions else None
    return {
        "pending_questions": count("pending"),
        "expired_questions": count("expired"),
        "dismissed_questions": count("dismissed"),
        "asked_questions": count("asked"),
        "topic_count": len(state.get("topics") or []),
        "transcript_lines": len(state.get("transcript") or []),
        "top_question_score": top.get("score") if isinstance(top, dict) else None,
        "last_ts": state.get("last_ts"),
    }


def _order_key(value):
    """Total order over grid values: numbers, strings, then JSON fallback."""
    if value is None:
        return (0, 0, "")
    if isinstance(value, bool):
        return (1, int(value), "")
    if isinstance(value, (int, float)):
        return (2, value, "")
    if isinstance(value, str):
        return (3, 0, value)
    return (4, 0, json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr))


def sweep(events, grid, runner=reduce_events):
    """Run `runner` once per grid combination and derive per-run metrics.

    `grid` maps parameter names to candidate values; every combination is passed
    to `runner` as keyword arguments, so tuning parameters are injected instead
    of patched into globals. `grid` key order defines the parameter tuple, and
    results are sorted by that tuple, making repeated sweeps byte-identical.
    """
    names = list(grid)
    combos = itertools.product(*(grid[name] for name in names)) if names else [()]
    results = []
    for combo in combos:
        params = dict(zip(names, combo))
        results.append({
            "params": params,
            "metrics": state_metrics(runner(events, **params)),
        })
    results.sort(key=lambda result: tuple(
        _order_key(result["params"][name]) for name in names
    ))
    return results


def _display(value):
    if value is None:
        return "null"
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr)


def format_sweep(results):
    """Fixed-width table: one row per combination, parameter columns first."""
    param_names = list(results[0]["params"]) if results else []
    headers = param_names + list(SWEEP_METRICS)
    rows = []
    for result in results:
        row = [_display(result["params"].get(name)) for name in param_names]
        row += [_display(result["metrics"].get(name)) for name in SWEEP_METRICS]
        rows.append(row)
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)).rstrip()
    ]
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def _number(value):
    """Finite numeric value, else 0.0; malformed state never breaks ranking."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _ranked(items, label):
    """Score-ranked view: score desc, recency desc, label asc.

    Sorting here (rather than trusting list order) keeps entries deterministic
    even when a caller hands in a hand-built state.
    """
    if not isinstance(items, list):
        return []

    def label_of(item):
        for field in (label, "key"):
            value = item.get(field)
            if isinstance(value, str) and value:
                return value
        return ""

    rows = [item for item in items if isinstance(item, dict)]
    return sorted(rows, key=lambda item: (
        -_number(item.get("score")),
        -_number(item.get("last_seen")),
        label_of(item),
    ))


def _iso_utc(ts):
    """ISO-8601 UTC string (second precision) from an epoch timestamp, or None.

    Conversion is explicit UTC, so the local timezone never leaks into output.
    """
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        seconds = float(ts)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    try:
        moment = datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _knowledge_bullet(item, label):
    title = item.get(label)
    if not (isinstance(title, str) and title.strip()):
        title = item.get("key")
    title = title.strip() if isinstance(title, str) else ""
    mentions = item.get("mentions")
    if isinstance(mentions, bool) or not isinstance(mentions, int):
        mentions = 1
    priority = item.get("priority")
    if not (isinstance(priority, str) and priority):
        priority = "medium"
    score = item.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        score = 0
    return f"- {title} (mentions: {mentions}, priority: {priority}, score: {score})"


def _knowledge_body(state, topics, pending):
    """Markdown body: top topics, strongest pending questions, session reason."""
    reason = state.get("reason")
    reason = reason if isinstance(reason, str) and reason else "none"
    lines = ["## Topics"]
    lines += [_knowledge_bullet(topic, "name") for topic in topics] or ["- none"]
    lines += ["", "## Pending questions"]
    lines += [_knowledge_bullet(q, "text") for q in pending] or ["- none"]
    lines += ["", "## Session", f"- reason: {reason}"]
    return "\n".join(lines)


def knowledge_entries(state):
    """One turu-format ledger entry per session in the reduced state.

    The timestamp is derived from `state["last_ts"]` (event time), never the
    wall clock, so entries are a pure function of the log. A state without a
    session (including an empty log) yields no entries.
    """
    if not isinstance(state, dict):
        return []
    session = state.get("session")
    if not isinstance(session, dict):
        return []
    session_id = session.get("id")
    if not (isinstance(session_id, str) and session_id):
        session_id = "unknown"

    topics = _ranked(state.get("topics"), "name")[:KNOWLEDGE_TOP_TOPICS]
    pending = [
        question
        for question in _ranked(state.get("questions"), "text")
        if question.get("status") == "pending"
    ][:KNOWLEDGE_TOP_QUESTIONS]

    body = _knowledge_body(state, topics, pending)
    timestamp = _iso_utc(state.get("last_ts"))
    header = ["Format: 1"]
    if timestamp is not None:
        header.append(f"Timestamp: {timestamp}")
    task = f"interview-live {session_id}"
    header.append(f"Task: {task}")
    return [{
        "format": 1,
        "timestamp": timestamp,
        "task": task,
        "body": body,
        "text": "\n".join(header) + "\n\n" + body,
    }]


def format_knowledge(entries):
    """Human-readable markdown: each entry is its full turu block."""
    return "\n\n".join(entry["text"] for entry in entries)


def dump_state(state, compact=False):
    """Canonical JSON: sorted keys and UTF-8 text make replays byte-comparable."""
    return json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if compact else None,
        indent=None if compact else 2,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="interview-live-replay",
        description="Fold an interview-live events.jsonl into its final state.",
    )
    parser.add_argument("events", help="path to the session events.jsonl")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit compact canonical JSON (default: indented JSON)",
    )
    parser.add_argument(
        "--sweep",
        metavar="GRID_JSON",
        help="run a parameter grid, e.g. '{\"now\": [1000, 2000]}'",
    )
    parser.add_argument(
        "--knowledge",
        action="store_true",
        help="emit turu-format knowledge entries for the reduced session",
    )
    args = parser.parse_args(argv)

    try:
        events = load_events(args.events)
    except OSError as exc:
        print(f"interview-live-replay: {exc}", file=sys.stderr)
        return 1

    if args.knowledge:
        if args.sweep is not None:
            print(
                "interview-live-replay: --knowledge and --sweep are mutually "
                "exclusive",
                file=sys.stderr,
            )
            return 2
        entries = knowledge_entries(reduce_events(events))
        if args.json:
            print(json.dumps(
                entries,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ))
        else:
            rendered = format_knowledge(entries)
            if rendered:
                print(rendered)
        return 0

    if args.sweep is not None:
        try:
            grid = json.loads(args.sweep)
        except (json.JSONDecodeError, ValueError) as exc:
            print(
                f"interview-live-replay: invalid --sweep JSON: {exc}",
                file=sys.stderr,
            )
            return 2
        if not isinstance(grid, dict) or any(
            not isinstance(values, (list, tuple)) for values in grid.values()
        ):
            print(
                "interview-live-replay: --sweep must be a JSON object mapping "
                "parameter names to value lists",
                file=sys.stderr,
            )
            return 2
        results = sweep(events, grid)
        if args.json:
            print(json.dumps(
                results,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ))
        else:
            print(format_sweep(results))
        return 0

    state = reduce_events(events)
    print(dump_state(state, compact=args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())