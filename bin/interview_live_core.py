#!/usr/bin/env python3
"""Purpose: Pure queue model and suggestion parsing for interview-live (no I/O).
Responsibilities:
  - Parse pi suggestion output (JSON or legacy plain lines) into questions/topics
  - Maintain a prioritized queue: topics dedupe by normalized name and accumulate
    mention counts (recurrence raises priority); questions dedupe by normalized
    text and merge paraphrases of the same pending question by token overlap
  - Keep topics as context state: a recurring topic with no question yields one
    deterministic low-priority candidate (canticos-6ic) that never outranks a
    grounded question
  - Record an optional one-line answer on the ask transition (canticos-8ax)
  - Rank items so the dashboard shows "what matters right now" first
  - Track question lifecycle status (pending / asked / dismissed / expired) and
    a first-render `shown` flag so events fire exactly once per question
  - Decay pending questions by staleness (time since last evidence) and expire
    them below the threshold; fresh evidence resets the decay clock
Rationale: canticos-c02 — the reactive TUI needs a small, unit-testable model
  decoupled from ffmpeg/whisper/pi subprocesses and from curses rendering. Tests
  import this module directly; the script and the TUI both share this one queue.
  canticos-4pi — exact-key dedupe fragmented the queue under paraphrase storms;
  token-set Jaccard (after a light es normalization) folds variants together.
  canticos-vvc — question lifecycle events need a status API and a shown flag
  here, where transitions happen, so TUI and plain mode share one truth.
  canticos-q9j — 41 evergreen pending items survived a 628-item backlog; decay
  is pure and injectable (P-13 half-life, P-14 threshold) and acts on staleness,
  never on mere existence, so a re-mentioned question survives.
"""

import json
import math
import re
import threading
import time

PRIORITY_BASE = {"high": 100, "medium": 60, "low": 30}
PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1}
RECURRENCE_BONUS = 40  # added per extra mention beyond the first
JACCARD_MERGE = 0.7  # token-set overlap at/above this merges paraphrase questions
DECAY_HALF_LIFE_S = 60.0  # P-13: decay weight halves every minute without evidence
EXPIRY_THRESHOLD = 0.15  # P-14: below this weight a pending question is retired

# canticos-6ic: topics are context, not actions. A topic that recurs (2+
# mentions) but has no question yet gets exactly one deterministic low-priority
# candidate, so the topic -> question path survives without outranking anything
# grounded.
TOPIC_SYNTH_MENTIONS = 2
TOPIC_SYNTH_PRIORITY = "low"
TOPIC_SYNTH_SOURCE = "topic-synth"

# canticos-47b: discovery slots are the learning goals of an interview type.
# Weights feed the goal-gap rank term; keywords let an untagged question still
# claim the goal it serves. The default config is the Mom Test's five slots.
GOAL_STALE_S = 900.0  # a goal with no question for 15 min is stale in the digest
THREAD_STATES = ("open", "parked", "closed")
_THREAD_STATE_RANK = {state: i for i, state in enumerate(THREAD_STATES)}
MOM_TEST_SLOTS = (
    {"id": "past_event", "label": "A concrete past event", "weight": 5,
     "keywords": ("when", "last time", "ultima vez", "cuando", "pasado",
                  "happened")},
    {"id": "cost", "label": "Cost in time or money", "weight": 4,
     "keywords": ("cost", "costo", "coste", "cuanto", "pago", "pagaste",
                  "precio", "pay", "price", "money", "dinero")},
    {"id": "workaround", "label": "Current workaround", "weight": 3,
     "keywords": ("workaround", "hiciste", "hacer", "solucion", "usaste",
                  "usar", "parche")},
    {"id": "decision", "label": "Decision process", "weight": 2,
     "keywords": ("decidiste", "decidio", "decision", "elegiste", "choose",
                  "chose", "why")},
    {"id": "outcome", "label": "Outcome and aftermath", "weight": 2,
     "keywords": ("resultado", "consecuencia", "despues", "after", "outcome")},
)

