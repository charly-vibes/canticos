#!/usr/bin/env python3
"""Purpose: Append-only, thread-safe JSONL event log for interview-live sessions.
Responsibilities:
  - Emit typed events (session_start, capture_start, transcript_segment,
    whisper_error, suggest_trigger, suggest_stale_drop, suggest_result,
    suggest_error, suggest_repair, suggest_reject, suggest_fallback,
    question_shown, question_asked, question_dismissed,
    participants_set, session_end) as one JSON object per line,
    in wall-clock order, with ts/seq metadata
  - Append (never truncate) so a session survives crashes and replays cleanly
  - Serialize writes with a lock: the TUI worker thread and the main thread both emit
Rationale: canticos-2vz — an audit trail enables replay, debugging, post-hoc
  analysis and future tooling without re-transcribing or re-running pi. The core
  queue module stays pure; this module owns all event I/O.
"""

import json
import threading
import time
from pathlib import Path


class EventLog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a")
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, etype, **payload):
        """Append one event; returns the recorded dict. Safe from any thread."""
        with self._lock:
            self._seq += 1
            rec = {"ts": time.time(), "seq": self._seq, "type": etype, **payload}
            self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._fh.flush()
            return rec

    def close(self):
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()