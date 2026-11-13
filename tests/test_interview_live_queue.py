#!/usr/bin/env python3
"""Purpose: Verify interview-live's queue merges Spanish paraphrase questions.
Responsibilities: Pin token-overlap merging in Queue._ingest (Jaccard >= 0.7 after
light es normalization): paraphrase pairs collapse into one pending item, distinct
questions survive, merged items keep the latest text and recurrence metadata, and
the exact-normalized-text dedupe path is unchanged.
Rationale: canticos-4pi — exact-key dedupe fragmented the queue under paraphrase
storms; interviewing copilot must surface one question per idea, not per wording.
"""

import json
import sys
from pathlib import Path

import pytest

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
# ---------------------------------------------------------------------------
# expiry policy (canticos-q9j, P-13 / P-14)
# ---------------------------------------------------------------------------

def _question(last_seen=1000.0, **over):
    q = {"key": "k", "text": "t", "priority": "medium", "mentions": 1,
         "added_at": last_seen, "last_seen": last_seen, "status": "pending",
         "shown": False, "score": 60}
    q.update(over)
    return q


def test_decay_weight_halves_every_half_life():
    """P-13: weight = 0.5 ** (age / 60s); future timestamps never boost."""
    q = _question()
    assert core.DECAY_HALF_LIFE_S == 60.0
    assert core.decay_weight(q, 1000.0) == 1.0
    assert core.decay_weight(q, 1060.0) == pytest.approx(0.5)
    assert core.decay_weight(q, 1120.0) == pytest.approx(0.25)
    assert core.decay_weight(q, 1000.0 - 30.0) == 1.0


def test_expiry_threshold_is_p14_and_expires_at_matches():
    """P-14: a question expires when its weight drops below 0.15; expires_at
    is the epoch second where that first happens."""
    assert core.EXPIRY_THRESHOLD == 0.15
    q = _question()
    at = core.expires_at(q)
    assert core.decay_weight(q, at) == pytest.approx(core.EXPIRY_THRESHOLD)
    assert core.decay_weight(q, at - 1.0) > core.EXPIRY_THRESHOLD
    assert core.decay_weight(q, at + 1.0) < core.EXPIRY_THRESHOLD


def test_expire_pending_orders_by_staleness_and_respects_boundary():
    q = core.Queue()
    q.ingest(questions=[{"text": "oldest"}], now=1000.0)
    q.ingest(questions=[{"text": "middle"}], now=1100.0)
    q.ingest(questions=[{"text": "newest"}], now=1200.0)
    oldest = q.questions[core.normalize_topic("oldest")]
    newest = q.questions[core.normalize_topic("newest")]
    boundary = core.expires_at(oldest)
    # still above threshold at the boundary (strictly below expires)
    assert q.expire_pending(now=boundary - 0.01) == []
    # past all three boundaries: reported most stale first
    expired = q.expire_pending(now=core.expires_at(newest) + 0.01)
    assert [e["text"] for e in expired] == ["oldest", "middle", "newest"]
    assert [e["status"] for e in expired] == ["expired"] * 3
    assert q.pending_questions() == []


def test_fresh_evidence_resets_decay_and_never_expires():
    """Anti-goal: decay tracks staleness, not existence — a re-mention bumps
    last_seen and rescues the question from expiry."""
    q = core.Queue()
    q.ingest(questions=[{"text": "Cuanto costo la multa"}], now=1000.0)
    q.ingest(questions=[{"text": "Cuanto costo la multa"}], now=1200.0)
    key = core.normalize_topic("Cuanto costo la multa")
    item = q.questions[key]
    assert item["last_seen"] == 1200.0
    assert q.expire_pending(now=1300.0) == []
    assert q.top_pending_question()["key"] == key
    assert core.decay_weight(item, 1300.0) > core.EXPIRY_THRESHOLD


def test_asked_dismissed_expired_never_resurface():
    q = core.Queue()
    q.ingest(questions=[{"text": "asked one"},
                        {"text": "dismissed one"},
                        {"text": "stale one"}], now=1000.0)
    q.mark_asked(core.normalize_topic("asked one"))
    q.mark_dismissed(core.normalize_topic("dismissed one"))
    expired = q.expire_pending(now=10**6)
    assert [e["text"] for e in expired] == ["stale one"]
    assert q.pending_questions() == [] and q.top_pending_question() is None
    # later evidence for the terminal states must not resurrect them
    q.ingest(questions=[{"text": "asked one"},
                        {"text": "dismissed one"},
                        {"text": "stale one"}], now=10**6 + 10)
    assert q.pending_questions() == []


def test_expire_pending_is_idempotent():
    q = core.Queue()
    q.ingest(questions=[{"text": "stale one"}], now=1000.0)
    first = q.expire_pending(now=10**6)
    assert len(first) == 1 and first[0]["status"] == "expired"
    assert q.expire_pending(now=10**6 + 100) == []
    assert q.questions[core.normalize_topic("stale one")]["status"] == "expired"


def test_snapshot_carries_decay_weight_and_expires_at():
    """queue.json annotates pending questions tolerantly; the stored queue
    stays pure (no snapshot-only keys leak into the live dicts)."""
    q = core.Queue()
    q.ingest(questions=[{"text": "Q1"}], now=1000.0)
    key = core.normalize_topic("Q1")
    snap = q.snapshot(participants=2, now=1060.0)
    item = snap["questions"][0]
    assert item["decay_weight"] == pytest.approx(0.5)
    assert item["expires_at"] == pytest.approx(core.expires_at(q.questions[key]))
    assert json.loads(json.dumps(snap, ensure_ascii=False))["questions"][0]["key"] == key
    assert "decay_weight" not in q.questions[key]