# Vowel accents only: folding these makes "cuánto"/"cuanto" meet without
# erasing ñ (año != ano) or any consonant distinction.
_ACCENT_FOLD = str.maketrans("áéíóúü", "aeiouu")

# Function words carry no question meaning; paraphrase variants differ mostly
# by these ("la multa" vs "esa multa"), so they are excluded from overlap.
_ES_STOPWORDS = frozenset({
    "el", "la", "los", "las", "lo", "un", "una", "unos", "unas",
    "de", "del", "al", "a", "en", "por", "para", "con", "sin", "sobre", "entre",
    "y", "e", "o", "u", "que", "le", "les", "se", "su", "sus",
    "mi", "mis", "tu", "tus", "este", "esta", "estos", "estas",
    "ese", "esa", "esos", "esas", "esto", "eso", "es", "son", "ser",
})


def fmt_ms(ms):
    """Milliseconds -> HH:MM:SS (single source of truth; TUI and transcript share it)."""
    s = ms // 1000
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def normalize_topic(text):
    """Canonical dedupe key: lowercase, strip punctuation, collapse whitespace."""
    s = re.sub(r"[^a-z0-9\u00e0-\u00ff\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def topic_question_text(name):
    """Deterministic candidate wording for a recurring, question-less topic
    (canticos-6ic). Same topic name -> same question text, so repeated ingests
    never create a second key."""
    return f"¿Qué puedes contarme sobre {name}?"


def _stem_es(word):
    """Conservative Spanish suffix strip: plural -s, then feminine/masculine
    final -a/-o on longer words. Short words and consonants stay intact."""
    if len(word) > 3 and word.endswith("s"):
        word = word[:-1]
    if len(word) > 4 and word[-1] in "ao":
        word = word[:-1]
    return word


def content_tokens(text):
    """Ordered content tokens (stopwords removed, es-stemmed). Shared by
    token_set and by the grounds matcher, which needs token order to compare a
    quote against contiguous line slices."""
    tokens = []
    for word in normalize_topic(text).translate(_ACCENT_FOLD).split():
        if word in _ES_STOPWORDS:
            continue
        stem = _stem_es(word)
        if stem:
            tokens.append(stem)
    return tokens


def token_set(text):
    """Content tokens used for paraphrase similarity. Deliberately separate from
    normalize_topic: keys stay exact so stored identity and lookups don't shift."""
    return frozenset(content_tokens(text))


def _jaccard_sets(a, b):
    """Jaccard overlap of two token sets; 0 when either side is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def jaccard_sets(set_a, set_b):
    """Jaccard overlap for callers that already tokenized (grounds matching)."""
    return _jaccard_sets(set_a, set_b)


def jaccard_score(text_a, text_b):
    """Token-set Jaccard overlap in [0, 1]; 0 when either side has no content."""
    return _jaccard_sets(token_set(text_a), token_set(text_b))


class ThreadTracker:
    """Interview threads (topics with a lifecycle), keyed by normalized topic.

    `touch` is the only way questions enter the tracker: the first touch opens
    the thread, later touches bump its counters. A parked or closed thread is
    never reopened by new evidence — resurrection needs an explicit `set_state`,
    so the tracker records the interview's shape instead of oscillating."""

    def __init__(self):
        self.threads = {}  # normalized key -> thread dict
        self._lock = threading.RLock()

    def touch(self, text, now=None):
        """Record a question (or any evidence) under its normalized topic.
        Returns a copy of the thread, or None for empty text."""
        now = now if now is not None else time.time()
        label = str(text or "").strip()
        key = normalize_topic(label)
        if not key:
            return None
        with self._lock:
            cur = self.threads.get(key)
            if cur is None:
                cur = {"key": key, "label": label, "state": "open",
                       "first_seen": now, "last_seen": now,
                       "question_count": 1}
                self.threads[key] = cur
            else:
                cur["last_seen"] = now
                cur["question_count"] += 1
                if cur["state"] == "open":
                    cur["label"] = label  # newest wording, same thread identity
            return dict(cur)

    def set_state(self, text, state, now=None):
        """Explicit lifecycle transition (open/parked/closed); None if unknown."""
        if state not in _THREAD_STATE_RANK:
            raise ValueError(f"unknown thread state: {state!r}")
        now = now if now is not None else time.time()
        key = normalize_topic(text)
        with self._lock:
            cur = self.threads.get(key)
            if cur is None:
                return None
            cur["state"] = state
            cur["last_seen"] = now
            return dict(cur)

    def park(self, text, now=None):
        return self.set_state(text, "parked", now)

    def close(self, text, now=None):
        return self.set_state(text, "closed", now)

    def snapshot(self, now=None):
        """Serializable threads, open first then most recently touched.
        `stale` mirrors goal staleness: no touch for GOAL_STALE_S seconds."""
        now = now if now is not None else time.time()
        with self._lock:
            items = []
            for thread in self.threads.values():
                item = dict(thread)
                item["age"] = max(0.0, now - item["last_seen"])
                item["stale"] = item["age"] >= GOAL_STALE_S
                items.append(item)
        items.sort(key=lambda t: (_THREAD_STATE_RANK[t["state"]],
                                  -t["last_seen"], t["key"]))
        return items


class SlotModel:
    """Config-driven discovery slots (learning goals) for one interview type.

    Pure helpers: `classify` maps question text to a slot by keyword evidence,
    `assign` returns question copies with `goal_id` filled, `coverage_state`
    reports per-slot touch state, and `goal_gap` scores a question by the weight
    of the slot it would open. Defaults to the Mom Test slots."""

    def __init__(self, slots=None, stale_s=GOAL_STALE_S):
        raw = MOM_TEST_SLOTS if slots is None else slots
        self.slots = []
        for slot in raw:
            self.slots.append({
                "id": str(slot["id"]),
                "label": str(slot.get("label") or slot["id"]),
                "weight": float(slot.get("weight", 1)),
                "keywords": tuple(str(k) for k in slot.get("keywords", ())),
            })
        self._weights = {slot["id"]: slot["weight"] for slot in self.slots}
        self.stale_s = float(stale_s)

    def classify(self, text):
        """Slot id with the most keyword evidence; config order breaks ties.
        None when nothing matches (the question still ranks, just without gap)."""
        tokens = token_set(text)
        if not tokens:
            return None
        best, best_hits = None, 0
        for slot in self.slots:
            hits = 0
            for keyword in slot["keywords"]:
                kw_tokens = token_set(keyword)
                if kw_tokens and kw_tokens <= tokens:
                    hits += 1
            if hits > best_hits:
                best, best_hits = slot["id"], hits
        return best

    def goal_id_for(self, question):
        """Explicit `goal_id` when it names a configured slot, else classify."""
        goal_id = question.get("goal_id")
        if goal_id in self._weights:
            return goal_id
        return self.classify(question.get("text", ""))

    def assign(self, questions):
        """Copies of `questions` with `goal_id` filled from keywords; explicit
        tags always win and unmatched questions stay untagged."""
        assigned = []
        for question in questions or []:
            item = dict(question)
            if item.get("goal_id") not in self._weights:
                goal_id = self.classify(item.get("text", ""))
                if goal_id:
                    item["goal_id"] = goal_id
                else:
                    item.pop("goal_id", None)
            assigned.append(item)
        return assigned

    def coverage_state(self, questions=None, now=None):
        """Per-slot coverage in config order. `covered` means a question touched
        the slot within `stale_s`; untouched/older slots are `stale`."""
        now = now if now is not None else time.time()
        coverage = {}
        for slot in self.slots:
            coverage[slot["id"]] = {
                "id": slot["id"], "label": slot["label"],
                "weight": slot["weight"], "question_count": 0,
                "last_seen": None, "age": None,
                "covered": False, "stale": True,
            }
        for question in questions or []:
            goal_id = self.goal_id_for(question)
            record = coverage.get(goal_id)
            if record is None:
                continue
            record["question_count"] += 1
            seen = question.get("last_seen")
            if seen is not None and (record["last_seen"] is None
                                     or seen > record["last_seen"]):
                record["last_seen"] = seen
        for record in coverage.values():
            if record["last_seen"] is not None:
                record["age"] = max(0.0, now - record["last_seen"])
            record["covered"] = (record["age"] is not None
                                 and record["age"] < self.stale_s)
            record["stale"] = not record["covered"]
        return coverage

    def goal_gap(self, question, questions=None, now=None):
        """Weight of the slot `question` would open, or 0 when another recent
        question already covers that slot. This is the per-question goal-gap
        rank term: it rewards the candidate that reopens an untouched goal."""
        now = now if now is not None else time.time()
        goal_id = self.goal_id_for(question)
        if goal_id is None:
            return 0.0
        key = question.get("key")
        for other in questions or []:
            if other is question:
                continue
            if key is not None and other.get("key") == key:
                continue
            if self.goal_id_for(other) != goal_id:
                continue
            seen = other.get("last_seen")
            if seen is not None and now - seen < self.stale_s:
                return 0.0
        return self._weights[goal_id]

    def snapshot(self, questions=None, now=None):
        """Serializable coverage list, config order preserved."""
        coverage = self.coverage_state(questions, now=now)
        return [coverage[slot["id"]] for slot in self.slots]


def speaker_label(spk, names):
    """Human label for a diarized speaker id; unknown ids fall back to 'Speaker N'
    (canticos-uzv). names maps stringified ids -> {"name": ...} (speakers.json)."""
    rec = names.get(str(spk)) or {}
    return rec.get("name") or f"Speaker {spk}"


def render_segment_line(start_ms, spk, text, names):
    """One transcript line. Named speakers render '[ts] Name: text'; unnamed
    keep the legacy '[ts] (speaker N) text' form so existing consumers and
    transcripts never shift meaning."""
    ts = fmt_ms(start_ms)
    if str(spk) in names:
        return f"[{ts}] {speaker_label(spk, names)}: {text}"
    return f"[{ts}] (speaker {spk}) {text}"


def _prio(value):
    return value if value in PRIORITY_RANK else "medium"


def score_for(priority, mentions):
    """Priority score: base by importance + recurrence bonus per extra mention."""
    base = PRIORITY_BASE.get(_prio(priority), 60)
    return base + (int(mentions) - 1) * RECURRENCE_BONUS


def decay_weight(q, now, half_life=DECAY_HALF_LIFE_S):
    """Staleness weight in (0, 1]: halves every `half_life` seconds since the
    question's last evidence (last_seen). Pure; clock skew never boosts above 1."""
    age = max(0.0, now - q["last_seen"])
    return 0.5 ** (age / half_life)


def expires_at(q, half_life=DECAY_HALF_LIFE_S, threshold=EXPIRY_THRESHOLD):
    """Epoch second at which the question's decay weight reaches `threshold`
    (the expiry boundary; expiry itself triggers strictly below it)."""
    return q["last_seen"] + half_life * math.log2(1.0 / threshold)


def parse_suggestion_output(text):
    """Return (questions, topics) lists of dicts from raw pi output.

    Prefers JSON (the system prompt now asks for it): {"questions": [...],
    "topics": [{"name", "priority", "note"}]}. Falls back to legacy plain lines
    (one question per line) when stdout is not JSON — transcript still works.
    """
    text = (text or "").strip()
    if not text:
        return [], []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            questions, topics = [], []
            for q in obj.get("questions") or []:
                if isinstance(q, dict):
                    questions.append({"text": str(q.get("text", "")).strip(),
                                      "priority": _prio(q.get("priority")),
                                      "grounds": str(q.get("grounds") or "").strip(),
                                      "goal_id": str(q.get("goal_id") or "").strip(),
                                      "contract_miss": False})
                elif q:
                    questions.append({"text": str(q).strip(), "priority": "medium",
                                      "grounds": "", "goal_id": "",
                                      "contract_miss": False})
            for t in obj.get("topics") or []:
                if isinstance(t, dict):
                    topics.append({"name": str(t.get("name", "")).strip(),
                                   "priority": _prio(t.get("priority")),
                                   "note": str(t.get("note") or "").strip()})
                elif t:
                    topics.append({"name": str(t).strip(), "priority": "medium", "note": ""})
            if questions or topics:
                return questions, topics
    # legacy plain-line output: keep only explicit list items (Q1:, 2), - bullets)
    # canticos-ap3: the fallback still works, but each item is marked as a
    # contract miss so health metrics can count it without dropping the batch.
    questions = []
    list_re = re.compile(r"^\s*(?:[Qq]\d*|\d+|[-*\u2022])[:.)]?\s*(.*)$")
    for line in text.splitlines():
        m = list_re.match(line.strip())
        if m and m.group(1):
            questions.append({"text": m.group(1).strip(), "priority": "medium",
                              "grounds": "", "contract_miss": True})
    return questions, []


class Queue:
    """Prioritized suggestion queue shared by the worker loop, the suggestion
    thread, and the TUI. RLock-guarded: ingest happens off-thread while the
    dashboard iterates ranked views. A new question whose content tokens overlap
    a pending item's by >= JACCARD_MERGE folds into it instead of adding a key."""

    def __init__(self, threads=None, slots=None):
        self.topics = {}     # normalized key -> topic dict
        self.questions = {}  # normalized key -> question dict
        self.threads = threads  # ThreadTracker or None (canticos-47b)
        self.slots = slots      # SlotModel or None (canticos-47b)
        self._lock = threading.RLock()

    def ingest(self, questions=None, topics=None, now=None):
        with self._lock:
            self._ingest(questions, topics, now)

    def _match_pending(self, tokens):
        """Best pending question whose token set overlaps tokens above threshold.
        Dict order breaks ties, so repeated ingests stay deterministic."""
        best, best_score = None, 0.0
        for q in self.questions.values():
            if q.get("status") != "pending":
                continue
            score = _jaccard_sets(tokens, token_set(q["text"]))
            if score >= JACCARD_MERGE and score > best_score:
                best, best_score = q, score
        return best

    def _ingest(self, questions=None, topics=None, now=None):
        now = now if now is not None else time.time()
        for q in questions or []:
            text = (q.get("text") if isinstance(q, dict) else str(q)).strip()
            key = normalize_topic(text)
            if not key:
                continue
            prio = q.get("priority") if isinstance(q, dict) else "medium"
            extra = ({k: q.get(k) for k in ("grounds", "evidence", "contract_miss",
                                             "source")
                      if q.get(k) is not None} if isinstance(q, dict) else {})
            synthesized = extra.get("source") == TOPIC_SYNTH_SOURCE
            # canticos-47b: every question references a thread (explicit ref,
            # else its own normalized text) and, when slots are configured, the
            # goal it serves (explicit tag wins over keyword classification).
            thread_ref = None
            goal_id = None
            if isinstance(q, dict):
                thread_ref = " ".join(str(q.get("thread") or "").split()).strip() or None
                goal_id = str(q.get("goal_id") or "").strip() or None
            explicit_goal = goal_id is not None
            if goal_id is None and self.slots is not None:
                goal_id = self.slots.classify(text)
            if goal_id:
                extra["goal_id"] = goal_id
            cur = self.questions.get(key)
            fuzzy = False
            if cur is None and not synthesized:
                cur = self._match_pending(token_set(text))
                fuzzy = cur is not None
            if fuzzy and thread_ref is None:
                # a paraphrase folds into the original question: same thread
                thread_ref = cur.get("thread") or cur.get("text")
            thread_key = normalize_topic(thread_ref or text)
            if self.threads is not None:
                touched = self.threads.touch(thread_ref or text, now)
                if touched:
                    thread_key = touched["key"]
            if cur is None:
                cur = {"key": key, "text": text, "priority": prio,
                       "mentions": 1, "added_at": now, "last_seen": now,
                       "status": "pending", "shown": False, "score": 0,
                       "thread": thread_key, **extra}
                self.questions[key] = cur
            else:
                cur["mentions"] += 1
                cur["last_seen"] = now
                for k, v in extra.items():
                    if k == "contract_miss":
                        cur[k] = bool(v)
                    elif k == "goal_id" and not explicit_goal and "goal_id" in cur:
                        continue  # keep the thread's established goal
                    elif v not in (None, ""):
                        cur[k] = v
                if fuzzy:
                    # paraphrase: show the newest wording, keep the original key
                    cur["text"] = text
                if PRIORITY_RANK.get(prio, 2) > PRIORITY_RANK.get(cur["priority"], 2):
                    cur["priority"] = prio
            cur["score"] = score_for(cur["priority"], cur["mentions"])

        for t in topics or []:
            name = (t.get("name") if isinstance(t, dict) else str(t)).strip()
            key = normalize_topic(name)
            if not key:
                continue
            prio = t.get("priority") if isinstance(t, dict) else "medium"
            note = t.get("note", "") if isinstance(t, dict) else ""
            cur = self.topics.get(key)
            if cur is None:
                cur = {"key": key, "name": name, "priority": prio, "note": note,
                       "mentions": 1, "added_at": now, "last_seen": now, "score": 0}
                self.topics[key] = cur
            else:
                cur["mentions"] += 1
                cur["last_seen"] = now
                if note:
                    cur["note"] = note
                if PRIORITY_RANK.get(prio, 2) > PRIORITY_RANK.get(cur["priority"], 2):
                    cur["priority"] = prio
            cur["score"] = score_for(cur["priority"], cur["mentions"])
        self._synthesize_topics(now)

    def topic_question_count(self, key):
        """Questions attached to a topic thread (canticos-6ic). Explicit thread
        refs are the link; a question whose own text normalizes to the topic key
        counts too. Terminal questions still count: the topic *has* a path to a
        question, so no synthetic duplicate is needed."""
        key = normalize_topic(key)
        return sum(1 for q in self.questions.values()
                   if normalize_topic(q.get("thread") or q["text"]) == key)

    def has_question(self, key):
        """True when the topic already has at least one question; drives the
        strip's ✓ marker and blocks topic synthesis."""
        return self.topic_question_count(key) > 0

    def topic_entries(self):
        """Strip-ready topic view (ranked, one dict per topic). The TUI renders
        these as a single clipped context line; they are never action rows."""
        with self._lock:
            return [{"key": t["key"], "name": t["name"],
                     "mentions": t["mentions"], "priority": t["priority"],
                     "has_question": self.has_question(t["key"])}
                    for t in self.ranked_topics()]

    def _synthesize_topics(self, now=None):
        """canticos-6ic topic-to-question path: a topic mentioned at least
        TOPIC_SYNTH_MENTIONS times with no question yet yields exactly one
        deterministic low-priority candidate. Sorted by topic key so repeated
        ingests are deterministic; the candidate's explicit thread is the topic
        key, which makes it count as that topic's question and blocks a second
        synthesis."""
        if not self.topics:
            return
        candidates = []
        for t in sorted(self.topics.values(), key=lambda item: item["key"]):
            if t["mentions"] < TOPIC_SYNTH_MENTIONS:
                continue
            if self.topic_question_count(t["key"]):
                continue
            candidates.append({
                "text": topic_question_text(t["name"]),
                "priority": TOPIC_SYNTH_PRIORITY,
                "source": TOPIC_SYNTH_SOURCE,
                "evidence": "no-evidence",
                "thread": t["key"],
            })
        if candidates:
            self._ingest(questions=candidates, now=now)

    def ranked_topics(self):
        with self._lock:
            return sorted(self.topics.values(),
                          key=lambda t: (-t["score"], -t["last_seen"], t["name"]))

    def _goal_gaps(self, now):
        """Per-question goal-gap term; empty when no slot model is configured,
        which makes the rank key byte-for-byte the pre-canticos-47b one."""
        if self.slots is None:
            return {}
        questions = list(self.questions.values())
        return {q["key"]: self.slots.goal_gap(q, questions, now=now)
                for q in questions}

    def ranked_questions(self, now=None):
        """Score order plus the goal-gap term (canticos-47b): a question that
        would reopen a heavy, untouched discovery slot outranks an equally
        scored question on a goal already covered. Ties stay deterministic."""
        now = now if now is not None else time.time()
        with self._lock:
            gaps = self._goal_gaps(now)
            return sorted(self.questions.values(),
                          key=lambda q: (0 if q["status"] == "pending" else 1,
                                         1 if q.get("source") == TOPIC_SYNTH_SOURCE else 0,
                                         -(q["score"] + gaps.get(q["key"], 0.0)),
                                         -q["last_seen"], q["text"]))

    def pending_questions(self, now=None):
        """Ranked pending view: terminal questions (asked / dismissed /
        expired) never appear. canticos-vvc — lifecycle status is the single
        source of truth for what the interviewer can still act on; canticos-q9j
        adds 'expired' as a terminal status."""
        return [q for q in self.ranked_questions(now) if q["status"] == "pending"]

    def goal_snapshot(self, now=None):
        """Goal coverage for the prompt digest / queue.json; None when the
        session has no slot model (the prompt then has no goals section)."""
        with self._lock:
            if self.slots is None:
                return None
            return self.slots.snapshot(list(self.questions.values()), now=now)

    def top_pending_question(self):
        pending = self.pending_questions()
        return pending[0] if pending else None

    def mark_shown(self, key):
        """Flag a question as rendered and return a snapshot on that first flip
        only. Repeated frames, asked and dismissed questions return None (the
        question_shown event must fire exactly once per question lifetime)."""
        with self._lock:
            q = self.questions.get(key)
            if not q or q["status"] != "pending" or q.get("shown"):
                return None
            q["shown"] = True
            return dict(q)

    def _transition(self, key, status, **fields):
        """Set a lifecycle status; return a snapshot only when it changed, so
        repeated calls never emit duplicate transition events. Extra fields
        (canticos-8ax: a captured answer) ride on the same transition."""
        with self._lock:
            q = self.questions.get(key)
            if not q or q["status"] == status:
                return None
            q["status"] = status
            q.update(fields)
            return dict(q)

    def mark_asked(self, key, answer=None):
        """Mark asked and store the optional one-line answer (canticos-8ax);
        returns the question snapshot when it transitioned. A skipped answer is
        stored as None so queue.json keeps a stable field."""
        return self._transition(key, "asked", answer=answer)

    def mark_dismissed(self, key):
        """Dismiss (canticos-vvc): kept in the snapshot for the audit trail but
        out of every pending view, and it never re-emits as shown."""
        return self._transition(key, "dismissed")

    def expire_pending(self, now=None, half_life=DECAY_HALF_LIFE_S,
                       threshold=EXPIRY_THRESHOLD):
        """Retire pending questions whose decay weight fell below `threshold`
        (canticos-q9j). Idempotent: only the transitions made in this call are
        returned, most stale first, so callers emit one event per expiry."""
        now = now if now is not None else time.time()
        with self._lock:
            expired = []
            for q in self.questions.values():
                if q["status"] != "pending":
                    continue
                if decay_weight(q, now, half_life) < threshold:
                    q["status"] = "expired"
                    expired.append(dict(q))
            expired.sort(key=lambda q: (q["last_seen"], q["key"]))
            return expired

    def snapshot(self, participants=None, updated_at=None, now=None):
        """Serializable state, written to queue.json for archival/debug.
        Carries the decay inputs at snapshot time: every question gets its
        decay_weight and expires_at so stale-but-live items are diagnosable."""
        now = now if now is not None else time.time()
        questions = [dict(q, decay_weight=decay_weight(q, now),
                          expires_at=expires_at(q))
                     for q in self.ranked_questions(now)]
        data = {
            "participants": participants,
            "updated_at": updated_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "questions": questions,
            "topics": self.ranked_topics(),
        }
        # canticos-47b: threads/goals are present only when the session tracks
        # them, so bare Queue() snapshots keep their historical shape.
        if self.threads is not None:
            data["threads"] = self.threads.snapshot(now=now)
        if self.slots is not None:
            data["goals"] = self.slots.snapshot(questions, now=now)
        return data