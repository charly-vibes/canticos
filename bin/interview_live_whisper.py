#!/usr/bin/env python3
"""Purpose: whisper-cli transcription for interview-live (no capture/pi logic).
Responsibilities:
  - Run whisper-cli with diarization (-di) at a rising -ot offset
  - Parse its `[MM:SS.mmm --> MM:SS.mmm] (speaker N) text` output into segments
  - Degrade gracefully: missing binary / nonzero exit / timeout return [] and
    surface the failure through an on_error callback instead of crashing the loop
Rationale: canticos-9x1 — modularizing the monolith: transcription is a pure
  subprocess+parse concern that the coordinator can swap, fake in tests, or
  paginate without touching capture or suggestion code.
"""

import re
import subprocess
import sys

SEGMENT_RE = re.compile(
    r"\[(\d{2}):(\d{2}):(\d{2})\.\d{3} --> (\d{2}):(\d{2}):(\d{2})\.\d{3}\]\s*\(speaker (\d+)\)\s+(.*)"
)


def ts_to_ms(h, m, s):
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000


def parse_segments(text, offset_ms):
    """Whisper stdout -> [(start_ms, end_ms, speaker, text)] for new [start >= offset] segments."""
    segs = []
    for line in (text or "").splitlines():
        m = SEGMENT_RE.match(line.strip())
        if not m:
            continue
        start = ts_to_ms(*m.groups()[:3])
        end = ts_to_ms(*m.groups()[3:6])
        spk, text = int(m.group(7)), m.group(8)
        if start >= offset_ms:
            segs.append((start, end, spk, text))
    return segs


def run_whisper(whisper_model, language, offset_ms, wav_path, timeout=300, on_error=None, shift_ms=0):
    """Transcribe new audio since offset_ms. Returns segments; never raises on
    whisper failure — on_error(kind, detail) is called and [] is returned so the
    interview continues without the round.

    shift_ms: add to all returned timestamps WITHOUT seeking (used by --replay
    on sliced windows, where -ot would seek past the short window file)."""
    cmd = [
        "whisper-cli", "-m", whisper_model, "-l", language,
        "-di", "-ot", str(offset_ms), "-f", str(wav_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if on_error:
            on_error("timeout", None)
        sys.stderr.write(f"interview-live: whisper-cli timed out after {timeout}s — skipping round\n")
        return []
    except FileNotFoundError:
        if on_error:
            on_error("missing", None)
        sys.stderr.write("interview-live: whisper-cli not found — skipping round\n")
        return []
    if r.returncode != 0:
        if on_error:
            on_error("exit", r.returncode)
        sys.stderr.write(f"interview-live: whisper-cli failed (exit {r.returncode})\n")
        return []
    segs = parse_segments(r.stdout, offset_ms)
    if shift_ms:
        segs = [(s + shift_ms, e + shift_ms, sp, t) for s, e, sp, t in segs]
    return segs