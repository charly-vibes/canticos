#!/usr/bin/env python3
"""Purpose: Verify interview-live's CLI contract without real audio, whisper or pi.
Responsibilities: Exercise capture path selection (live vs --from), whisper -di
diarization argv (offsets, language, model), transcript accumulation/dedup,
pi suggestion session continuity, and failure handling through the real script
with fake ffmpeg/whisper-cli/pi binaries.
Rationale: canticos-59c — interviewing copilot must pin its transcript pipeline
and suggestion loop so regressions surface before a real interview.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin" / "interview-live"

FFMPEG_FAKE = """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
with open(os.environ['FFMPEG_LOG'], 'a') as fh:
    fh.write(json.dumps(sys.argv[1:]) + '\\n')
out = [a for a in sys.argv[1:] if not a.startswith('-')][-1]
Path(out).write_bytes(b'RIFF' + b'\\x00' * 400)
if os.environ.get('FFMPEG_LIVE'):
    time.sleep(60)
"""

# Emits segment block A when called at offset 0, block B at later offsets,
# simulating real incremental whisper output (block B starts at 14s).
WHISPER_FAKE = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
with open(os.environ['WHISPER_LOG'], 'a') as fh:
    fh.write(json.dumps(sys.argv[1:]) + '\\n')
args = sys.argv
if '-ot' in args and args[args.index('-ot') + 1] == '0':
    lines = [
        "[00:00:01.000 --> 00:00:04.000]  (speaker 0) Hola, cuentame sobre tu ultimo proyecto",
        "[00:00:05.000 --> 00:00:09.000]  (speaker 1) Bueno, el mes pasado tuvimos un problema con el proveedor",
        "[00:00:10.000 --> 00:00:13.000]  (speaker 0) Y como lo resolviste?",
    ]
else:
    lines = [
        "[00:00:14.000 --> 00:00:17.000]  (speaker 1) Pagamos una multa y perdimos dos semanas",
        "[00:00:18.000 --> 00:00:20.000]  (speaker 0) Que multa fue esa y cuanto te costo?",
    ]
print('\\n'.join(lines))
"""

FFPROBE_FAKE = """#!/usr/bin/env python3
import os, sys
if 'FFPROBE_OUT' in os.environ:
    sys.stdout.write(os.environ['FFPROBE_OUT'])
else:
    print('2')
    print('2')
"""

PI_FAKE = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
with open(os.environ['PI_LOG'], 'a') as fh:
    fh.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('PI_FAIL'):
    print("pi: provider error", file=sys.stderr)
    sys.exit(1)
