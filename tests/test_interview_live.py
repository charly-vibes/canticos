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
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import interview_live_core as core  # noqa: E402
import interview_live_events as evlog  # noqa: E402
import interview_live_suggest as sug  # noqa: E402

BIN = Path(__file__).resolve().parents[1] / "bin" / "interview-live"

FFMPEG_FAKE = """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
with open(os.environ['FFMPEG_LOG'], 'a') as fh:
    fh.write(json.dumps(sys.argv[1:]) + '\\n')
out = [a for a in sys.argv[1:] if not a.startswith('-')][-1]
sys.stderr.write('ffmpeg-fake: diagnostic line\\n')
sys.stderr.flush()
Path(out).write_bytes(b'RIFF' + b'\\x00' * 400)
if os.environ.get('FFMPEG_LIVE'):
    time.sleep(60)
"""

# Emits segment block A when called at offset 0, block B at later offsets,
# simulating real incremental whisper output (block B starts at 14s).
WHISPER_FAKE = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
if 'ORDER_LOG' in os.environ:
    with open(os.environ['ORDER_LOG'], 'a') as fh:
        fh.write('W\\n')
with open(os.environ['WHISPER_LOG'], 'a') as fh:
    fh.write(json.dumps(sys.argv[1:]) + '\\n')
args = sys.argv
shift = 0
for a in args:
    if a.endswith('.wav') and 'window-' in a:
        shift = int(a.split('window-')[1].split('.wav')[0]) // 1000
if '-ot' in args and args[args.index('-ot') + 1] == '0' and shift == 0:
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
if 'format=duration' in ' '.join(sys.argv):
    sys.stdout.write(os.environ.get('FFPROBE_DURATION', '75.5'))
elif 'FFPROBE_OUT' in os.environ:
    sys.stdout.write(os.environ['FFPROBE_OUT'])
else:
    print('2')
    print('2')
"""

PI_FAKE = """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
if 'ORDER_LOG' in os.environ:
    with open(os.environ['ORDER_LOG'], 'a') as fh:
        fh.write('P-start\\n')
with open(os.environ['PI_LOG'], 'a') as fh:
    fh.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('PI_FAIL'):
    print("pi: provider error", file=sys.stderr)
    sys.exit(1)
if os.environ.get('PI_MODE') == 'json':
    print(json.dumps({
        "questions": [
            {"text": "Q-A: Tell me more about the supplier problem and its cost.", "priority": "high"},
            {"text": "Q-B: What did you do differently the following month?", "priority": "medium"},
        ],
        "topics": [
            {"name": "Proveedor", "priority": "high", "note": "supplier problems"},
            {"name": "Multa", "priority": "low", "note": "penalty cost"},
        ],
    }))
else:
    print("Q1: Tell me more about the supplier problem and its cost.")
    print("Q2: What did you do differently the following month?")
if 'ORDER_LOG' in os.environ:
    if os.environ.get('PI_SLEEP'):
        time.sleep(float(os.environ['PI_SLEEP']))
    with open(os.environ['ORDER_LOG'], 'a') as fh:
        fh.write('P-end\\n')
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
                ORDER_LOG=str(log / "order.txt"),
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


def test_default_whisper_model_prefers_best_local(env):
    """Without --whisper-model/$WHISPER_MODEL, pick local large-v3-turbo when
    present (verified better on gold-standard replay), else base path."""
    r = run(env, "--rounds", "1", "--every", "0.2", "--min-segments", "99", live=True)
    assert r.returncode == 0, r.stderr
    w = log_lines(env, "WHISPER")
    assert w[0][w[0].index("-m") + 1].endswith("ggml-base.bin"), w[0]
    models = Path(env["HOME"]) / "models"
    models.mkdir()
    (models / "ggml-large-v3-turbo.bin").write_bytes(b"ggml")
    env2 = dict(env, WHISPER_LOG=env["WHISPER_LOG"] + ".2")
    run(env2, "--rounds", "1", "--every", "0.2", "--min-segments", "99", live=True)
    w2 = log_lines(env2, "WHISPER")
    assert w2[0][w2[0].index("-m") + 1].endswith("ggml-large-v3-turbo.bin"), w2[0]


def test_replay_mode_windows_like_live(env):
    """--replay streams an existing recording through the rolling window
    transcription + suggestion pipeline exactly like a live interview (not the
    one-shot --from batch), so questions can be validated against a gold-
    standard conversation without a mic."""
    src = Path(env["HOME"]) / "gold.wav"
    src.write_bytes(b"RIFF")
    env_ = {**env, "FFPROBE_DURATION": "30"}
    r = run(env_, "--replay", str(src), "--window", "10", "--min-segments", "1")
    assert r.returncode == 0, r.stderr
    w = log_lines(env_, "WHISPER")
    # windows transcribe from 0 (a nonzero -ot would SEEK past the slice);
    # absolute time is applied as a shift, so timestamps land at 10s/20s/30s
    assert all(x[x.index("-ot") + 1] == "0" for x in w), w
    txt = transcript_text(env_)
    assert "[00:00:01]" in txt, txt
    assert "[00:00:24]" in txt, "window 2 (10s) must yield 24s-shifted lines: " + txt
    ff = log_lines(env_, "FFMPEG")
    assert any("-ss" in x for x in ff[1:]), "no window slicing calls"
    assert "proveedor" in txt
    # ran to end of audio without --rounds and flushed suggestions
    assert "P-end" in Path(env_["ORDER_LOG"]).read_text()


