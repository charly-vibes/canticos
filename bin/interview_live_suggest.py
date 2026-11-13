#!/usr/bin/env python3
"""Purpose: pi suggestion prompting for interview-live (Mom Test methodology).
Responsibilities:
  - Own the Mom Test system prompt, including the participant-count CONTEXT line
  - Resolve the effective participant count: the TUI prompt answer (ui) wins
    over the CLI default when the dashboard is active
  - Build the stateless pi argv for a suggestion round (full contract, rolling
    window, new lines, pending digest; optional model)
Rationale: canticos-9x1 + canticos-c02 — the participant count entered in the
  dashboard never reached the model (CORR-001); the fix is a single resolution
  point used by both the prompt and the queue snapshot. Keeping the prompt and
  argv construction here makes suggestion behavior testable without spawning pi.
  canticos-7h9 — an optional stronger question model rides the same argv builder;
  its soft budget (P-04, 45s) is injectable so tests never wait on a real clock.
  canticos-ap3 — validate_round() is the single contract gate: zero questions
  fail, unverifiable grounds are demoted (never deleted), >2 topics are flagged,
  and legacy plain lines stay usable but count as a contract miss.
  canticos-ate — suggestion calls are stateless: every round re-sends the
  contract plus a 12k-budgeted window/new-lines/pending-digest prompt, so
  quality no longer depends on a growing pi session.
"""

import time

import interview_live_core as core

QUESTION_MODEL_TIMEOUT_S = 45.0  # P-04: soft budget for --question-model before fallback
PI_TIMEOUT_S = 180.0  # default pi budget for the fallback / pre-flag path
RECENT_WINDOW_LINES = 40  # rolling transcript context (canticos-ate, P-03)
PROMPT_BUDGET = 12000  # P-03: 12k chars for the whole prompt (contract included)
DIGEST_LIMIT = 5  # pending-question digest entries per round

# validate_round reasons; the coordinator keys repair/reject decisions on these.
REASON_NO_QUESTIONS = "no-questions"
REASON_NO_EVIDENCE = "no-evidence"
REASON_TOO_MANY_TOPICS = "too-many-topics"
REASON_CONTRACT_MISS = "contract-miss"
MAX_TOPICS = 2  # the prompt asks for the important topics, not a taxonomy

MOM_SYSTEM_PROMPT = """You are a Mom Test interviewing coach (methodology from Rob Fitzpatrick's "The Mom Test").

The Mom Test core rules:
- Talk about THEIR life, not yours. Never pitch, solve, or brag.
- Ask about specific facts and events from the PAST — the last time it happened, what
  they did, who was involved, what it cost (time/money), what happened right before/after.
- Do not ask about hypotheticals ("would you...?") or opinions ("do you think...?")
  — people answer those with lies and compliments.
- Follow up on the 3 big lies: compliments ("great idea"), hypotheticals, and
  generosities ("I'd definitely buy it", "I'll introduce you to X").
- When you hear a strong signal (a specific past problem, emotion, a number, a name),
  dig deeper: "When did that last happen?", "What did you do about it?", "How much
  did it cost you?".
- Keep the interviewer:interviewee speech ratio low — questions short, interviewee talks most.

The transcript lines are prefixed with [MM:SS] and (speaker N). speaker 0 = interviewer (you),
speaker 1 = guest.

Your job: examine the NEW transcript lines, find the strongest signal moments, and
suggest concrete follow-up questions (grounded in what the guest actually said,
in the language the conversation uses) that a good Mom Test interviewer would ask
NEXT, plus surface the important topics worth prioritizing as the interview goes.

Output ONLY a JSON object (no preamble, no markdown fences) with exactly two keys:
- "questions": array of 1-4 objects, each
  {"text": str, "priority": "high"|"medium"|"low", "grounds": str}
  "text" is the follow-up question, in the conversation's language;
  "grounds" is a SHORT VERBATIM QUOTE (a few words, copied exactly) from the NEW
  transcript lines — the guest's own words that justify the question. Every
  question needs grounds: if you cannot quote the guest, drop the question.
- "topics": array of objects {"name": str, "priority": "high"|"medium"|"low", "note": str}
  — important topics/signals heard in the NEW lines; keep names short (a few words)
  and return at most 2.

HARD RULES FOR QUESTION QUALITY — a question that breaks these must be rewritten or dropped:
1. Every question anchors on the guest's OWN exact words or a timestamped moment: quote
   the phrase inside the question ("when you said 'we paid a fine' — how much was it?")
   or name the moment ([01:12] the regulator story). No quote anchor, no question.
2. Reusability test: if a question could be pasted into any other interview of this kind
   unchanged, it is worthless — delete it. Questions must be un-interchangeable.
3. Banned shapes (never, unless tied to a specific unexplained detail the guest just
   said): "tell me more about X", "what do you think about X?", "how was the experience?",
   "what would you like to see?", and step-by-step walk-ins that don't name what actually
   happened ("¿Qué pasó exactamente, paso a paso?" with no referenced event is generic).
4. Prefer what is falsifiable and concrete: numbers (what did it cost?), names and roles
   (who exactly was there?), dates (which day, when was the last time?), sequence (what
   happened right before, right after?).
5. If the guest was vague, abstract, or hypothetical, do not follow into abstraction:
   pull the last specific instance instead ("when did that last happen to you, in person?").
"""


