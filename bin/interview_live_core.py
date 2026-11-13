#!/usr/bin/env python3
"""Purpose: Pure queue model and suggestion parsing for interview-live (no I/O).
Responsibilities:
  - Parse pi suggestion output (JSON or legacy plain lines) into questions/topics
  - Maintain a prioritized queue: topics dedupe by normalized name and accumulate
    mention counts (recurrence raises priority); questions dedupe by normalized
    text and merge paraphrases of the same pending question by token overlap
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


def _stem_es(word):
    """Conservative Spanish suffix strip: plural -s, then feminine/masculine
    final -a/-o on longer words. Short words and consonants stay intact."""
    if len(word) > 3 and word.endswith("s"):
        word = word[:-1]
    if len(word) > 4 and word[-1] in "ao":
        word = word[:-1]
    return word


def token_set(text):
    """Content tokens used for paraphrase similarity. Deliberately separate from
    normalize_topic: keys stay exact so stored identity and lookups don't shift."""
    tokens = set()
    for word in normalize_topic(text).translate(_ACCENT_FOLD).split():
        if word in _ES_STOPWORDS:
            continue
        stem = _stem_es(word)
        if stem:
            tokens.add(stem)
    return frozenset(tokens)


def _jaccard_sets(a, b):
    """Jaccard overlap of two token sets; 0 when either side is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def jaccard_score(text_a, text_b):
    """Token-set Jaccard overlap in [0, 1]; 0 when either side has no content."""
    return _jaccard_sets(token_set(text_a), token_set(text_b))


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
                                      "priority": _prio(q.get("priority"))})
                elif q:
                    questions.append({"text": str(q).strip(), "priority": "medium"})
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
    questions = []
    list_re = re.compile(r"^\s*(?:[Qq]\d*|\d+|[-*\u2022])[:.)]?\s*(.*)$")
    for line in text.splitlines():
        m = list_re.match(line.strip())
        if m and m.group(1):
            questions.append({"text": m.group(1).strip(), "priority": "medium"})
    return questions, []


class Queue:
    """Prioritized suggestion queue shared by the worker loop, the suggestion
    thread, and the TUI. RLock-guarded: ingest happens off-thread while the
    dashboard iterates ranked views. A new question whose content tokens overlap
    a pending item's by >= JACCARD_MERGE folds into it instead of adding a key."""

    def __init__(self):
        self.topics = {}     # normalized key -> topic dict
        self.questions = {}  # normalized key -> question dict
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
            cur = self.questions.get(key)
            fuzzy = False
            if cur is None:
                cur = self._match_pending(token_set(text))
                fuzzy = cur is not None
            if cur is None:
                cur = {"key": key, "text": text, "priority": prio,
                       "mentions": 1, "added_at": now, "last_seen": now,
                       "status": "pending", "shown": False, "score": 0}
                self.questions[key] = cur
            else:
                cur["mentions"] += 1
                cur["last_seen"] = now
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

    def ranked_topics(self):
        with self._lock:
            return sorted(self.topics.values(),
                          key=lambda t: (-t["score"], -t["last_seen"], t["name"]))

    def ranked_questions(self):
        with self._lock:
            return sorted(self.questions.values(),
                          key=lambda q: (0 if q["status"] == "pending" else 1,
                                         -q["score"], -q["last_seen"], q["text"]))

    def pending_questions(self):
        """Ranked pending view: terminal questions (asked / dismissed /
        expired) never appear. canticos-vvc — lifecycle status is the single
        source of truth for what the interviewer can still act on; canticos-q9j
        adds 'expired' as a terminal status."""
        return [q for q in self.ranked_questions() if q["status"] == "pending"]

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

    def _transition(self, key, status):
        """Set a lifecycle status; return a snapshot only when it changed, so
        repeated calls never emit duplicate transition events."""
        with self._lock:
            q = self.questions.get(key)
            if not q or q["status"] == status:
                return None
            q["status"] = status
            return dict(q)

    def mark_asked(self, key):
        """Mark asked; returns the question snapshot when it transitioned."""
        return self._transition(key, "asked")

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
                     for q in self.ranked_questions()]
        return {
            "participants": participants,
            "updated_at": updated_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "questions": questions,
            "topics": self.ranked_topics(),
        }