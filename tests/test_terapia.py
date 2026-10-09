"""Tests for terapia — the pi session query CLI."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

TERAPIA = Path(__file__).resolve().parent.parent / "bin" / "terapia"


def run(*args, env_sessions=None):
    env = None
    if env_sessions is not None:
        import os
        env = dict(os.environ, TERAPIA_SESSIONS=str(env_sessions))
    return subprocess.run(
        ["nu", "--no-config-file", str(TERAPIA), *args],
        capture_output=True, text=True, env=env,
    )


def write_session(root, project, ts, sid, cwd, entries):
    d = root / f"--{project.replace('/', '--')}--"
    d.mkdir(parents=True, exist_ok=True)
    name = f"{ts.replace(':', '-').replace('.', '-')}Z_{sid}.jsonl"
    entries[0]["timestamp"] = f"{ts}.000Z"
    with open(d / name, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return sid


def session_header(ts, sid, cwd):
    return {"type": "session", "version": 3, "id": sid, "timestamp": ts, "cwd": cwd}


def user_msg(text):
    return {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def tool_call(call_id, name, args):
    return {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": call_id, "name": name, "arguments": args}]}}


def tool_result(call_id, name, text, is_error=False):
    return {"type": "message", "message": {"role": "toolResult", "toolCallId": call_id,
            "toolName": name, "content": [{"type": "text", "text": text}], "isError": is_error}}


def sample_session(sid, cwd="/home/sasha/para/x"):
    return [
        session_header("2026-10-09T10:00:00.000Z", sid, cwd),
        user_msg("hola"),
        tool_call("c1", "bash", {"command": "rg foo bar.txt"}),
        tool_result("c1", "bash", "No such file or directory: bar.txt\nexit code 1", True),
        tool_call("c2", "read", {"path": "x.py"}),
        tool_result("c2", "read", "oldText not found", True),
        tool_call("c3", "bash", {"command": "cargo test"}),
        tool_result("c3", "bash", "all green", False),
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "done"}]}},
    ]


def test_list(tmp):
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/home/sasha/a/proj", sample_session("sid-aaa"))
    r = run("list", env_sessions=tmp)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert len(out) == 1
    s = out[0]
    assert s["id"] == "sid-aaa"
    assert s["project"] == "a/proj"
    assert s["messages"] == 5
    assert s["tool_calls"] == 3
    assert s["errors"] == 2


def test_list_filters_by_project(tmp):
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    write_session(tmp, "b/proj", "2026-10-09T11:00:00", "sid-bbb", "/b/proj", sample_session("sid-bbb"))
    r = run("list", "--project", "b/proj", env_sessions=tmp)
    out = json.loads(r.stdout)
    assert [s["id"] for s in out] == ["sid-bbb"]


def test_list_window(tmp):
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    write_session(tmp, "a/proj", "2026-10-20T10:00:00", "sid-bbb", "/a/proj", sample_session("sid-bbb"))
    r = run("list", "--since", "2026-10-15", env_sessions=tmp)
    out = json.loads(r.stdout)
    assert [s["id"] for s in out] == ["sid-bbb"]


def test_errors_classified(tmp):
    sid = write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    r = run("errors", sid, env_sessions=tmp)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    classes = [e["class"] for e in out]
    assert classes == ["file_not_found", "edit_mismatch"]


def test_errors_grouped(tmp):
    sid = write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    r = run("errors", sid, "--group-by", "class", env_sessions=tmp)
    out = json.loads(r.stdout)
    assert out == {"file_not_found": 1, "edit_mismatch": 1}


def test_errors_all_across_sessions(tmp):
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    write_session(tmp, "b/proj", "2026-10-09T11:00:00", "sid-bbb", "/b/proj", sample_session("sid-bbb"))
    r = run("errors", "--all", env_sessions=tmp)
    out = json.loads(r.stdout)
    assert len(out) == 4
    assert all(e["class"] in ("file_not_found", "edit_mismatch") for e in out)


def test_tools(tmp):
    sid = write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    r = run("tools", sid, env_sessions=tmp)
    out = json.loads(r.stdout)
    assert [(t["tool"], t["isError"]) for t in out] == [("bash", True), ("read", True), ("bash", False)]
    assert out[0]["input"] == {"command": "rg foo bar.txt"}

    r = run("tools", sid, "--tool", "bash", env_sessions=tmp)
    out = json.loads(r.stdout)
    assert all(t["tool"] == "bash" for t in out)

    r = run("tools", sid, "--errors-only", env_sessions=tmp)
    out = json.loads(r.stdout)
    assert len(out) == 2 and all(t["isError"] for t in out)


def test_raw(tmp):
    sid = write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    r = run("raw", sid, "--type", "session", env_sessions=tmp)
    out = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    assert len(out) == 1 and out[0]["type"] == "session"


def test_session_prefix_resolution(tmp):
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    r = run("tools", "sid-aa", env_sessions=tmp)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)

    r = run("tools", "sid-zzz", env_sessions=tmp)
    assert r.returncode != 0 and "not found" in r.stderr.lower()


def test_no_heredoc_needed_smoke(tmp):
    # the whole point: one command answers what used to take a bespoke script
    sid = write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    r = run("stats", sid, env_sessions=tmp)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["tool_calls"] == 3 and out["errors"] == 2 and out["by_class"] == {"file_not_found": 1, "edit_mismatch": 1}


if __name__ == "__main__":
    import tempfile as tf
    failures = 0
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        with tf.TemporaryDirectory() as d:
            try:
                fn(Path(d))
                print(f"ok   {name}")
            except Exception:
                failures += 1
                import traceback
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