def effective_participants(participants, ui):
    """TUI prompt answer wins over the CLI default when the dashboard is active."""
    return ui.participants if ui else participants


def system_prompt(participants):
    return MOM_SYSTEM_PROMPT + (
        f"\n\nCONTEXT: this interview has {participants} participants total "
        "(speaker 0 = the interviewer; with 2-speaker diarization all guests "
        "share the speaker 1 label)."
    )


def _grounds_verified(grounds, window_lines):
    """True when the grounds quote appears (fuzzily) in the transcript window.
    Token-set Jaccard >= JACCARD_MERGE against contiguous slices of each line —
    a verbatim phrase is usually shorter than the rendered line, so comparing
    only against whole lines would reject honest quotes. The concatenated token
    stream also catches quotes that straddle two adjacent lines."""
    quote = core.token_set(grounds)
    if not quote:
        return False
    size = len(quote)
    streams = [core.content_tokens(line) for line in window_lines]
    streams.append([tok for stream in streams for tok in stream])
    for tokens in streams:
        if len(tokens) < size:
            continue
        for start in range(len(tokens) - size + 1):
            if core.jaccard_sets(quote, frozenset(tokens[start:start + size])) >= core.JACCARD_MERGE:
                return True
    return False


def validate_round(questions, topics=None, window_lines=None):
    """Contract gate for one suggestion batch.

    Returns (accepted, rejected, reasons):
    - accepted: every usable question, with evidence='grounds' when its grounds
      quote matched the window, else evidence='no-evidence' (demoted, never
      deleted). Legacy plain-line questions stay accepted with contract_miss.
    - rejected: only unusable entries (empty text), never grounds failures.
    - reasons: contract signals the coordinator acts on — 'no-questions',
      'no-evidence', 'too-many-topics', 'contract-miss'.
    """
    accepted, rejected, reasons = [], [], []
    window = list(window_lines or [])
    for raw in questions or []:
        if not isinstance(raw, dict):
            rejected.append(raw)
            continue
        q = dict(raw)
        text = str(q.get("text") or "").strip()
        if not text:
            rejected.append(q)
            continue
        q["text"] = text
        if q.get("contract_miss"):
            q["evidence"] = "no-evidence"
            reasons.append(REASON_CONTRACT_MISS)
            accepted.append(q)
            continue
        grounds = str(q.get("grounds") or "").strip()
        if grounds and _grounds_verified(grounds, window):
            q["evidence"] = "grounds"
        else:
            q["evidence"] = "no-evidence"
            reasons.append(REASON_NO_EVIDENCE)
        accepted.append(q)
    if not accepted:
        reasons.insert(0, REASON_NO_QUESTIONS)
    if len(topics or []) > MAX_TOPICS:
        reasons.append(REASON_TOO_MANY_TOPICS)
    return accepted, rejected, list(dict.fromkeys(reasons))


def digest_lines(pending, now=None, limit=DIGEST_LIMIT):
    """Pending-question digest with freshness/expiry awareness: the strongest
    entries that are still alive, each carrying its decay weight, age, and time
    to expiry (P-13/14). Sub-threshold entries are omitted even if the queue has
    not yet run its render-time expiry sweep."""
    now = now if now is not None else time.time()
    lines = []
    for q in pending or []:
        if not isinstance(q, dict):
            continue
        text = " ".join(str(q.get("text") or "").split())
        if not text:
            continue
        last_seen = q.get("last_seen")
        if last_seen is None:
            weight, age, expires_in = 1.0, 0.0, None
        else:
            age = max(0.0, now - last_seen)
            weight = core.decay_weight(q, now)
            expires_in = max(0.0, core.expires_at(q) - now)
        if weight < core.EXPIRY_THRESHOLD:
            continue
        expiry = f", expires in {expires_in:.0f}s" if expires_in is not None else ""
        lines.append(
            f'{len(lines) + 1}. "{text}" ({q.get("priority", "medium")}; '
            f"freshness {weight:.2f}, {age:.0f}s old{expiry})")
        if len(lines) >= limit:
            break
    return lines


def goals_digest_lines(goals, now=None):
    """LEARNING GOALS digest entries: goal id/label plus last-touch age and a
    STALE marker when nothing has probed the goal for GOAL_STALE_S seconds
    (canticos-47b). Accepts SlotModel snapshots or plain goal dicts."""
    now = now if now is not None else time.time()
    lines = []
    for goal in goals or []:
        if not isinstance(goal, dict):
            continue
        goal_id = str(goal.get("id") or goal.get("goal_id") or "").strip()
        if not goal_id:
            continue
        label = str(goal.get("label") or goal.get("name") or goal_id).strip()
        age = goal.get("age")
        if age is None and goal.get("last_seen") is not None:
            age = max(0.0, now - goal["last_seen"])
        stale = goal.get("stale")
        if stale is None:
            stale = age is None or age >= core.GOAL_STALE_S
        touched = "never" if age is None else f"{age:.0f}s ago"
        suffix = " [STALE]" if stale else ""
        lines.append(f"- {goal_id} ({label}): last touch {touched}{suffix}")
    return lines


