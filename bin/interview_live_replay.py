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
Rationale: canticos-qes — tuning suggestion parameters requires replaying a
  recorded session and comparing final state, not eyeballing a live dashboard.
  All freshness timestamps come from event ts values (or the caller-supplied
  `now` fallback); the reducer never reads the system clock, which is what makes
  the state a deterministic function of the log. Lifecycle replay uses the same
  Queue transitions as the TUI, so asked/dismissed/shown statuses cannot drift
  between live rendering and replay.
"""

import argparse
import json
import math
import sys
from pathlib import Path

import interview_live_core as core

LIFECYCLE_TYPES = ("question_shown", "question_asked", "question_dismissed")


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
    args = parser.parse_args(argv)

    try:
        events = load_events(args.events)
    except OSError as exc:
        print(f"interview-live-replay: {exc}", file=sys.stderr)
        return 1

    state = reduce_events(events)
    print(dump_state(state, compact=args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())