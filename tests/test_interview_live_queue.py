#!/usr/bin/env python3
"""Purpose: Verify interview-live's queue merges Spanish paraphrase questions.
Responsibilities: Pin token-overlap merging in Queue._ingest (Jaccard >= 0.7 after
light es normalization): paraphrase pairs collapse into one pending item, distinct
questions survive, merged items keep the latest text and recurrence metadata, and
the exact-normalized-text dedupe path is unchanged.
Rationale: canticos-4pi — exact-key dedupe fragmented the queue under paraphrase
storms; interviewing copilot must surface one question per idea, not per wording.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import interview_live_core as core  # noqa: E402

PARAPHRASE_A = "¿Cuánto pagaste por la multa?"
PARAPHRASE_B = "Cuanto pagaste por esa multa?"
DISTINCT = "¿Cuánto cuesta la multa?"


def test_spanish_paraphrase_merges():
    q = core.Queue()
    q.ingest(questions=[{"text": PARAPHRASE_A, "priority": "medium"}], now=1000.0)
    q.ingest(questions=[{"text": PARAPHRASE_B, "priority": "medium"}], now=1100.0)
    assert len(q.questions) == 1
    item = next(iter(q.questions.values()))
    assert item["mentions"] == 2


def test_distinct_question_survives():
    q = core.Queue()
    q.ingest(questions=[{"text": PARAPHRASE_A, "priority": "medium"}], now=1000.0)
    q.ingest(questions=[{"text": DISTINCT, "priority": "medium"}], now=1100.0)
    assert len(q.questions) == 2
    assert core.jaccard_score(PARAPHRASE_A, DISTINCT) < core.JACCARD_MERGE


def test_merge_keeps_latest_text_and_priority():
    q = core.Queue()
    first_key = core.normalize_topic(PARAPHRASE_A)
    q.ingest(questions=[{"text": PARAPHRASE_A, "priority": "low"}], now=1000.0)
    q.ingest(questions=[{"text": PARAPHRASE_B, "priority": "high"}], now=1200.0)
    item = q.questions[first_key]
    # latest variant wins as display text, but the key stays the original
    assert item["text"] == PARAPHRASE_B
    assert item["key"] == first_key
    assert list(q.questions) == [first_key]
    assert item["mentions"] == 2
    assert item["added_at"] == 1000.0
    assert item["last_seen"] == 1200.0
    assert item["priority"] == "high"

# exact duplicates take the pre-existing key path, not the fuzzy one
def test_exact_duplicate_still_dedupes():
    q = core.Queue()
    q.ingest(questions=[{"text": PARAPHRASE_A, "priority": "medium"}], now=1000.0)
    q.ingest(questions=[{"text": PARAPHRASE_A.upper(), "priority": "medium"}], now=1100.0)
    key = core.normalize_topic(PARAPHRASE_A)
    assert list(q.questions) == [key]
    assert q.questions[key]["mentions"] == 2


def test_threshold_constant_imported_and_used():
    assert core.JACCARD_MERGE == 0.7
    assert core.jaccard_score(PARAPHRASE_A, PARAPHRASE_B) >= core.JACCARD_MERGE
    assert core.jaccard_score(PARAPHRASE_A, DISTINCT) < core.JACCARD_MERGE


def test_asked_question_does_not_absorb_new_variant():
    q = core.Queue()
    q.ingest(questions=[{"text": PARAPHRASE_A, "priority": "medium"}], now=1000.0)
    q.mark_asked(core.normalize_topic(PARAPHRASE_A))
    q.ingest(questions=[{"text": PARAPHRASE_B, "priority": "medium"}], now=1100.0)
    # asked items keep their status; the new wording becomes its own pending item
    assert len(q.questions) == 2
    assert q.questions[core.normalize_topic(PARAPHRASE_A)]["status"] == "asked"
    assert q.top_pending_question()["text"] == PARAPHRASE_B