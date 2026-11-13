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


# ---------------------------------------------------------------------------
# learning goals + threads (canticos-47b)
# ---------------------------------------------------------------------------

def test_slot_model_default_mom_test_config_is_deterministic():
    model = core.SlotModel()
    assert [s["id"] for s in model.slots] == [
        "past_event", "cost", "workaround", "decision", "outcome"]
    assert [s["weight"] for s in model.slots] == [5, 4, 3, 2, 2]
    assert model.classify("¿Cuándo fue la última vez que pasó?") == "past_event"
    assert model.classify("¿Cuánto costó la multa?") == "cost"
    assert model.classify("¿Qué hiciste para evitar la multa?") == "workaround"
    assert model.classify("¿Por qué decidiste cambiar de proveedor?") == "decision"
    assert model.classify("¿Qué resultado tuvo la reunión?") == "outcome"
    assert model.classify("hola, buenos días") is None


def test_slot_model_custom_config_assigns_goal_ids():
    model = core.SlotModel([
        {"id": "pain", "label": "Pain", "weight": 2, "keywords": ["dolor"]},
        {"id": "money", "label": "Money", "weight": 9, "keywords": ["dinero", "coste"]},
    ])
    assert [s["id"] for s in model.slots] == ["pain", "money"]
    assert model.classify("¿Cuánto dinero costó?") == "money"
    assert model.classify("dolor de espalda") == "pain"
    questions = model.assign([
        {"text": "¿Cuánto dinero costó?"},
        {"text": "sin pistas", "goal_id": "pain"},
        {"text": "otra cosa"},
    ])
    # explicit tags survive; keyword matches fill the rest deterministically
    assert [q.get("goal_id") for q in questions] == ["money", "pain", None]


def test_slot_model_coverage_state_is_ordered_and_marks_stale_goals():
    model = core.SlotModel(stale_s=900.0)
    covered = model.coverage_state(
        [{"text": "cuanto costo", "last_seen": 1000.0}], now=1899.0)
    assert list(covered) == [s["id"] for s in model.slots]  # config order
    assert covered["cost"]["covered"] is True and covered["cost"]["stale"] is False
    assert covered["cost"]["question_count"] == 1
    assert covered["cost"]["age"] == pytest.approx(899.0)
    # the 900s boundary is stale (>= 900s without a question)
    stale = model.coverage_state(
        [{"text": "cuanto costo", "last_seen": 1000.0}], now=1900.0)
    assert stale["cost"]["stale"] is True and stale["cost"]["covered"] is False
    never = model.coverage_state([], now=1000.0)
    assert all(rec["stale"] for rec in never.values())
    assert never["cost"]["last_seen"] is None


def test_goal_gap_ranking_differential_uncovered_heavy_beats_covered_light():
    slots = core.SlotModel([
        {"id": "heavy", "label": "Heavy goal", "weight": 10, "keywords": ["heavy"]},
        {"id": "light", "label": "Light goal", "weight": 1, "keywords": ["light"]},
    ])
    q = core.Queue(slots=slots)
    q.ingest([{"text": "light question one"}], now=1000.0)
    q.ingest([{"text": "light question two"},
              {"text": "heavy question one"}], now=1100.0)
    live = list(q.questions.values())
    heavy = q.questions[core.normalize_topic("heavy question one")]
    light = q.questions[core.normalize_topic("light question two")]
    assert slots.goal_gap(heavy, live, now=1100.0) == 10
    assert slots.goal_gap(light, live, now=1100.0) == 0
    ranked = [item["text"] for item in q.pending_questions(now=1100.0)]
    assert ranked[0] == "heavy question one"
    # deterministic and stable: repeating the call never reshuffles ties
    assert ranked == [item["text"] for item in q.pending_questions(now=1100.0)]
    # a second heavy touch clears the bonus (both heavy questions are recent)
    q.ingest([{"text": "heavy question two"}], now=1105.0)
    live = list(q.questions.values())
    assert slots.goal_gap(heavy, live, now=1105.0) == 0


def test_ranking_without_goal_model_is_unchanged():
    plain = core.Queue()
    assert plain.slots is None
    plain.ingest([{"text": "low one", "priority": "low"},
                  {"text": "high one", "priority": "high"}], now=1000.0)
    assert [item["text"] for item in plain.pending_questions(now=1000.0)] == [
        "high one", "low one"]


def test_thread_tracker_counts_batches_and_never_resurrects_closed():
    tracker = core.ThreadTracker()
    first = tracker.touch("Pricing pain", now=1000.0)
    second = tracker.touch("Pricing pain", now=1300.0)
    assert first["state"] == "open" and second["state"] == "open"
    assert second["key"] == core.normalize_topic("Pricing pain")
    assert second["first_seen"] == 1000.0 and second["last_seen"] == 1300.0
    assert second["question_count"] == 2
    tracker.close("Pricing pain", now=1400.0)
    third = tracker.touch("Pricing pain", now=1500.0)
    assert third["state"] == "closed", "touch never resurrects a closed thread"
    snap = tracker.snapshot(now=1500.0)
    assert snap[0]["state"] == "closed"
    assert snap[0]["question_count"] == 3
    assert snap[0]["stale"] is False