def test_live_mode_capture_argv_and_offsets(env):
    r = run(env, "--rounds", "2", "--every", "1", "--min-segments", "1", live=True)
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


def test_live_capture_overwrites_stale_file(env):
    """Regression: with a leftover capture.wav in the interview dir, ffmpeg
    exits immediately ('File already exists') unless -y is passed — the live
    argv must include -y so reusing an interview dir starts capturing."""
    out = Path(env["INTERVIEW_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "capture.wav").write_bytes(b"stale-audio")
    r = run(env, "--rounds", "1", "--every", "1", "--min-segments", "1", live=True)
    assert r.returncode == 0, r.stderr
    assert "exited early" not in r.stderr, r.stderr
    ff = log_lines(env, "FFMPEG")
    assert ff, "ffmpeg never invoked"
    assert "-y" in ff[0], ff[0]
    # ffmpeg stderr is captured to the interview dir for post-mortems
    errlog = out / "ffmpeg.log"
    assert errlog.exists() and "diagnostic line" in errlog.read_text()


def test_transcription_continues_while_pi_thinks(env):
    """Regression: the pi suggestion call ran inline in the worker loop and
    froze transcript updates for its whole duration; suggestions must run
    asynchronously so live transcription never waits on pi, and pending
    batches are flushed (joined) before exit."""
    env_ = {**env, "PI_SLEEP": "2"}
    r = run(env_, "--rounds", "4", "--every", "0.5", "--min-segments", "1", live=True)
    assert r.returncode == 0, r.stderr
    order = Path(env_["ORDER_LOG"]).read_text().split()
    w_idx = [i for i, x in enumerate(order) if x == "W"]
    assert len(w_idx) == 4, order
    assert "P-start" in order and "P-end" in order, order
    # rounds kept transcribing while the first pi batch was still thinking
    p0 = order.index("P-start")
    pend = order.index("P-end")
    assert any(p0 < i < pend for i in w_idx), order
    # transcript complete despite async suggestions
    txt = transcript_text(env_)
    assert "proveedor" in txt


def test_live_mode_pi_session_continuity(env):
    run(env, "--rounds", "2", "--every", "1", "--min-segments", "1", live=True)
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
    # suggestions saved (parsed from plain lines into bullet form)
    q = [f for f in out_files(env, ".md") if "questions" in f.name]
    assert q and "supplier problem" in q[0].read_text()


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


def test_participants_in_pi_system_prompt(env):
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    run(env, "--from", str(rec), "--participants", "3")
    calls = log_lines(env, "PI")
    assert calls and "--system-prompt" in calls[0]
    joined = " ".join(calls[0]).lower()
    assert "3" in joined and "participants" in joined


def test_participants_default_two(env):
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    run(env, "--from", str(rec))
    joined = " ".join(log_lines(env, "PI")[0]).lower()
    assert "2 participants" in joined


def test_queue_json_recurrence_and_dedupe(env):
    env['PI_MODE'] = 'json'
    r = run(env, "--rounds", "2", "--every", "1", "--min-segments", "1", live=True)
    assert r.returncode == 0, r.stderr
    des = [f for f in out_files(env, ".json") if "queue" in f.name]
    assert des, "queue.json never written"
    q = json.loads(des[0].read_text())
    assert q["participants"] == 2
    # both fake rounds emit the same two topics -> deduped with recurrences=2
    assert len(q["topics"]) == 2
    prov = next(t for t in q["topics"] if t["name"] == "Proveedor")
    multa = next(t for t in q["topics"] if t["name"] == "Multa")
    assert prov["mentions"] == 2
    # recurring high-priority topic outranks the low one
    assert prov["score"] > multa["score"]


def test_plain_pi_output_fallback(env):
    # default PI_MODE (plain) — legacy line output still lands in questions.md
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    r = run(env, "--from", str(rec))
    assert r.returncode == 0, r.stderr
    q = [f for f in out_files(env, ".md") if "questions" in f.name]
    assert q and "supplier problem" in q[0].read_text()
    # plain mode also writes a queue file with no topics
    des = [f for f in out_files(env, ".json") if "queue" in f.name]
    assert des
    data = json.loads(des[0].read_text())
    assert data["topics"] == []
    assert any("supplier problem" in it["text"] for it in data["questions"])


def test_queue_logic_unit(capsys):
    # unit-level recurrence: a topic seen across three batches outranks a one-off high
    now = 1000.0
    q = core.Queue()
    q.ingest(
        questions=[{"text": "Qn1", "priority": "medium"}],
        topics=[{"name": "slow-burn", "priority": "medium", "note": ""}],
        now=now)
    now += 100
    q.ingest(
        questions=[{"text": "Qn2", "priority": "low"}],
        topics=[{"name": "slow-burn", "priority": "low", "note": ""}, {"name": "one-off", "priority": "high", "note": ""}],
        now=now)
    assert q.topics["slow burn"]["mentions"] == 2
    assert q.topics["one off"]["mentions"] == 1
    # recurring topic (3 mentions, medium) beats a one-off high: 60+80 > 100
    now += 100
    q.ingest(topics=[{"name": "slow-burn", "priority": "medium", "note": ""}], now=now)
    assert q.topics["slow burn"]["mentions"] == 3
    ranked = [t["name"] for t in q.ranked_topics()]
    assert ranked[0] == "slow-burn"
    # questions preserved in arrival order (no topic dedupe interference)
    assert [it["text"] for it in q.ranked_questions()] == ["Qn1", "Qn2"]


def test_participants_validation(env):
    r = run(env, "--participants", "0")
    assert r.returncode != 0
    assert "participants" in r.stderr.lower()



def test_event_log_appends_typed_lines(tmp_path):
    log = evlog.EventLog(tmp_path / "events.jsonl")
    log.emit("session_start", mode="replay", participants=2)
    log.emit("transcript_segment", start_ms=1000, end_ms=4000, speaker=1, text="hola")
    log.emit("session_end", reason="replay", duration=1.5)
    log.close()
    lines = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines() if l.strip()]
    assert len(lines) == 3
    assert lines[0]["type"] == "session_start" and lines[0]["mode"] == "replay"
    assert lines[1]["type"] == "transcript_segment" and lines[1]["speaker"] == 1
    assert lines[2]["type"] == "session_end"
    assert all("ts" in e and "seq" in e for e in lines)
    assert [e["seq"] for e in lines] == [1, 2, 3]


def test_event_log_reopen_appends(tmp_path):
    p = tmp_path / "events.jsonl"
    evlog.EventLog(p).emit("a", n=1)
    evlog.EventLog(p).emit("b", n=2)  # new writer must append, not truncate
    lines = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert [e["type"] for e in lines] == ["a", "b"]



def test_from_mode_writes_event_log(env):
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    r = run(env, "--from", str(rec))
    assert r.returncode == 0, r.stderr
    files = [f for f in out_files(env, ".jsonl") if "events" in f.name]
    assert files, "events.jsonl never written"
    lines = [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]
    assert lines, "events.jsonl empty"
    assert all(isinstance(e, dict) and "type" in e and "ts" in e for e in lines)
    # timeline: session_start first, transcript + suggestions in between, session_end last
    assert lines[0]["type"] == "session_start"
    assert lines[0]["mode"] == "replay"
    assert any(e["type"] == "transcript_segment" and e["speaker"] == 1 for e in lines)
    assert any(e["type"] == "suggest_result" for e in lines)
    assert lines[-1]["type"] == "session_end"


def test_whisper_timeout_returns_empty(monkeypatch):
    """A hanging whisper-cli must not crash the session: TimeoutExpired is
    caught, the on_error callback fires, and transcribe returns []."""
    import interview_live_whisper as whisper
    from subprocess import TimeoutExpired

    errors = []

    def boom(*a, **k):
        raise TimeoutExpired("whisper-cli", 300)

    monkeypatch.setattr(whisper.subprocess, "run", boom)
    segs = whisper.run_whisper(
        "/models/ggml-base.bin", "es", 13000, "/tmp/capture.wav",
        on_error=lambda kind, detail=None: errors.append(kind))
    assert segs == []
    assert errors == ["timeout"]


def test_system_prompt_carries_participants():
    p = sug.system_prompt(3)
    assert "3 participants" in p
    assert "Mom Test" in p
    p2 = sug.system_prompt(2)
    assert "2 participants" in p2


def test_system_prompt_forces_grounding_and_bans_generic_questions():
    """Live runs produced batch questions that could be asked in any interview
    ('¿Qué pasó exactamente, paso a paso...?' x3). The prompt must demand
    anchoring on the guest's own words and ban unanchored probes."""
    p = sug.system_prompt(2).lower()
    assert "exact words" in p, "prompt must require quoting the guest"
    assert "any other interview" in p, "prompt must ban interchangeable questions"
    assert "tell me more" in p, "prompt must call out the generic 'tell me more' shape"
    assert "number" in p, "prompt must push for numbers/names/dates"


def test_effective_participants_tui_overrides_args():
    # TUI prompt answer (ui.participants) must win over the CLI default
    assert sug.effective_participants(2, None) == 2

    class Stub:
        participants = 4

    assert sug.effective_participants(2, Stub()) == 4