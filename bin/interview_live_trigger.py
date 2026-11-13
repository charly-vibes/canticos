#!/usr/bin/env python3
"""Purpose: Pure trigger policy deciding WHEN a suggestion batch is worth a pi call.
Responsibilities:
  - Accumulate incoming whisper segments into one coalesced pending batch
  - Fire on guest turn end (P-01 quiet floor), long guest monologue (P-02 cap),
    or an explicit force (manual round / end-of-run flush)
  - Guarantee no segment loss: drain() returns every added segment exactly once
Rationale: canticos-2iv — the measured baseline (222 batches / 83.5 min, median
  21s cadence, 628 questions, 587 manual dismissals) showed the round timer, not
  the conversation, driving suggestions and the interviewer acting as the
  garbage collector. Cadence becomes a pure function of the transcript with the
  clock injected, so it is testable without audio, whisper or pi. Constants
  adopt the claude proposal ids P-01 (turn-end 1.2s) / P-02 (monologue 25s).
"""

TURN_END_QUIET_S = 1.2   # P-01: pause that confirms the guest yielded the floor
MONOLOGUE_MAX_S = 25.0   # P-02: cap a single guest monologue before asking

INTERVIEWER = 0          # diarized speaker 0 is always the interviewer


class TriggerPolicy:
    """Batching/trigger state for one session. Segments are (start_ms, end_ms,
    speaker, text) tuples in arrival order; `now` is a monotonic clock."""

    def __init__(self, turn_end_quiet_s=TURN_END_QUIET_S,
                 monologue_s=MONOLOGUE_MAX_S, min_segments=1):
        self.turn_end_quiet_s = float(turn_end_quiet_s)
        self.monologue_ms = float(monologue_s) * 1000
        self.min_segments = max(1, int(min_segments))
        self._pending = []
        self._run_spk = None   # speaker of the trailing utterance
        self._run_start_ms = 0
        self._run_end_ms = 0
        self._turn_end = None  # {"at": wall clock, "confirmed": bool}

    @property
    def pending_count(self):
        return len(self._pending)

    def add(self, segments, now):
        """Feed the segments transcribed in this round. A speaker change away
        from a guest marks a turn end; a >=quiet_s audio pause confirms it
        without waiting for the loop timer (replay windows arrive instantly)."""
        for seg in segments:
            start_ms, end_ms, spk = seg[0], seg[1], seg[2]
            if self._run_spk is None:
                self._run_spk, self._run_start_ms, self._run_end_ms = spk, start_ms, end_ms
            elif spk != self._run_spk:
                if self._run_spk != INTERVIEWER:
                    self._turn_end = {
                        "at": now,
                        "confirmed": (start_ms - self._run_end_ms) >= self.turn_end_quiet_s * 1000,
                    }
                self._run_spk, self._run_start_ms, self._run_end_ms = spk, start_ms, end_ms
            else:
                self._run_start_ms = min(self._run_start_ms, start_ms)
                self._run_end_ms = max(self._run_end_ms, end_ms)
            self._pending.append(seg)

    def due(self, now):
        """Reason to fire now ("turn-end" | "monologue"), or None."""
        if len(self._pending) < self.min_segments:
            return None
        if (self._run_spk not in (None, INTERVIEWER)
                and (self._run_end_ms - self._run_start_ms) >= self.monologue_ms):
            return "monologue"
        if self._turn_end is not None:
            if (self._turn_end["confirmed"]
                    or (now - self._turn_end["at"]) >= self.turn_end_quiet_s):
                return "turn-end"
        return None

    def drain(self):
        """Take the whole pending batch (the manual force path) and reset the
        batching state; the next batch starts from the next segment."""
        drained, self._pending = self._pending, []
        self._run_spk = None
        self._run_start_ms = self._run_end_ms = 0
        self._turn_end = None
        return drained