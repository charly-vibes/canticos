#!/usr/bin/env python3
"""Purpose: pyannote diarization worker for interview-live (--diarize).
Responsibilities:
  - Load pyannote speaker-diarization-3.1 (HF_TOKEN from env for gated access)
  - Run it on one wav and print RTTM to stdout (SPEAKER lines only)
Rationale: canticos-q73 — pyannote needs torch (~2GB), so it runs in a
  uv-managed ephemeral env driven by interview_live_diarize.run_pyannote,
  never in interview-live's own imports; stdout is pure RTTM for parsing.
"""

import os
import sys


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: interview_live_pyannote_worker.py WAV\n")
        return 2
    wav = sys.argv[1]
    from pyannote.audio import Pipeline  # heavy import: only in this worker
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=os.environ.get("HF_TOKEN") or True,
    )
    dia = pipeline(wav)
    for turn, _, label in dia.itertracks(yield_label=True):
        sys.stdout.write(
            f"SPEAKER {wav} 1 {turn.start:.3f} {turn.duration:.3f} <NA> <NA> {label} <NA>\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