def _goals_section(goal_lines):
    return ("## LEARNING GOALS (discovery slots for this interview; tag every "
            "question with the id of the goal it serves in \"goal_id\"; a "
            "[STALE] goal has gone untouched — prefer reopening it):\n"
            + "\n".join(goal_lines))


def _prompt_text(new_lines, window, digest, repair, goal_lines=None):
    """Assemble the stateless user prompt: rolling window, goals, new lines,
    digest. The order follows the P-03 spec so trimming can drop the oldest
    context without reordering the model's attention."""
    parts = []
    if repair:
        parts.append(
            "YOUR PREVIOUS ANSWER WAS REJECTED — it violated the output contract.\n"
            f"Rejected output:\n{repair}\n"
            "Return a corrected answer: the JSON object described in the system "
            "prompt, with at least one grounded question.")
    if window:
        parts.append("RECENT TRANSCRIPT WINDOW (context only; do not re-analyze):\n"
                     + "\n".join(window))
    if goal_lines:
        parts.append(_goals_section(goal_lines))
    parts.append("NEW TRANSCRIPT LINES (analyze these):\n" + new_lines)
    if digest:
        parts.append("PENDING QUESTIONS (already suggested — avoid verbatim "
                     "repeats; prefer deepening or a different thread):\n"
                     + "\n".join(digest))
    parts.append("Return ONLY the JSON object described in the system prompt.")
    return "\n\n".join(parts)


def build_prompt(new_lines, window_lines=None, pending=None, now=None,
                 max_chars=PROMPT_BUDGET, contract_chars=0, repair=None,
                 goals=None):
    """Stateless content for one suggestion round (canticos-ate).

    Enforces the P-03 byte budget by trimming the rolling window first (oldest
    lines), then the pending digest (lowest-ranked entries), then the goals
    digest. The system contract is never trimmed: `contract_chars` is its length
    and is reserved up front. Returns at most `max_chars - contract_chars`
    characters. With no goals configured the prompt is byte-identical to the
    pre-canticos-47b content (no LEARNING GOALS section).
    """
    allowance = max(0, int(max_chars) - int(contract_chars))
    if isinstance(new_lines, str):
        new_text = new_lines
    else:
        new_text = "\n".join(str(line) for line in (new_lines or []))
    window = [str(line) for line in (window_lines or [])]
    digest = digest_lines(pending, now=now)
    goal_lines = goals_digest_lines(goals, now=now)
    # 1) trim the window from the oldest end
    while window and len(_prompt_text(new_text, window, digest, repair,
                                      goal_lines)) > allowance:
        window.pop(0)
    # 2) then trim the digest from its lowest-ranked end
    while digest and len(_prompt_text(new_text, window, digest, repair,
                                      goal_lines)) > allowance:
        digest.pop()
    # 3) then drop goal entries from the tail (config order keeps the heavies)
    while goal_lines and len(_prompt_text(new_text, window, digest, repair,
                                          goal_lines)) > allowance:
        goal_lines.pop()
    # 4) last resort: drop the oldest new lines (still never the contract)
    new_parts = new_text.splitlines()
    while new_parts and len(_prompt_text("\n".join(new_parts), window, digest,
                                         repair, goal_lines)) > allowance:
        new_parts.pop(0)
    prompt = _prompt_text("\n".join(new_parts), window, digest, repair, goal_lines)
    if len(prompt) > allowance:  # pathological input: hard ceiling, contract intact
        prompt = prompt[:allowance]
    return prompt


def build_pi_cmd(session_name, first, pi_model, participants, ui, transcript_lines,
                 question_model=None, repair=None, window_lines=None, pending=None,
                 now=None, goals=None):
    """pi argv for one stateless suggestion round (canticos-ate).

    `--session-id` is deliberately gone: continuity is no longer required for
    suggestion quality, and every round re-sends the Mom Test contract plus the
    rolling window, the new lines, and the pending digest. `session_name`/`first`
    remain for call-site compatibility but no longer affect the argv.
    `question_model` overrides --pi-model for this call only (canticos-7h9);
    `repair` quotes a rejected answer back into the prompt (canticos-ap3);
    `goals` is a SlotModel.snapshot list, injected as the LEARNING GOALS digest
    (canticos-47b) — None/empty keeps the prompt unchanged.
    """
    contract = system_prompt(effective_participants(participants, ui))
    prompt = build_prompt(transcript_lines, window_lines=window_lines,
                          pending=pending, now=now, max_chars=PROMPT_BUDGET,
                          contract_chars=len(contract), repair=repair,
                          goals=goals)
    cmd = ["pi", "--print", "--system-prompt", contract]
    model = question_model or pi_model
    if model:
        cmd += ["--model", model]
    cmd += [prompt]
    return cmd