def test_ingest_annotates_questions_with_thread_and_goal_refs():
    tracker = core.ThreadTracker()
    q = core.Queue(threads=tracker, slots=core.SlotModel())
    q.ingest([{"text": "¿Cuánto costó la multa?"}], now=1000.0)
    key = core.normalize_topic("¿Cuánto costó la multa?")
    item = q.questions[key]
    assert item["thread"] == key
    assert item["goal_id"] == "cost"
    q.ingest([{"text": "¿Cuánto costó la multa?"}], now=1100.0)
    assert q.questions[key]["mentions"] == 2
    assert tracker.snapshot(now=1100.0)[0]["question_count"] == 2


def test_snapshot_includes_threads_and_goals_only_when_configured():
    bare = core.Queue()
    bare.ingest([{"text": "Q1"}], now=1000.0)
    bare_snap = bare.snapshot(now=1000.0)
    assert "threads" not in bare_snap and "goals" not in bare_snap

    q = core.Queue(threads=core.ThreadTracker(), slots=core.SlotModel())
    q.ingest([{"text": "¿Cuánto costó la multa?"}], now=1000.0)
    snap = q.snapshot(now=1000.0)
    assert [g["id"] for g in snap["goals"]] == [s["id"] for s in core.MOM_TEST_SLOTS]
    assert snap["goals"][1]["id"] == "cost" and snap["goals"][1]["covered"] is True
    assert snap["threads"][0]["question_count"] == 1
    question = snap["questions"][0]
    assert question["thread"] == core.normalize_topic("¿Cuánto costó la multa?")
    assert question["goal_id"] == "cost"


# ---------------------------------------------------------------------------
# topics as context + topic-to-question synthesis (canticos-6ic)
# ---------------------------------------------------------------------------

def test_topic_with_one_mention_does_not_synthesize_a_question():
    q = core.Queue()
    q.ingest(topics=[{"name": "Proveedor", "priority": "high", "note": ""}],
             now=1000.0)
    assert q.pending_questions(now=1000.0) == []
    assert q.topic_question_count("proveedor") == 0
    assert not q.has_question("proveedor")


def test_recurring_topic_synthesizes_one_deterministic_candidate():
    q = core.Queue()
    q.ingest(topics=[{"name": "Proveedor", "priority": "high", "note": ""}],
             now=1000.0)
    q.ingest(topics=[{"name": "Proveedor", "priority": "high", "note": ""}],
             now=1100.0)
    synth = [item for item in q.pending_questions(now=1100.0)
             if item.get("source") == core.TOPIC_SYNTH_SOURCE]
    assert len(synth) == 1
    item = synth[0]
    assert item["text"] == core.topic_question_text("Proveedor")
    assert item["priority"] == "low"
    assert item["evidence"] == "no-evidence"
    assert item["thread"] == "proveedor"
    assert q.topic_question_count("proveedor") == 1
    assert q.has_question("proveedor") is True
    # recurring evidence never creates a second candidate for the same topic
    q.ingest(topics=[{"name": "Proveedor", "priority": "high", "note": ""}],
             now=1200.0)
    assert len([item for item in q.questions.values()
                if item.get("source") == core.TOPIC_SYNTH_SOURCE]) == 1


def test_existing_question_blocks_topic_synthesis():
    q = core.Queue()
    q.ingest(questions=[{"text": "Cuentame del problema del proveedor",
                         "thread": "proveedor", "priority": "high"}],
             topics=[{"name": "Proveedor", "priority": "high", "note": ""}],
             now=1000.0)
    q.ingest(topics=[{"name": "Proveedor", "priority": "high", "note": ""}],
             now=1100.0)
    assert q.topic_question_count("proveedor") == 1
    assert not [item for item in q.questions.values()
                if item.get("source") == core.TOPIC_SYNTH_SOURCE]


def test_synth_candidate_never_outranks_grounded_questions():
    q = core.Queue()
    q.ingest(topics=[{"name": "Proveedor", "priority": "high", "note": ""}],
             now=1000.0)
    q.ingest(questions=[{"text": "pregunta anclada de prioridad baja",
                         "priority": "low",
                         "grounds": "texto del invitado",
                         "evidence": "grounds"}],
             topics=[{"name": "Proveedor", "priority": "high", "note": ""}],
             now=1100.0)
    pending = q.pending_questions(now=1100.0)
    grounded = next(item for item in pending if item.get("evidence") == "grounds")
    synth = next(item for item in pending
                 if item.get("source") == core.TOPIC_SYNTH_SOURCE)
    assert pending.index(grounded) < pending.index(synth)
    # even a score bump cannot promote the synth above a grounded question
    q.questions[synth["key"]]["score"] = 10_000.0
    assert q.pending_questions(now=1100.0)[0]["key"] == grounded["key"]


def test_snapshot_carries_asked_answer_and_none_when_skipped():
    q = core.Queue()
    q.ingest(questions=[{"text": "primera pregunta", "priority": "high"},
                        {"text": "segunda pregunta", "priority": "high"}],
             now=1000.0)
    keys = [item["key"] for item in q.ranked_questions(now=1000.0)]
    q.mark_asked(keys[0], answer="porque costaba demasiado")
    q.mark_asked(keys[1])
    snap = q.snapshot(now=1000.0)
    by_key = {item["key"]: item for item in snap["questions"]}
    assert by_key[keys[0]]["answer"] == "porque costaba demasiado"
    assert by_key[keys[1]]["answer"] is None