print("Q1: Tell me more about the supplier problem and its cost.")
print("Q2: What did you do differently the following month?")
"""


@pytest.fixture
def env(tmp_path):
    bins = tmp_path / "bin"
    bins.mkdir()
    for name, code in [("ffmpeg", FFMPEG_FAKE), ("ffprobe", FFPROBE_FAKE),
                       ("whisper-cli", WHISPER_FAKE), ("pi", PI_FAKE)]:
        p = bins / name
        p.write_text(code)
        p.chmod(0o755)
    log = tmp_path / "logs"
    log.mkdir()
    return dict(os.environ, PATH=f"{bins}:{os.environ['PATH']}",
                HOME=str(tmp_path), INTERVIEW_DIR=str(tmp_path / "out"),
                FFMPEG_LOG=str(log / "ffmpeg.jsonl"),
                WHISPER_LOG=str(log / "whisper.jsonl"),
                PI_LOG=str(log / "pi.jsonl"),
                INTERVIEW_MIC="test-mic", INTERVIEW_MONITOR="test-sink.monitor")


def run(env_, *args, timeout=40, live=False):
    if live:
        env_ = {**env_, "FFMPEG_LIVE": "1"}
    return subprocess.run([str(BIN), *args], env=env_,
                          capture_output=True, text=True, timeout=timeout)


def log_lines(env_, name):
    path = Path(env_[f"{name}_LOG"])
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def out_files(env_, suffix=".txt"):
    d = Path(env_['INTERVIEW_DIR'])
    return sorted(d.glob(f"*{suffix}")) if d.exists() else []


def transcript_text(env_):
    files = [f for f in out_files(env_) if "transcript" in f.name]
    return files[0].read_text() if files else ""


def test_live_mode_capture_argv_and_offsets(env):
    r = run(env, "--live", "--rounds", "2", "--every", "1", "--min-segments", "1", live=True)
    assert r.returncode == 0, r.stderr
    ff = log_lines(env, "FFMPEG")
    assert ff, "ffmpeg never invoked"
    args = ff[0]
    assert "-f" in args and "pulse" in args
    assert "test-mic" in args and "test-sink.monitor" in args
    # stereo join after mono downmix of both sources (inside the filter string)
    assert any("pan=mono" in e and "join" in e and "channel_layout=stereo" in e for e in args)
    # whisper: pass 1 offset 0, pass 2 offset = 13s end of last segment
    w = log_lines(env, "WHISPER")
    assert len(w) == 2
    assert "-di" in w[0]
    assert w[0][w[0].index("-ot") + 1] == "0"
    assert w[1][w[1].index("-ot") + 1] == "13000", w[1]
    # transcript: block A (3 lines) + block B (2 lines), both speakers
    txt = transcript_text(env)
    assert txt.count("proveedor") == 1
    assert "multa" in txt and "speaker 0" in txt and "speaker 1" in txt


def test_live_mode_pi_session_continuity(env):
    run(env, "--live", "--rounds", "2", "--every", "1", "--min-segments", "1", live=True)
    calls = log_lines(env, "PI")
    assert len(calls) == 2
    first, second = calls[0], calls[1]
    # first call: --session-id + --system-prompt (Mom Test methodology) + transcript
    assert first[0] == "--session-id"
    assert "--system-prompt" in first
    joined = " ".join(first).lower()
    assert "mom test" in joined
    # second call: same session, no system prompt (methodology persists), new text
    assert second[0] == "--session-id"
    assert "--system-prompt" not in second
    # suggestions saved
    q = [f for f in out_files(env, ".md") if "questions" in f.name]
    assert q and "Q1" in q[0].read_text()


def test_from_mode_single_pass(env):
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    r = run(env, "--from", str(rec))
    assert r.returncode == 0, r.stderr
    ff = log_lines(env, "FFMPEG")
    assert ff and str(rec) in ff[0]
    # two-stream recording: distinct stream specifiers in the filter
    flt = next(e for e in ff[0] if "join=" in e)
    assert "[0:a:0]" in flt and "[0:a:1]" in flt
    w = log_lines(env, "WHISPER")
    assert len(w) == 1 and "-di" in w[0]
    assert "speaker 0" in transcript_text(env)
    pi = log_lines(env, "PI")
    assert len(pi) == 1 and pi[0][0] == "--session-id"
    assert "--system-prompt" in pi[0]


def test_from_mode_single_stereo_stream(env):
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    env['FFPROBE_OUT'] = '2\n'  # one stereo stream
    r = run(env, "--from", str(rec))
    assert r.returncode == 0, r.stderr
    ff = log_lines(env, "FFMPEG")
    flt = next(e for e in ff[0] if "join=" in e)
    # single-stream: channel-splitting pan, no :1 stream specifier
    assert "[0:a]pan=mono|c0=c0" in flt and "[0:a]pan=mono|c0=c1" in flt
    assert "[0:a:1]" not in flt


def test_default_pi_model_used(env):
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    run(env, "--from", str(rec))
    calls = log_lines(env, "PI")
    joined = " ".join(calls[0])
    assert "qwen-token-plan-individual/qwen3.8-flash" in joined
    assert "--model" in calls[0]


def test_pi_model_forwarded(env):
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    run(env, "--from", str(rec), "--pi-model", "anthropic/claude-3.7-sonnet")
    calls = log_lines(env, "PI")
    assert any("claude-3.7-sonnet" in str(e) for e in calls[0])
    assert "--model" in calls[0]


def test_language_forwarded_to_whisper(env):
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    run(env, "--from", str(rec), "--language", "es")
    w = log_lines(env, "WHISPER")
    assert "-l" in w[0] and "es" in w[0]


def test_pi_failure_is_nonfatal(env):
    env['PI_FAIL'] = '1'
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    r = run(env, "--from", str(rec))
    assert r.returncode == 0, r.stderr
    assert "speaker 0" in transcript_text(env)
    assert "pi" in r.stderr.lower()


def test_missing_whisper_errors(env):
    # isolate PATH so the real whisper-cli (linuxbrew) is unreachable
    bins = Path(env['PATH'].split(os.pathsep)[0])
    isolated = dict(env, PATH=f"{bins}:/usr/bin:/bin")
    whisper = bins / "whisper-cli"
    whisper.rename(whisper.with_name("whisper-cli.bak"))
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    r = run(isolated, "--from", str(rec))
    whisper.with_name("whisper-cli.bak").rename(whisper)
    assert r.returncode != 0
    assert "whisper" in r.stderr.lower()