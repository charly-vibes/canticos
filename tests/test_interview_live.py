#!/usr/bin/env python3
"""Purpose: Verify interview-live's CLI contract without real audio, whisper or pi.
Responsibilities: Exercise capture path selection (live vs --from), whisper -di
diarization argv (offsets, language, model), transcript accumulation/dedup,
pi suggestion prompting (stateless contract + rolling window + validation and
repair), and failure handling through the real script
with fake ffmpeg/whisper-cli/pi binaries.
Rationale: canticos-59c — interviewing copilot must pin its transcript pipeline
and suggestion loop so regressions surface before a real interview.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import interview_live_core as core  # noqa: E402
import interview_live_diarize as dia  # noqa: E402
import interview_live_events as evlog  # noqa: E402
import interview_live_suggest as sug  # noqa: E402
import interview_live_trigger as trig  # noqa: E402
import interview_live_whisper as wh  # noqa: E402

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
if os.environ.get('WHISPER_STREAM'):
    ot = 0
    if '-ot' in args:
        ot = int(args[args.index('-ot') + 1]) // 1000
    def ts(t):
        return "%02d:%02d:%02d.000" % (t // 3600, t % 3600 // 60, t % 60)
    st = ot + 1
    lines = [
        "[%s --> %s]  (speaker 1) guest point %d" % (ts(st), ts(st + 2), ot),
        "[%s --> %s]  (speaker 0) follow up %d" % (ts(st + 4), ts(st + 6), ot),
    ]
elif '-ot' in args and args[args.index('-ot') + 1] == '0' and shift == 0:
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
# canticos-7h9: keyed on the --model value so the strong-model attempt can
# time out or fail while the --pi-model fallback still succeeds.
model = sys.argv[sys.argv.index('--model') + 1] if '--model' in sys.argv else ''
if model and model == os.environ.get('PI_SLOW_MODEL'):
    time.sleep(float(os.environ.get('PI_SLOW_S', '2')))
if model and model == os.environ.get('PI_FAIL_MODEL'):
    print("pi: provider error", file=sys.stderr)
    sys.exit(1)
if os.environ.get('PI_FAIL'):
    print("pi: provider error", file=sys.stderr)
    sys.exit(1)
VALID_JSON = {
    "questions": [
        {"text": "Q-A: Tell me more about the supplier problem and its cost.",
         "priority": "high", "grounds": "tuvimos un problema con el proveedor"},
        {"text": "Q-B: What did you do differently the following month?",
         "priority": "medium", "grounds": "Pagamos una multa"},
    ],
    "topics": [
        {"name": "Proveedor", "priority": "high", "note": "supplier problems"},
        {"name": "Multa", "priority": "low", "note": "penalty cost"},
    ],
}
EMPTY_JSON = {"questions": [], "topics": [{"name": "Senal", "priority": "medium", "note": "x"}]}
if os.environ.get('PI_MODE') in ('empty_first', 'empty_always'):
    counter = os.environ.get('PI_COUNT_FILE')
    n = 0
    if counter:
        p = Path(counter)
        n = int(p.read_text()) if p.exists() else 0
        p.write_text(str(n + 1))
    if os.environ['PI_MODE'] == 'empty_always' or n == 0:
        print(json.dumps(EMPTY_JSON))
    else:
        print(json.dumps(VALID_JSON))
elif os.environ.get('PI_MODE') == 'json':
    print(json.dumps(VALID_JSON))
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
                PI_COUNT_FILE=str(log / "pi_count.txt"),
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
    # session-dir layout (canticos-det): artifacts live under root/<session>/
    d = Path(env_['INTERVIEW_DIR'])
    return sorted(d.rglob(f"*{suffix}")) if d.exists() else []


def transcript_text(env_):
    files = [f for f in out_files(env_) if "transcript" in f.name]
    return files[0].read_text() if files else ""


def test_session_directory_layout(env):
    """canticos-det: artifacts live in <root>/<session>/, not the flat root.
    --session pins the dir name; repeated runs with the same name append."""
    r = run(env, "--session", "reunion-test", "--rounds", "1", "--every", "0.2",
            "--min-segments", "99", live=True)
    assert r.returncode == 0, r.stderr
    root = Path(env["INTERVIEW_DIR"])
    work = root / "reunion-test"
    assert work.is_dir()
    assert (work / "transcript.txt").exists()
    assert (work / "capture.wav").exists()
    assert (work / "events.jsonl").exists()
    # root stays clean: only session dirs, no stray artifacts
    assert not (root / "transcript.txt").exists()
    assert not (root / "capture.wav").exists()
    r2 = run(env, "--session", "reunion-test", "--rounds", "1", "--every", "0.2",
             "--min-segments", "99", live=True)
    assert r2.returncode == 0, r2.stderr
    txt = (work / "transcript.txt").read_text()
    assert txt.count("speaker 0") >= 2  # appended, not clobbered


def test_session_dir_auto_name(env):
    """canticos-det: without --session the dir is interview-YYYYMMDD-HHMMSS.
    canticos-ate: suggestion calls are stateless, so pi argv no longer carries
    the session name; the artifact directory contract is what matters here."""
    r = run(env, "--rounds", "1", "--every", "0.2", "--min-segments", "1",
            "--pi-model", "fake-model", live=True)
    assert r.returncode == 0, r.stderr
    root = Path(env["INTERVIEW_DIR"])
    dirs = [d.name for d in root.iterdir() if d.is_dir() and d.name.startswith("interview-")]
    assert len(dirs) == 1, dirs
    assert re.fullmatch(r"interview-\d{8}-\d{6}", dirs[0]), dirs
    pi_calls = log_lines(env, "PI")
    assert pi_calls and all("--session-id" not in call for call in pi_calls)


def test_session_name_rejects_path_traversal(env):
    """canticos-det: --session is filesystem-safe — no slashes, no '..'."""
    for bad in ("../escape", "a/b", ".."):
        r = run(env, "--session", bad, "--rounds", "1", live=True)
        assert r.returncode == 2, (bad, r.returncode, r.stderr)
        assert "--session" in r.stderr
        root = Path(env["INTERVIEW_DIR"])
        assert not (root / bad).exists()


def make_session(root, name, *, start_ts=None, end_ts=None, duration=None,
                 wav_bytes=None, transcript_lines=0, questions=0):
    """Fabricate a session dir with the artifacts --list summarizes."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    events = []
    if start_ts is not None:
        events.append({"ts": start_ts, "seq": 1, "type": "session_start",
                       "session": name, "mode": "live"})
    if end_ts is not None:
        events.append({"ts": end_ts, "seq": 2, "type": "session_end",
                       "duration": duration if duration is not None else 0.0})
    if events:
        (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    if wav_bytes:
        (d / "capture.wav").write_bytes(b"\0" * wav_bytes)
    if transcript_lines:
        (d / "transcript.txt").write_text(
            "\n".join(f"[00:{i:02d}:00] (speaker 0) linea" for i in range(transcript_lines)) + "\n")
    if questions:
        (d / "queue.json").write_text(json.dumps(
            {"questions": [{"text": f"q{i}", "priority": "medium"} for i in range(questions)],
             "topics": []}, ensure_ascii=False))
    return d


def test_list_sessions_newest_first(env):
    """canticos-cnb: --list shows sessions newest-first with artifact summary."""
    root = Path(env["INTERVIEW_DIR"])
    make_session(root, "interview-20260101-090000", start_ts=1767248400.0,
                 end_ts=1767252000.0, duration=3600.0, wav_bytes=1024,
                 transcript_lines=12, questions=4)
    make_session(root, "interview-20260102-100000", start_ts=1767338400.0)
    r = run(env, "--list")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.index("20260102") < out.index("20260101")  # newest first
    assert "12 lines" in out
    assert "4 questions" in out
    assert "1.0 KB" in out
    assert "no recording" in out


def test_list_json(env):
    """canticos-cnb: --list --json is machine-readable."""
    root = Path(env["INTERVIEW_DIR"])
    make_session(root, "interview-20260101-090000", start_ts=1767248400.0,
                 end_ts=1767252000.0, duration=3600.0, wav_bytes=2048,
                 transcript_lines=3, questions=2)
    r = run(env, "--list", "--json")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert len(data["sessions"]) == 1
    s = data["sessions"][0]
    assert s["name"] == "interview-20260101-090000"
    assert s["duration_s"] == 3600.0
    assert s["transcript_lines"] == 3
    assert s["questions"] == 2
    assert s["recording_bytes"] == 2048
    r2 = run(env, "--json")
    assert r2.returncode == 2 and "--list" in r2.stderr


def test_list_empty_root(env):
    """canticos-cnb: empty/missing root is a friendly no-op, exit 0."""
    r = run(env, "--list")
    assert r.returncode == 0, r.stderr
    assert "no sessions" in r.stdout.lower()


def test_list_flags_legacy_flat_root(env):
    """canticos-cnb: artifacts directly in the root (pre-session layout) are
    flagged so the user knows --adopt can move them into a session."""
    root = Path(env["INTERVIEW_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "capture.wav").write_bytes(b"\0" * 512)
    (root / "transcript.txt").write_text("[00:01:00] (speaker 0) hola\n")
    r = run(env, "--list")
    assert r.returncode == 0, r.stderr
    assert "--adopt" in r.stdout


def test_parse_segments_multidigit_speaker():
    """canticos-uzv: speaker ids above 9 must parse (regex was single \d)."""
    txt = "[00:00:01.000 --> 00:00:02.000]  (speaker 12) texto largo"
    segs = wh.parse_segments(txt, 0)
    assert segs == [(1000, 2000, 12, "texto largo")]


def test_speaker_label_named_and_fallback():
    """canticos-uzv: known id -> name; unknown -> Speaker N fallback."""
    assert core.speaker_label(1, {"1": {"name": "Ana"}}) == "Ana"
    assert core.speaker_label(12, {"12": {"name": "Bruno"}}) == "Bruno"
    assert core.speaker_label(0, {}) == "Speaker 0"


def test_render_segment_line_named_vs_legacy():
    """canticos-uzv: named speaker renders 'Name: text'; unnamed keeps the
    legacy '(speaker N)' form so old transcripts/consumers don't shift."""
    assert core.render_segment_line(65000, 1, "hola", {"1": {"name": "Ana"}}) == "[00:01:05] Ana: hola"
    assert core.render_segment_line(65000, 1, "hola", {}) == "[00:01:05] (speaker 1) hola"


def test_speaker_flag_wires_names_into_transcript(env):
    """canticos-uzv: --speaker N=Name names speakers in the live transcript
    and persists speakers.json (unnamed id 0 defaults to You)."""
    r = run(env, "--speaker", "1=Ana", "--rounds", "1", "--every", "0.2",
            "--min-segments", "99", live=True)
    assert r.returncode == 0, r.stderr
    root = Path(env["INTERVIEW_DIR"])
    work = next(d for d in root.iterdir() if d.is_dir())
    txt = (work / "transcript.txt").read_text()
    assert "Ana: Bueno, el mes pasado" in txt
    assert "You: Hola, cuentame" in txt
    spk = json.loads((work / "speakers.json").read_text())
    assert spk["1"]["name"] == "Ana" and spk["0"]["name"] == "You"


def test_set_speaker_rewrites_transcript(env):
    """canticos-uzv: post-hoc tagging rewrites transcript labels in place and
    persists speakers.json; the session dir is untouched otherwise."""
    root = Path(env["INTERVIEW_DIR"])
    d = root / "sess"
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(
        "[00:01:00] (speaker 0) hola\n"
        "[00:02:00] (speaker 1) que tal\n"
        "[00:03:00] (speaker 1) bien\n")
    r = run(env, "--session", "sess", "--set-speaker", "1=Ana", "--set-speaker", "0=Bruno")
    assert r.returncode == 0, r.stderr
    txt = (d / "transcript.txt").read_text()
    assert "[00:01:00] Bruno: hola" in txt
    assert "[00:02:00] Ana: que tal" in txt
    assert "speaker 0" not in txt and "speaker 1" not in txt
    spk = json.loads((d / "speakers.json").read_text())
    assert spk["1"]["name"] == "Ana" and spk["0"]["name"] == "Bruno"
    # malformed spec is a usage error
    r2 = run(env, "--session", "sess", "--set-speaker", "Ana")
    assert r2.returncode == 2 and "--set-speaker" in r2.stderr
    r3 = run(env, "--set-speaker", "1=Ana")  # needs --session
    assert r3.returncode == 2 and "--session" in r3.stderr


def test_adopt_moves_legacy_flat_root(env):
    """canticos-bm7: --adopt moves flat root artifacts into one adopt-<ts>
    session dir named after the oldest artifact; idempotent afterwards."""
    root = Path(env["INTERVIEW_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "capture.wav").write_bytes(b"\0" * 512)
    (root / "transcript.txt").write_text("[00:01:00] (speaker 0) hola\n")
    (root / "questions.md").write_text("# questions\n")
    (root / "queue.json").write_text(json.dumps({"questions": [{"text": "q"}], "topics": []}))
    (root / "events.jsonl").write_text(
        json.dumps({"ts": 1767248400.0, "seq": 1, "type": "session_start"}) + "\n")
    (root / "ffmpeg.log").write_text("log\n")
    (root / "window-5000.wav").write_bytes(b"\0" * 64)  # legacy replay slice
    r = run(env, "--adopt")
    assert r.returncode == 0, r.stderr
    dirs = [d for d in root.iterdir() if d.is_dir()]
    assert len(dirs) == 1, dirs
    d = dirs[0]
    assert d.name.startswith("adopt-")
    for f in ("capture.wav", "transcript.txt", "questions.md", "queue.json",
              "events.jsonl", "ffmpeg.log", "window-5000.wav"):
        assert (d / f).exists(), f
    assert not (root / "capture.wav").exists()
    r2 = run(env, "--adopt")  # idempotent: no flat artifacts left
    assert r2.returncode == 0 and "nothing to adopt" in r2.stdout.lower()
    assert [x for x in root.iterdir() if x.is_dir()] == [d]


def test_adopt_clean_root_is_noop(env):
    """canticos-bm7: session-organized roots are left alone (exit 0)."""
    root = Path(env["INTERVIEW_DIR"])
    make_session(root, "interview-20260101-090000", start_ts=1767248400.0)
    r = run(env, "--adopt")
    assert r.returncode == 0, r.stderr
    assert "nothing to adopt" in r.stdout.lower()
    r2 = run(env, "--adopt", "--json")
    assert r2.returncode == 2  # --json stays list-only


def test_parse_rttm_basic_and_malformed():
    """canticos-q73: RTTM lines -> (start_ms, end_ms, label); junk skipped."""
    rttm = (
        "SPEAKER cap 1 0.500 2.250 <NA> <NA> SPEAKER_00 <NA>\n"
        "SPEAKER cap 1 3.000 1.000 <NA> <NA> SPEAKER_01 <NA>\n"
        "junk line\n"
        "SPEAKER cap 1 bad float 1.0 <NA> <NA> SPEAKER_02 <NA>\n"
        "SPEAKER cap 1 5.000 -1.0 <NA> <NA> SPEAKER_03 <NA>\n"
    )
    turns = dia.parse_rttm(rttm)
    assert turns == [(500, 2750, "SPEAKER_00"), (3000, 4000, "SPEAKER_01")]


def test_assign_turn_speakers_overlap_and_first_appearance():
    """canticos-q73: lines re-attributed by max overlap; labels become ints in
    first-appearance order; no-overlap lines keep their original id."""
    lines = [
        "[00:00:01] (speaker 0) uno",
        "[00:00:04] (speaker 1) dos",
        "[00:00:06] (speaker 0) tres",
        "[00:01:00] (speaker 0) sin turnos",
    ]
    turns = [
        (500, 2500, "SPEAKER_07"),   # covers line 1
        (3500, 5500, "SPEAKER_02"),  # covers line 2
        (5500, 7000, "SPEAKER_07"),  # covers line 3 (same voice returns)
    ]
    out, ids = dia.assign_turn_speakers(lines, turns)
    assert ids == {"SPEAKER_07": 0, "SPEAKER_02": 1}
    assert out[0] == "[00:00:01] (speaker 0) uno"
    assert out[1] == "[00:00:04] (speaker 1) dos"
    assert out[2] == "[00:00:06] (speaker 0) tres"
    assert out[3] == "[00:01:00] (speaker 0) sin turnos"  # no turn: untouched


def test_assign_turn_speakers_max_overlap_wins():
    """canticos-q73: a line between two turns lands on the bigger overlap."""
    lines = ["[00:00:05] (speaker 1) frontera"]
    turns = [(3000, 5000, "A"), (5200, 8000, "B")]
    out, _ = dia.assign_turn_speakers(lines, turns)
    # 1s probe starting at 5s: overlap 0 with A (ends 5.0), ~0.8s with B
    assert out[0] == "[00:00:05] (speaker 1) frontera"


def test_placeholder_speakers_preserves_named():
    """canticos-q73: diarized ids get placeholder names; user-named ids win."""
    out = dia.placeholder_speakers({"SPEAKER_09": 1, "SPEAKER_00": 0},
                                   existing_names={"1": {"name": "Ana"}})
    assert out == {"0": {"name": "Speaker 0"}, "1": {"name": "Ana"}}


UV_FAKE = """#!/usr/bin/env python3
import os, sys
from pathlib import Path
with open(os.environ['UV_LOG'], 'a') as fh:
    fh.write(' '.join(sys.argv[1:]) + '\\n')
rttm = os.environ.get('UV_RTTM', '')
sys.stdout.write(rttm)
if os.environ.get('UV_ERR'):
    sys.stderr.write(os.environ['UV_ERR'])
sys.exit(int(os.environ.get('UV_FAIL', '0')))
"""


def test_diarize_session_rewrites_transcript(env):
    """canticos-q73 e2e: --session X --diarize runs the pyannote worker (via
    uv), re-attributes transcript lines by overlap, writes speakers.json
    placeholders, and logs diarize events."""
    import subprocess as sp
    bins = Path(env["PATH"].split(":")[0])
    uv = bins / "uv"
    uv.write_text(UV_FAKE)
    uv.chmod(0o755)
    root = Path(env["INTERVIEW_DIR"])
    d = root / "sess"
    d.mkdir(parents=True)
    (d / "capture.wav").write_bytes(b"RIFF")
    (d / "transcript.txt").write_text(
        "[00:00:01] (speaker 0) hola a los dos\n"
        "[00:00:04] (speaker 1) hola\n"
        "[00:00:06] (speaker 0) y como seguimos\n")
    env2 = {**env,
            "UV_LOG": str(root / "uv.jsonl"),
            "UV_RTTM": "SPEAKER cap 1 0.500 2.000 <NA> <NA> SPEAKER_00 <NA>\n"
                       "SPEAKER cap 1 3.500 1.500 <NA> <NA> SPEAKER_07 <NA>\n"
                       "SPEAKER cap 1 5.500 2.000 <NA> <NA> SPEAKER_00 <NA>\n"}
    r = sp.run([str(BIN), "--session", "sess", "--diarize"], env=env2,
               capture_output=True, text=True, timeout=40)
    assert r.returncode == 0, r.stderr
    txt = (d / "transcript.txt").read_text()
    assert "[00:00:01] (speaker 0) hola a los dos" in txt
    assert "[00:00:04] (speaker 1) hola" in txt
    assert "[00:00:06] (speaker 0) y como seguimos" in txt
    assert "speaker 2" not in txt
    spk = json.loads((d / "speakers.json").read_text())
    assert spk["0"]["name"] == "Speaker 0" and spk["1"]["name"] == "Speaker 1"
    uv_calls = (root / "uv.jsonl").read_text()
    assert "pyannote.audio" in uv_calls and "capture.wav" in uv_calls
    events = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "diarize_done" and e["speakers"] == 2 for e in events)
    r2 = sp.run([str(BIN), "--session", "sess", "--diarize"],
                env={**env2, "UV_FAIL": "1",
                     "UV_ERR": "GatedRepoError: 403 not in the authorized list\n"},
                capture_output=True, text=True, timeout=40)
    assert r2.returncode == 1 and "diarization failed" in r2.stderr.lower()
    assert "GatedRepoError" in r2.stderr  # the reason is surfaced, not swallowed


def test_diarize_requires_session_and_recording(env):
    """canticos-q73: --diarize without --session is a usage error; without
    capture.wav it fails cleanly."""
    r = run(env, "--diarize")
    assert r.returncode == 2 and "--session" in r.stderr
    root = Path(env["INTERVIEW_DIR"])
    d = root / "sess"
    d.mkdir(parents=True)
    r2 = run(env, "--session", "sess", "--diarize")
    assert r2.returncode == 1 and "capture.wav" in r2.stderr


def test_default_language_is_spanish(env):
    """Interviews are Spanish; per-window 'auto' misdetects on short clips,
    so es is the default and --language auto must be requested explicitly."""
    r = run(env, "--rounds", "1", "--every", "0.2", "--min-segments", "99", live=True)
    assert r.returncode == 0, r.stderr
    w = log_lines(env, "WHISPER")
    assert w[0][w[0].index("-l") + 1] == "es", w[0]


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
    env_ = {**env, "PI_SLEEP": "3"}
    # turn-end triggers only fire once the 1.2s quiet floor has passed across a
    # round boundary, so give rounds room (canticos-2iv changed the cadence).
    r = run(env_, "--rounds", "4", "--every", "2.5", "--min-segments", "1", live=True)
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
    assert len(pi) == 1 and pi[0][0] == "--print"
    assert "--session-id" not in pi[0]
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
    # two trigger points (round 2 turn-end, round 3 turn-end) -> two batches
    r = run(env, "--rounds", "3", "--every", "2", "--min-segments", "1", live=True)
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
    # canticos-a6q: the new payload fields are emitted for plain output too
    files = [f for f in out_files(env, ".jsonl") if "events" in f.name]
    lines = [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]
    res = next(e for e in lines if e["type"] == "suggest_result")
    assert "Q1: Tell me more about the supplier problem" in res["raw_output"]
    assert "proveedor" in res["input_lines"].lower()


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


def test_suggest_result_carries_input_and_raw_output(env):
    """canticos-a6q: suggest_result must record the exact prompt input sent to
    pi and pi's raw stdout, so a bad batch can be attributed to the prompt vs
    the transcript context after the fact. Both fields stay flat strings so
    jq filters like `.input_lines` work without nesting."""
    env['PI_MODE'] = 'json'
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    r = run(env, "--from", str(rec))
    assert r.returncode == 0, r.stderr
    files = [f for f in out_files(env, ".jsonl") if "events" in f.name]
    lines = [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]
    results = [e for e in lines if e["type"] == "suggest_result"]
    assert results, "no suggest_result event"
    for e in results:
        # a transcript line that was actually sent to pi
        assert isinstance(e["input_lines"], str)
        assert "proveedor" in e["input_lines"].lower()
        assert "[00:00:05] (speaker 1) Bueno, el mes pasado tuvimos un problema con el proveedor" in e["input_lines"]
        # pi's stdout verbatim, before any parsing/stripping
        assert isinstance(e["raw_output"], str)
        assert "Q-A: Tell me more about the supplier problem" in e["raw_output"]
        assert "Proveedor" in e["raw_output"]  # still the unparsed JSON blob
        assert e["raw_output"].endswith("\n")  # verbatim stdout, not the stripped `out`
    # unchanged suggestion behavior: the parsed payload is still there too
    first = results[0]
    assert any("supplier problem" in q for q in first["questions"])
    assert any(t["name"] == "Proveedor" for t in first["topics"])
    # input_lines keeps its delta meaning; the stateless prompt embeds it
    assert first["input_lines"] in log_lines(env, "PI")[0][-1]


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


# -- stateless prompt digest (canticos-ate) -----------------------------------

def test_pi_prompt_budget_is_p03():
    assert sug.PROMPT_BUDGET == 12000


def test_build_pi_cmd_is_stateless_and_carries_contract_every_round():
    """No --session-id: every suggestion call is self-contained and carries the
    Mom Test system contract, whether it is the first round or a later one."""
    pending = [{"text": "¿Cuánto pagaste?", "priority": "high", "last_seen": time.time()}]
    first = sug.build_pi_cmd("sess", True, "m1", 2, None, "NEW-ONE",
                             window_lines=["[00:00:01] (speaker 1) old context"],
                             pending=[])
    later = sug.build_pi_cmd("sess", False, "m1", 2, None, "NEW-TWO",
                             window_lines=["[00:00:01] (speaker 1) old context"],
                             pending=pending)
    for cmd in (first, later):
        assert "--session-id" not in cmd
        assert cmd[:2] == ["pi", "--print"]
        assert "--system-prompt" in cmd
        assert "Mom Test" in cmd[cmd.index("--system-prompt") + 1]
    # every round carries a full prompt: window, new lines, digest
    assert "old context" in first[-1] and "NEW-ONE" in first[-1]
    assert "PENDING QUESTIONS" in later[-1]
    assert "¿Cuánto pagaste?" in later[-1]
    # the first round has no pending digest to show
    assert "PENDING QUESTIONS" not in first[-1]


def test_prompt_budget_trims_window_first_then_digest():
    now = 1000.0
    window = [f"[00:{i:02d}] window line {i} " + "w" * 300 for i in range(40)]
    pending = [{"text": f"pending q{i} " + "p" * 200, "priority": "medium",
                "last_seen": now} for i in range(5)]
    prompt = sug.build_prompt("NEW-ESSENTIAL", window_lines=window,
                              pending=pending, now=now, max_chars=900)
    assert len(prompt) <= 900
    # new lines are never trimmed; the oldest window lines go first
    assert "NEW-ESSENTIAL" in prompt
    assert "window line 0" not in prompt
    # top digest entry survives while the tail is already gone
    assert "pending q0" in prompt
    assert "pending q4" not in prompt


def test_prompt_digest_skips_expired_and_caps_at_five():
    now = 1000.0
    stale = [{"text": f"stale {i}", "priority": "high", "last_seen": 0.0}
             for i in range(2)]
    fresh = [{"text": f"fresh {i}", "priority": "medium", "last_seen": now}
             for i in range(7)]
    lines = sug.digest_lines(stale + fresh, now=now)
    assert len(lines) == 5
    assert all("stale" not in line for line in lines)
    assert lines[0].startswith('1. "fresh 0"')


def test_pi_cmd_total_prompt_stays_within_budget():
    window = [f"[00:{i:02d}] " + "x" * 500 for i in range(40)]
    pending = [{"text": "q" * 400, "priority": "low", "last_seen": time.time()}
               for _ in range(5)]
    cmd = sug.build_pi_cmd("sess", False, "m", 2, None, "NEW",
                           window_lines=window, pending=pending)
    sys_prompt = cmd[cmd.index("--system-prompt") + 1]
    assert len(sys_prompt) + len(cmd[-1]) <= sug.PROMPT_BUDGET


def test_live_mode_suggestion_rounds_are_stateless(env):
    """Keep the first-round/later-round invariant under the stateless contract:
    round one has no earlier context or pending digest; later rounds carry the
    rolling window and the digest — and BOTH carry the system prompt."""
    # 3 rounds / 2s: round 3 fires the first batch (turn-end + quiet floor),
    # the end-of-run flush fires the second.
    run(env, "--rounds", "3", "--every", "2", "--min-segments", "1", live=True)
    calls = log_lines(env, "PI")
    assert len(calls) == 2
    first, second = calls
    for call in (first, second):
        assert "--session-id" not in call
        assert call[0] == "--print"
        assert "--system-prompt" in call
        assert "mom test" in call[call.index("--system-prompt") + 1].lower()
    # first batch: no rolling context, no pending digest yet
    assert "RECENT TRANSCRIPT WINDOW" not in first[-1]
    assert "PENDING QUESTIONS" not in first[-1]
    # later batch: window + digest of what was already suggested
    assert "RECENT TRANSCRIPT WINDOW" in second[-1]
    assert "PENDING QUESTIONS" in second[-1]
    assert "supplier problem" in second[-1].lower()
    # suggestions saved (parsed from plain lines into bullet form)
    q = [f for f in out_files(env, ".md") if "questions" in f.name]
    assert q and "supplier problem" in q[0].read_text()

# -- contract hardening (canticos-ap3) ----------------------------------------

WINDOW_ES = [
    "[00:00:05] (speaker 1) Bueno, el mes pasado tuvimos un problema con el proveedor",
    "[00:00:14] (speaker 1) Pagamos una multa y perdimos dos semanas",
]


def test_parse_suggestion_output_parses_grounds_quote():
    """The contract now carries grounds as its own field, not buried in text."""
    out = json.dumps({
        "questions": [{"text": "¿Cuánto pagaste?", "priority": "high",
                       "grounds": "Pagamos una multa"}],
        "topics": [],
    })
    qs, _ = core.parse_suggestion_output(out)
    assert qs[0]["grounds"] == "Pagamos una multa"
    assert not qs[0].get("contract_miss")


def test_parse_plain_lines_marks_contract_miss():
    """Legacy plain-line output must keep working but be countable as a miss."""
    qs, _ = core.parse_suggestion_output("Q1: Tell me about the supplier.\nQ2: Next?")
    assert len(qs) == 2
    assert all(q["contract_miss"] for q in qs)
    assert all(q["grounds"] == "" for q in qs)


def test_validate_round_requires_at_least_one_question():
    accepted, rejected, reasons = sug.validate_round([], [{"name": "x"}], WINDOW_ES)
    assert accepted == [] and rejected == []
    assert "no-questions" in reasons


def test_validate_round_verifies_spanish_grounds_fuzzily():
    """Light es morphology keeps accent/case/plural variants of the quote
    matching the transcript line (shared with the dedupe normalizer)."""
    qs = [{"text": "¿Cuánto pagaste?", "priority": "high",
           "grounds": "PAGAMOS una multa"}]
    accepted, rejected, reasons = sug.validate_round(qs, [], WINDOW_ES)
    assert len(accepted) == 1 and rejected == []
    assert accepted[0]["evidence"] == "grounds"
    assert "no-evidence" not in reasons


def test_validate_round_demotes_hallucinated_grounds_not_deletes():
    qs = [{"text": "¿Por qué gritó el gerente?", "priority": "high",
           "grounds": "el gerente nos grito de repente"}]
    accepted, rejected, reasons = sug.validate_round(qs, [], WINDOW_ES)
    assert len(accepted) == 1, "demoted question must never be deleted"
    assert accepted[0]["evidence"] == "no-evidence"
    assert rejected == []
    assert "no-evidence" in reasons


def test_validate_round_flags_too_many_topics_but_keeps_questions():
    topics = [{"name": f"t{i}"} for i in range(3)]
    accepted, _, reasons = sug.validate_round(
        [{"text": "q", "grounds": "Pagamos una multa"}], topics, WINDOW_ES)
    assert accepted and "too-many-topics" in reasons
    _, _, two = sug.validate_round([{"text": "q"}], topics[:2], WINDOW_ES)
    assert "too-many-topics" not in two


def test_validate_round_plain_fallback_is_contract_miss_but_kept():
    qs, _ = core.parse_suggestion_output("Q1: Tell me about the supplier.")
    accepted, rejected, reasons = sug.validate_round(qs, [], WINDOW_ES)
    assert len(accepted) == 1 and rejected == []
    assert accepted[0]["text"] == "Tell me about the supplier."
    assert "contract-miss" in reasons


def test_empty_batch_repairs_once_then_ingests(env):
    """A zero-question batch triggers exactly one repair round on the same
    model; the repaired batch is ingested and logged as suggest_repair."""
    env_ = {**env, "PI_MODE": "empty_first"}
    rec = Path(env_["HOME"]) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    r = run(env_, "--from", str(rec), "--session", "repair-once")
    assert r.returncode == 0, r.stderr
    calls = log_lines(env_, "PI")
    assert len(calls) == 2, "expected exactly one repair call"
    assert "--system-prompt" in calls[1]
    # the rejected output is quoted back into the repair prompt
    assert '"questions": []' in calls[1][-1]
    models = [c[c.index("--model") + 1] for c in calls]
    assert models[0] == models[1]
    events = session_events(env_, "repair-once")
    repairs = [e for e in events if e["type"] == "suggest_repair"]
    assert len(repairs) == 1
    assert repairs[0]["reason"] == "no-questions"
    assert '"questions": []' in repairs[0]["rejected"]
    assert not [e for e in events if e["type"] == "suggest_reject"]
    results = [e for e in events if e["type"] == "suggest_result"]
    assert results and results[0]["questions"], results
    q = [f for f in out_files(env_, ".json") if "queue" in f.name]
    assert q and json.loads(q[0].read_text())["questions"]


def test_empty_batch_after_repair_emits_suggest_reject(env):
    """No retry loops: a still-empty repair emits suggest_reject once."""
    env_ = {**env, "PI_MODE": "empty_always"}
    rec = Path(env_["HOME"]) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    r = run(env_, "--from", str(rec), "--session", "repair-reject")
    assert r.returncode == 0, r.stderr
    calls = log_lines(env_, "PI")
    assert len(calls) == 2, "repair must not loop"
    events = session_events(env_, "repair-reject")
    reps = [e for e in events if e["type"] == "suggest_repair"]
    rejects = [e for e in events if e["type"] == "suggest_reject"]
    assert len(reps) == 1 and len(rejects) == 1
    assert "no-questions" in rejects[0]["reasons"]
    assert not [e for e in events if e["type"] == "suggest_result"]


def test_repair_is_skipped_when_a_newer_batch_is_pending(env, monkeypatch):
    """The repair inherits the stale guard: a newer enqueued batch means the
    rejected output is no longer worth a second pi call."""
    rec = Path(env["HOME"]) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    mod = load_cli_module()
    args = mod.parse_args(["--from", str(rec), "--session", "stale-repair",
                           "--out", env["INTERVIEW_DIR"]])
    tool = mod.InterviewLive(args)
    empty = json.dumps({"questions": [], "topics": []})
    calls = []

    def fake_pi(cmd, timeout):
        calls.append(cmd)
        return empty, None

    monkeypatch.setattr(tool, "_run_pi", fake_pi)
    tool._suggest_seq = 2
    tool.suggest(["[00:00:05] (speaker 1) hola"], True, seq=1)
    tool.log.close()
    assert len(calls) == 1, "stale repair must be skipped"
    events = session_events(env, "stale-repair")
    assert not [e for e in events if e["type"] in ("suggest_repair", "suggest_reject")]


# -- trigger policy (canticos-2iv) -------------------------------------------
# Measured baseline: 222 batches / 83.5min (median 21s), 628 questions, 587
# manual dismissals. Batches must follow the conversation, not the round timer.

def test_trigger_policy_turn_end_after_quiet_floor():
    p = trig.TriggerPolicy(min_segments=1)
    p.add([(0, 2000, 1, "guest")], now=0.0)
    assert p.due(0.5) is None, "guest still holds the floor"
    p.add([(3000, 4000, 0, "interviewer")], now=1.0)  # 1s audio gap < 1.2s floor
    assert p.due(1.0) is None, "quiet floor not met yet"
    assert p.due(2.2) == "turn-end"
    assert p.drain() == [(0, 2000, 1, "guest"), (3000, 4000, 0, "interviewer")]
    assert p.due(9.0) is None, "a drained policy must not re-fire"


def test_trigger_policy_turn_end_uses_audio_pause():
    """Replay/from-file windows arrive back-to-back in wall time: a >=1.2s
    pause in the audio itself proves the guest yielded the floor."""
    p = trig.TriggerPolicy()
    p.add([(0, 2000, 1, "guest")], now=0.0)
    p.add([(5000, 6000, 0, "interviewer")], now=0.1)
    assert p.due(0.1) == "turn-end"


def test_trigger_policy_monologue_cap_fires_without_turn_end():
    p = trig.TriggerPolicy()
    p.add([(0, 12000, 1, "a"), (12000, 24000, 1, "b")], now=0)
    assert p.due(0) is None, "24s monologue is still under the 25s cap"
    p.add([(24000, 26000, 1, "c")], now=0)
    assert p.due(0) == "monologue"
    assert len(p.drain()) == 3


def test_trigger_policy_interviewer_only_never_fires():
    p = trig.TriggerPolicy()
    p.add([(0, 3000, 0, "q1"), (3000, 6000, 0, "q2")], now=0)
    assert p.due(1000.0) is None


def test_trigger_policy_coalesces_and_keeps_every_segment():
    p = trig.TriggerPolicy()
    segs = [(0, 1000, 1, "a"), (1000, 2000, 1, "b"), (3000, 4000, 0, "c")]
    p.add(segs[:2], now=0)
    assert p.pending_count == 2 and p.due(10) is None
    p.add(segs[2:], now=1)
    assert p.due(1) is None, "quiet floor starts at the turn-end observation"
    assert p.drain() == segs, "drain must return every segment exactly once"
    assert p.pending_count == 0 and p.drain() == []


def test_trigger_policy_min_segments_floor():
    p = trig.TriggerPolicy(min_segments=3)
    p.add([(0, 2000, 1, "g"), (4000, 5000, 0, "i")], now=0)
    assert p.due(99) is None, "below the configured floor"
    p.add([(6000, 7000, 0, "j")], now=1)
    assert p.due(99) == "turn-end"


def test_trigger_stale_batches_coalesce_without_losing_lines(env):
    """EDGE-002: when pi is still thinking and newer batches queue up, stale
    batches are dropped — but their lines must ride along with the newest one,
    so no transcript segment vanishes from the model's view.

    WHISPER_STREAM emits a >=1.2s audio pause after each guest turn, so every
    round produces a confirmed turn end; pi is slower than the round cadence,
    which builds the backlog the stale-drop path exists for."""
    env_ = {**env, "PI_SLEEP": "4", "WHISPER_STREAM": "1"}
    r = run(env_, "--rounds", "8", "--every", "0.2", "--min-segments", "1",
            live=True, timeout=60)
    assert r.returncode == 0, r.stderr
    calls = log_lines(env_, "PI")
    assert calls, "no suggestion batches ran"
    assert len(calls) < 5, f"stale-drop did not coalesce: {len(calls)} pi calls"
    prompts = "\n".join(" ".join(c) for c in calls)
    lost = [line for line in transcript_text(env_).splitlines()
            if line.strip() and line not in prompts]
    assert not lost, f"transcript lines lost from pi prompts: {lost}"
    events = [json.loads(line) for f in out_files(env_, ".jsonl")
              for line in f.read_text().splitlines() if line.strip()]
    assert any(e["type"] == "suggest_trigger" for e in events), events
    assert any(e["type"] == "suggest_stale_drop" for e in events), events


def test_plain_mode_emits_question_shown(env):
    """canticos-vvc: a plain stdout suggestion round logs question_shown for the
    questions it renders, exactly once each, and no question_asked when nobody
    interacted with the dashboard."""
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    r = run(env, "--from", str(rec))
    assert r.returncode == 0, r.stderr
    files = [f for f in out_files(env, ".jsonl") if "events" in f.name]
    assert files, "events.jsonl never written"
    lines = [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]
    shown = [e for e in lines if e["type"] == "question_shown"]
    assert shown, "no question_shown event"
    assert any("supplier problem" in e["text"] for e in shown)
    for e in shown:
        assert {"key", "text", "priority", "score"} <= set(e)
        assert e["priority"] in ("high", "medium", "low")
    assert len(shown) == len({e["key"] for e in shown}), "question_shown re-emitted"
    assert not [e for e in lines if e["type"] == "question_asked"]


# -- question model (canticos-7h9) --------------------------------------------

DEFAULT_PI_MODEL = "qwen-token-plan-individual/qwen3.8-flash"
STRONG_MODEL = "strong/question-model"


def load_cli_module():
    """Import the extension-less coordinator as a module for in-process tests."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("interview_live_cli_test", str(BIN))
    spec = importlib.util.spec_from_loader("interview_live_cli_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def session_events(env_, session):
    path = Path(env_["INTERVIEW_DIR"]) / session / "events.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_question_model_unset_pins_default_pi_argv(env):
    """Anti-goal: with --question-model unset the pi argv stays minimal (one
    call, one --model, no extra flags) — but canticos-ate made it stateless:
    no --session-id, and the system contract rides every round."""
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    r = run(env, "--from", str(rec), "--session", "argv-pin")
    assert r.returncode == 0, r.stderr
    calls = log_lines(env, "PI")
    assert len(calls) == 1
    result = next(e for e in session_events(env, "argv-pin") if e["type"] == "suggest_result")
    assert "--session-id" not in calls[0]
    assert calls[0][0] == "--print"
    assert calls[0][1:3] == ["--system-prompt", sug.system_prompt(2)]
    assert calls[0][3:5] == ["--model", DEFAULT_PI_MODEL]
    assert result["input_lines"] in calls[0][-1]
    assert not [e for e in session_events(env, "argv-pin") if e["type"] == "suggest_fallback"]


def test_question_model_flag_and_env_used_by_suggestion_path(env):
    """--question-model (and $INTERVIEW_QUESTION_MODEL) replace the model for
    the suggestion call; --pi-model stays the fallback default."""
    rec = Path(env['HOME']) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    r = run(env, "--from", str(rec), "--session", "qm-flag",
            "--question-model", STRONG_MODEL)
    assert r.returncode == 0, r.stderr
    calls = log_lines(env, "PI")
    assert len(calls) == 1
    assert calls[0][calls[0].index("--model") + 1] == STRONG_MODEL
    assert not [e for e in session_events(env, "qm-flag") if e["type"] == "suggest_fallback"]

    r2 = run({**env, "INTERVIEW_QUESTION_MODEL": "env/question-model"},
             "--from", str(rec), "--session", "qm-env")
    assert r2.returncode == 0, r2.stderr
    calls2 = log_lines(env, "PI")
    assert len(calls2) == 2
    assert calls2[-1][calls2[-1].index("--model") + 1] == "env/question-model"


@pytest.mark.parametrize("fault,reason", [
    ({"PI_SLOW_MODEL": STRONG_MODEL, "PI_SLOW_S": "2"}, "timeout"),
    ({"PI_FAIL_MODEL": STRONG_MODEL}, "exit-1"),
])
def test_question_model_falls_back_to_pi_model_once(env, monkeypatch, fault, reason):
    """A slow/failing strong model falls back once to --pi-model within the
    injectable soft budget, emits suggest_fallback, and marks the TUI status
    'done (fallback)'."""
    rec = Path(env["HOME"]) / "rec.mka"
    rec.write_bytes(b"fake-audio")
    # subprocess inherits the real os.environ mapping, so mutate it in place
    for key, value in dict(env, **fault).items():
        monkeypatch.setenv(key, value)
    mod = load_cli_module()
    args = mod.parse_args(["--from", str(rec), "--session", "qm-fallback",
                           "--question-model", STRONG_MODEL,
                           "--pi-model", "fallback/pi-model"])
    tool = mod.InterviewLive(args)

    class UiStub:
        participants = 2
        suggestion = "idle"

    tool.ui = UiStub()
    monkeypatch.setattr(sug, "QUESTION_MODEL_TIMEOUT_S", 0.3)
    started = time.monotonic()
    tool.suggest(["[00:00:01] (speaker 1) hola"], True)
    elapsed = time.monotonic() - started
    tool.log.close()
    assert elapsed < 2.0, elapsed  # P-04 soft budget is injectable, not real 45s
    calls = log_lines(env, "PI")
    assert [c[c.index("--model") + 1] for c in calls] == [STRONG_MODEL, "fallback/pi-model"]
    assert tool.ui.suggestion == "done (fallback)"
    events = session_events(env, "qm-fallback")
    fallbacks = [e for e in events if e["type"] == "suggest_fallback"]
    assert len(fallbacks) == 1
    fb = fallbacks[0]
    assert fb["reason"] == reason
    assert fb["strong_model"] == STRONG_MODEL
    assert fb["fallback_model"] == "fallback/pi-model"
    assert any(e["type"] == "suggest_result" for e in events)
    assert tool.queue.pending_questions()
