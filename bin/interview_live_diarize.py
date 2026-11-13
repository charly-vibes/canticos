#!/usr/bin/env python3
"""Purpose: Post-hoc N-speaker diarization for interview-live sessions.
Responsibilities:
  - Parse pyannote RTTM output into speaker turns
  - Re-label existing transcript lines by timestamp overlap (no re-transcription)
  - Remap diarizer labels to stable int ids (first appearance order)
Rationale: canticos-q73 — whisper-cli -di (tiny-diary) hard-caps 2 speaker
  clusters; pyannote gives N speakers from one audio. Transcription already
  happened (transcript.txt); we only re-attribute lines to speakers so the
  tagging layer (speakers.json, --set-speaker) works unchanged.
"""

import re
import subprocess
import sys
from pathlib import Path

WORKER = Path(__file__).with_name("interview_live_pyannote_worker.py")


def ts_to_ms(text):
    """'HH:MM:SS' or 'MM:SS' -> milliseconds; None when unparseable."""
    parts = (text or "").strip().split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    parts = [int(p) for p in parts]
    if len(parts) == 2:
        parts = [0] + parts
    h, m, s = parts
    return (h * 3600 + m * 60 + s) * 1000


def parse_rttm(text):
    """RTTM lines -> [(start_ms, end_ms, label)]. Malformed lines are skipped.

    Format: SPEAKER <file> <chn> <start> <dur> <> <> <label> <>"""
    turns = []
    for line in (text or "").splitlines():
        f = line.split()
        if len(f) < 8 or f[0] != "SPEAKER":
            continue
        try:
            start, dur = float(f[3]), float(f[4])
        except ValueError:
            continue
        label = f[7]
        if start < 0 or dur <= 0 or not label:
            continue
        turns.append((int(start * 1000), int((start + dur) * 1000), label))
    turns.sort()
    return turns


# legacy transcript line: [HH:MM:SS] (speaker N) text
DIARIZE_LINE_RE = re.compile(r"^\[([^\]]+)\]\s+\(speaker (\d+)\)\s?(.*)$")


def overlap_ms(line_start, line_end, turn_start, turn_end):
    return max(0, min(line_end, turn_end) - max(line_start, turn_start))


def assign_turn_speakers(lines, turns):
    """Transcript lines -> (remapped_lines, label_to_id).

    Each '(speaker N)' line is re-attributed to the diarizer turn with the
    max time overlap around the line's own span; lines with no overlapping
    turn keep their original id. Diarizer labels become ints in first
    appearance order (by turn start time) for consistency with the rest of
    the artifacts (events.jsonl, TUI role labels)."""
    label_ids = {}
    for _, _, label in turns:
        if label not in label_ids:
            label_ids[label] = len(label_ids)
    out = []
    for line in lines:
        m = DIARIZE_LINE_RE.match(line)
        if not m:
            out.append(line)
            continue
        start = ts_to_ms(m.group(1))
        text = m.group(3)
        if start is None:
            out.append(line)
            continue
        # line end is unknown (no end ts in transcript lines); treat the line
        # as a point and pick the turn containing/near it with max overlap of
        # a 1s probe window
        probe_s, probe_e = start, start + 1000
        best_id, best_ov = None, 0
        for ts, te, label in turns:
            ov = overlap_ms(probe_s, probe_e, ts, te)
            if ov > best_ov:
                best_ov, best_id = ov, label_ids[label]
        if best_id is None:
            out.append(line)
            continue
        out.append(f"[{m.group(1)}] (speaker {best_id}) {text}")
    return out, label_ids


def placeholder_speakers(label_ids, existing_names=None):
    """label map -> speakers.json content with placeholder names for every
    diarized id, preserving any already-named id (existing_names wins)."""
    existing = existing_names or {}
    out = {}
    for label, sid in sorted(label_ids.items(), key=lambda kv: kv[1]):
        key = str(sid)
        out[key] = existing.get(key) or {"name": f"Speaker {sid}"}
    return out


def run_pyannote(wav_path, timeout=1800, on_error=None):
    """Run pyannote on one wav via a uv-managed ephemeral env (torch ~2GB
    stays out of interview-live's runtime env). Returns RTTM text, or None
    on failure — on_error(kind, detail) is called instead of raising.
    Requires HF_TOKEN in the environment for the gated pyannote models.
    CPU-only torch index: most machines running this have no NVIDIA GPU."""
    cmd = ["uv", "run", "--no-project", "--python", "3.12",
           "--index-strategy", "unsafe-best-match",
           "--index-url", "https://download.pytorch.org/whl/cpu",
           "--extra-index-url", "https://pypi.org/simple",
           "--with", "pyannote.audio", "--with", "torch", "--with", "torchaudio",
           "python", str(WORKER), str(wav_path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if on_error:
            on_error("timeout", timeout)
        return None
    except FileNotFoundError:
        if on_error:
            on_error("uv-missing", None)
        return None
    if r.returncode != 0:
        if on_error:
            on_error("exit", (r.stderr or "").strip()[-500:])
        return None
    return r.stdout
