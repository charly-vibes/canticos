#!/usr/bin/env python3
"""Purpose: Verify fi's public CLI without provider requests or real credentials.
Responsibilities: Exercise argv forwarding, validation, diagnostics and cleanup.
Rationale: GH #1 requires tests through the real Bash wrapper and a fake backend.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin" / "fi"
MODELS = [
    "openrouter/free",
    "thinkingmachines/inkling:free",
    "poolside/laguna-xs-2.1:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "recursive/flint-v1.0:free",
]


@pytest.fixture
def backend(tmp_path):
    pi = tmp_path / "pi"
    pi.write_text("""#!/usr/bin/env python3
import json, os, subprocess, sys, time
from pathlib import Path
Path(os.environ['ARGV_FILE']).write_text(json.dumps(sys.argv[1:]))
if os.environ.get('BLOCK_PI'):
    child = subprocess.Popen(['sleep', '30'])
    Path(os.environ['PID_FILE']).write_text(str(child.pid))
    child.wait()
sys.exit(int(os.environ.get('PI_EXIT', '0')))
""")
    pi.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}",
               HOME=str(tmp_path), ARGV_FILE=str(tmp_path / "argv.json"),
               PID_FILE=str(tmp_path / "pid"), OPENROUTER_API_KEY="")
    return env


def run(backend, *args):
    return subprocess.run([str(BIN), *args], env=backend,
                          capture_output=True, text=True, timeout=5)


def argv(backend):
    return json.loads(Path(backend['ARGV_FILE']).read_text())


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('syntax', ['positional', 'separate', 'equals'])
def test_exact_models(backend, model, syntax):
    args = {'positional': [model], 'separate': ['run', '--model', model],
            'equals': ['run', f'--model={model}']}[syntax]
    result = run(backend, *args, '--print')
    assert result.returncode == 0, result.stderr
    assert argv(backend)[:4] == ['--provider', 'openrouter', '--model', f'openrouter/{model}']
    assert '--print' in argv(backend)


@pytest.mark.parametrize('selector,model', [('inkling', MODELS[1]),
    ('inkling-s', 'thinkingmachines/inkling-small:free'), ('flint', 'recursive/flint-v1.0:free'),
    ('run', MODELS[0]),
    ('openrouter', MODELS[0])])
def test_alias_and_default(backend, selector, model):
    assert run(backend, selector, 'hello').returncode == 0
    assert argv(backend)[3] == f'openrouter/{model}'
    assert argv(backend)[-1] == 'hello'


def test_forwarding(backend):
    forwarded = ['--mode', 'json', '--print', '--name', 'literal name',
                 '--session', 'a session.jsonl', '--tools', 'read,bash',
                 '  quote\' " $() \\ \n\t--model=paid  ']
    assert run(backend, 'inkling', '--timeout', '2', *forwarded).returncode == 0
    assert argv(backend)[4:] == forwarded


@pytest.mark.parametrize('args', [
    ['--model'], ['--model='], ['--model', 'paid/model'],
    ['--model', '--print'], ['--model', ''], ['--provider'],
    ['--provider', 'anthropic'], ['--provider=other'], ['--timeout'],
    *[[f'--timeout={v}'] for v in ['', '0', '0.0', '-1', 'NaN', 'inf', '1s', '1e999']],
])
def test_invalid_input_never_dispatches(backend, args):
    result = run(backend, 'run', *args)
    assert result.returncode != 0
    assert not Path(backend['ARGV_FILE']).exists()


def test_literal_option_values_and_separator(backend):
    args = ['--name', '--model=paid', '--session', '--timeout=0',
            '--', '--provider=other', '--model', 'paid', '']
    assert run(backend, 'run', *args).returncode == 0
    assert argv(backend)[4:] == args


@pytest.mark.parametrize('command', ['doctor', 'list', 'ls', '--help'])
def test_info_commands(backend, command):
    result = run(backend, command)
    assert result.returncode == 0
    assert 'free' in result.stdout.lower()
    assert not Path(backend['ARGV_FILE']).exists()


@pytest.mark.parametrize('environment_key', ['', 'secret-environment-key'])
def test_doctor_redacts_credentials(backend, environment_key):
    auth = Path(backend['HOME']) / '.pi/agent/auth.json'
    auth.parent.mkdir(parents=True)
    auth.write_text('{"openrouter":{"key":"secret-file-key"}}')
    backend['OPENROUTER_API_KEY'] = environment_key
    result = run(backend, 'doctor')
    assert result.returncode == 0
    assert 'secret-file-key' not in result.stdout + result.stderr
    assert 'secret-environment-key' not in result.stdout + result.stderr


def test_backend_exit_status(backend):
    backend['PI_EXIT'] = '17'
    assert run(backend, 'run').returncode == 17


def alive(pid):
    stat = Path(f'/proc/{pid}/stat')
    return stat.exists() and stat.read_text().split()[2] != 'Z'


@pytest.mark.parametrize('outer', [False, True])
def test_timeout_cleans_descendants(backend, outer):
    backend['BLOCK_PI'] = '1'
    command = [str(BIN), 'run', '--timeout', '30' if outer else '0.2']
    if outer:
        command = ['timeout', '--kill-after=1s', '0.5', *command]
    process = subprocess.Popen(command, env=backend, start_new_session=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child = None
    try:
        deadline = time.monotonic() + 3
        while not Path(backend['PID_FILE']).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert Path(backend['PID_FILE']).exists(), 'backend never started'
        child = int(Path(backend['PID_FILE']).read_text())
        assert process.wait(timeout=4) == 124
        deadline = time.monotonic() + 1
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not alive(child), 'timeout left a live descendant'
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        if child and alive(child):
            os.kill(child, signal.SIGKILL)


@pytest.mark.parametrize('option', ['--session-id', '--fork', '-t', '--exclude-tools',
                                   '-xt', '--api-key'])
def test_pi_option_values_are_not_wrapper_options(backend, option):
    forwarded = [option, '--timeout=0', '--name=experiment', '--print']
    assert run(backend, 'run', *forwarded).returncode == 0
    assert argv(backend)[4:] == forwarded


@pytest.mark.parametrize('name', [['--name=experiment'], ['-n', 'experiment']])
def test_name_and_provider_forwarding(backend, name):
    args = [*name, '--print']
    assert run(backend, '--timeout=2', '--provider=openrouter', *args).returncode == 0
    assert argv(backend)[4:] == args


def test_unknown_positional_model_is_rejected(backend):
    assert run(backend, 'typo', 'hello').returncode != 0
    assert not Path(backend['ARGV_FILE']).exists()
