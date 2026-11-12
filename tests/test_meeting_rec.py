#!/usr/bin/env python3
"""Purpose: Verify meeting-rec's CLI contract without touching real PipeWire audio.
Responsibilities: Exercise flag parsing, device detection wiring, ffmpeg argv
construction, output-path defaults, interrupt handling and failure modes through
the real Bash wrapper with fake ffmpeg/pactl binaries.
Rationale: canticos-a78 — meetings need mic + system audio on separate Opus
tracks; tests pin the exact ffmpeg argv so regressions surface before a meeting.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin" / "meeting-rec"

FFMPEG_FAKE = """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
Path(os.environ['ARGV_FILE']).write_text(json.dumps(sys.argv[1:]))
if os.environ.get('FAIL_FFMPEG'):
    print('fake ffmpeg boom', file=sys.stderr)
    sys.exit(1)
if os.environ.get('BLOCK_FFMPEG'):
    time.sleep(30)
"""


@pytest.fixture
def env(tmp_path):
    """Fake ffmpeg + pactl on PATH, HOME pointed at tmp."""
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text(FFMPEG_FAKE)
    ffmpeg.chmod(0o755)
    pactl = tmp_path / "pactl"
    pactl.write_text("""#!/usr/bin/env bash
case "$1" in
  get-default-source) echo test-mic ;;
  get-default-sink) echo test-sink ;;
  *) exit 1 ;;
esac
""")
    pactl.chmod(0o755)
    return dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}",
                HOME=str(tmp_path), ARGV_FILE=str(tmp_path / "argv.json"))


def run(env_, *args):
    return subprocess.run([str(BIN), *args], env=env_,
                          capture_output=True, text=True, timeout=10)


def argv(env_):
    return json.loads(Path(env_['ARGV_FILE']).read_text())


def out_path(env_, result, pattern=r'meeting-\d{8}-\d{6}\.mka'):
    import re
    m = re.search(r'Saved: (\S+)', result.stdout)
    assert m, f"no Saved line in: {result.stdout!r}"
    path = Path(m.group(1))
    assert re.search(pattern, path.name), path.name
    return path


def test_default_records_both_tracks(env):
    result = run(env)
    assert result.returncode == 0, result.stderr
    a = argv(env)
    path = out_path(env, result)
    assert a[:-1] == ['-hide_banner', '-nostats', '-loglevel', 'warning',
                      '-nostdin', '-n', '-f', 'pulse', '-i', 'test-mic',
                      '-f', 'pulse', '-i', 'test-sink.monitor',
                      '-map', '0', '-map', '1', '-c:a', 'libopus']
    assert a[-1] == str(path)
    assert path.parent == Path(env['HOME']) / 'Meetings'
    assert path.parent.is_dir()


@pytest.mark.parametrize('flag,expected', [
    ('-m', ['-n', '-f', 'pulse', '-i', 'test-mic', '-map', '0', '-c:a', 'libopus']),
    ('-s', ['-n', '-f', 'pulse', '-i', 'test-sink.monitor', '-map', '0', '-c:a', 'libopus']),
])
def test_single_source_modes(env, flag, expected):
    result = run(env, flag)
    assert result.returncode == 0, result.stderr
    a = argv(env)
    assert a[:5] == ['-hide_banner', '-nostats', '-loglevel', 'warning', '-nostdin']
    assert a[5:-1] == expected
    assert a[-1].endswith('.mka')


def test_output_override(env):
    custom = Path(env['HOME']) / 'call.mka'
    result = run(env, '-o', str(custom))
    assert result.returncode == 0, result.stderr
    assert argv(env)[-1] == str(custom)
    assert 'Saved' in result.stdout


def test_output_dir_env(env):
    outdir = Path(env['HOME']) / 'grabaciones'
    result = run({**env, 'MEETING_REC_DIR': str(outdir)})
    assert result.returncode == 0, result.stderr
    assert out_path(env, result).parent == outdir
    assert outdir.is_dir()


def test_sigint_still_saves(env):
    env['BLOCK_FFMPEG'] = '1'
    proc = subprocess.Popen([str(BIN)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.5)
    proc.send_signal(signal.SIGINT)
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0, stderr
    assert 'Saved:' in stdout


def test_sigterm_still_saves(env):
    env['BLOCK_FFMPEG'] = '1'
    proc = subprocess.Popen([str(BIN)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=10)
    assert proc.returncode == 0


def test_usage_exits_nonzero(env):
    result = run(env, '-h')
    assert result.returncode != 0
    assert 'Usage' in result.stdout + result.stderr


def test_ffmpeg_missing(tmp_path):
    # Bare PATH with only env+bash so the shebang resolves but ffmpeg/pactl don't
    bare = tmp_path / 'bare-bin'
    bare.mkdir()
    for tool in ('env', 'bash'):
        src = shutil_which(tool)
        assert src, tool
        os.symlink(src, bare / tool)
    env_ = dict(os.environ, PATH=str(bare), HOME=str(tmp_path))
    result = run(env_)
    assert result.returncode != 0
    assert 'ffmpeg' in result.stderr


def shutil_which(tool):
    for d in os.environ['PATH'].split(os.pathsep):
        cand = Path(d) / tool
        if cand.exists():
            return str(cand)
    return None


def test_pactl_failure(env):
    pactl = Path(env['PATH'].split(os.pathsep)[0]) / 'pactl'
    pactl.write_text('#!/usr/bin/env bash\nexit 1\n')
    pactl.chmod(0o755)
    result = run(env)
    assert result.returncode != 0
    assert result.stderr.strip()


def test_ffmpeg_failure_keeps_log(env):
    env['FAIL_FFMPEG'] = '1'
    result = run(env)
    assert result.returncode != 0
    assert 'ffmpeg failed' in result.stderr
    log = Path(result.stderr.split('recording log (')[1].split(')')[0])
    assert log.exists()
