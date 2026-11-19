"""Tests for terapia — the pi + omp session query CLI."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TERAPIA = Path(__file__).resolve().parent.parent / "bin" / "terapia"


def run(*args, env_sessions=None):
    env = None
    if env_sessions is not None:
        import os
        omp = env_sessions / "omp"
        omp.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, TERAPIA_SESSIONS=str(env_sessions),
                   TERAPIA_OMP_SESSIONS=str(omp), TERAPIA_CACHE=str(env_sessions / "cache"))
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


def write_omp_session(root, project, ts, sid, cwd, entries, agent=None):
    # omp layout: <root>/<-project>/[<session-dir>/]<file>.jsonl; project dirs
    # encode "/" as "-" (single dash), mains at the project root, subagents nested.
    d = root / f"-{project.replace('/', '-')}"
    if agent is None:
        d.mkdir(parents=True, exist_ok=True)
        name = f"{ts.replace(':', '-').replace('.', '-')}Z_{sid}.jsonl"
    else:
        d = d / f"{ts.replace(':', '-').replace('.', '-')}Z_{sid}"
        d.mkdir(parents=True, exist_ok=True)
        name = f"{agent}.jsonl"
    entries[0]["timestamp"] = f"{ts}.000Z"
    with open(d / name, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return sid


def omp_main_session(sid):
    # omp puts a `title` entry before the `session` header.
    return [
        {"type": "title", "v": 1, "title": "Fix parser", "updatedAt": "2026-11-11T00:00:00.000Z"},
        session_header("2026-11-11T00:00:00.000Z", sid, "/var/home/sasha/x/repo"),
        user_msg("vamos"),
        tool_call("k1", "bash", {"command": "pytest -x"}),
        tool_result("k1", "bash", "Traceback (most recent call last):", True),
        tool_call("k2", "read", {"path": "x.py"}),
        tool_result("k2", "read", "all good", False),
    ]


def omp_agent_session(sid):
    # subagent transcripts carry the title on the session entry itself.
    return [
        dict(session_header("2026-11-11T01:00:00.000Z", sid, "/var/home/sasha/x/repo"), title="planner run"),
        tool_call("a1", "edit", {"path": "x.py"}),
        tool_result("a1", "edit", "oldText not found in file", True),
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


def test_cache_created_and_reused(tmp):
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    cache = tmp / "cache"
    env = dict(os.environ, TERAPIA_SESSIONS=str(tmp), TERAPIA_OMP_SESSIONS=str(tmp / "omp"), TERAPIA_CACHE=str(cache))
    r = subprocess.run(["nu", "--no-config-file", str(TERAPIA), "stats", "--all"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    first = json.loads(r.stdout)
    cache_files = list(cache.glob("*.json"))
    assert cache_files, "cache dir empty after first run"
    r = subprocess.run(["nu", "--no-config-file", str(TERAPIA), "stats", "--all"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    second = json.loads(r.stdout)
    assert second == first, "cached result differs from fresh scan"


def test_cache_invalidated_on_change(tmp):
    sid = write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    cache = tmp / "cache"
    env = dict(os.environ, TERAPIA_SESSIONS=str(tmp), TERAPIA_OMP_SESSIONS=str(tmp / "omp"), TERAPIA_CACHE=str(cache))
    r = subprocess.run(["nu", "--no-config-file", str(TERAPIA), "stats", "--all"],
                       capture_output=True, text=True, env=env)
    assert json.loads(r.stdout)["errors"] == 2
    # append a new failing tool result → must invalidate that file's cache entry
    with open(tmp / "--a--proj--" / f"2026-10-09T10-00-00Z_{sid}.jsonl", "a") as f:
        f.write(json.dumps({"type": "message", "message": {
            "role": "toolResult", "toolCallId": "c9", "toolName": "bash",
            "content": [{"type": "text", "text": "boom, exit code 1"}],
            "isError": True}}) + "\n")
    r = subprocess.run(["nu", "--no-config-file", str(TERAPIA), "stats", "--all"],
                       capture_output=True, text=True, env=env)
    out = json.loads(r.stdout)
    assert out["errors"] == 3, "stale cache after file change"
    assert out["by_class"].get("exit_nonzero") == 1


def test_no_heredoc_needed_smoke(tmp):
    # the whole point: one command answers what used to take a bespoke script
    sid = write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    r = run("stats", sid, env_sessions=tmp)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["tool_calls"] == 3 and out["errors"] == 2 and out["by_class"] == {"file_not_found": 1, "edit_mismatch": 1}


def test_list_includes_omp(tmp):
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-pi", "/a/proj", sample_session("sid-pi"))
    omp = tmp / "omp"
    write_omp_session(omp, "x/repo", "2026-11-11T00:00:00", "omp-main-1", "/var/home/sasha/x/repo", omp_main_session("omp-main-1"))
    write_omp_session(omp, "x/repo", "2026-11-11T01:00:00", "omp-sub-1", "/var/home/sasha/x/repo", omp_agent_session("omp-sub-1"), agent="Planner")
    r = run("list", env_sessions=tmp)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    pis = [s for s in out if s["source"] == "pi"]
    assert [s["id"] for s in pis] == ["sid-pi"]
    omps = {s["id"]: s for s in out if s["source"] == "omp"}
    assert set(omps) == {"omp-main-1", "omp-sub-1"}
    main = omps["omp-main-1"]
    assert main["project"] == "x/repo" and main["agent"] is None
    assert main["title"] == "Fix parser"
    assert main["tool_calls"] == 2 and main["errors"] == 1
    sub = omps["omp-sub-1"]
    assert sub["agent"] == "Planner"
    assert sub["title"] == "planner run"
    assert sub["tool_calls"] == 1 and sub["errors"] == 1


def test_list_source_filter(tmp):
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-pi", "/a/proj", sample_session("sid-pi"))
    write_omp_session(tmp / "omp", "x/repo", "2026-11-11T00:00:00", "omp-main-1", "/var/home/sasha/x/repo", omp_main_session("omp-main-1"))
    r = run("list", "--source", "omp", env_sessions=tmp)
    out = json.loads(r.stdout)
    assert [s["id"] for s in out] == ["omp-main-1"] and all(s["source"] == "omp" for s in out)
    r = run("list", "--source", "pi", env_sessions=tmp)
    out = json.loads(r.stdout)
    assert [s["id"] for s in out] == ["sid-pi"] and all(s["source"] == "pi" for s in out)


def test_stats_all_mixed_sources(tmp):
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-pi", "/a/proj", sample_session("sid-pi"))
    omp = tmp / "omp"
    write_omp_session(omp, "x/repo", "2026-11-11T00:00:00", "omp-main-1", "/var/home/sasha/x/repo", omp_main_session("omp-main-1"))
    write_omp_session(omp, "x/repo", "2026-11-11T01:00:00", "omp-sub-1", "/var/home/sasha/x/repo", omp_agent_session("omp-sub-1"), agent="Planner")
    r = run("stats", "--all", env_sessions=tmp)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["sessions"] == 3
    assert out["by_source"] == {"pi": 1, "omp": 2}
    assert out["errors"] == 4
    r = run("errors", "--all", env_sessions=tmp)
    rows = json.loads(r.stdout)
    assert {e["source"] for e in rows} == {"pi", "omp"}
    omp_classes = sorted(e["class"] for e in rows if e["source"] == "omp")
    assert omp_classes == ["edit_mismatch", "lang_runtime"]
    r = run("errors", "--all", "--source", "omp", env_sessions=tmp)
    rows = json.loads(r.stdout)
    assert len(rows) == 2 and all(e["source"] == "omp" for e in rows)


def test_tools_on_omp_session(tmp):
    write_omp_session(tmp / "omp", "x/repo", "2026-11-11T01:00:00", "omp-sub-1", "/var/home/sasha/x/repo", omp_agent_session("omp-sub-1"), agent="Planner")
    r = run("tools", "omp-sub-1", env_sessions=tmp)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert [(t["tool"], t["isError"]) for t in out] == [("edit", True)]


def test_stats_all_empty_corpora(tmp):
    # fresh machine / missing corpus dirs must not crash (canticos-25j)
    r = run("stats", "--all", env_sessions=tmp)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out == {"sessions": 0, "tool_calls": 0, "errors": 0, "by_class": {}, "by_source": {}}


def test_corrupt_cache_is_rescanned(tmp):
    # truncated cache writes must rebuild, not crash downstream (canticos-0l8)
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    cache = tmp / "cache"
    cache.mkdir()
    env = dict(os.environ, TERAPIA_SESSIONS=str(tmp), TERAPIA_OMP_SESSIONS=str(tmp / "omp"), TERAPIA_CACHE=str(cache))
    args = ["nu", "--no-config-file", str(TERAPIA), "stats", "--all"]
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    assert json.loads(r.stdout)["errors"] == 2
    for f in cache.glob("*.json"):
        f.write_text("{corrupt")
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["errors"] == 2, "corrupt cache entry must be rebuilt"


def test_list_uses_cache(tmp):
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    cache = tmp / "cache"
    env = dict(os.environ, TERAPIA_SESSIONS=str(tmp), TERAPIA_OMP_SESSIONS=str(tmp / "omp"), TERAPIA_CACHE=str(cache))
    r = subprocess.run(["nu", "--no-config-file", str(TERAPIA), "list"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)[0]["id"] == "sid-aaa"
    assert list(cache.glob("*.json")), "list should go through the meta cache (canticos-cjt)"


def test_run_isolates_cache(tmp):
    # fixture scans must not land in the real ~/.cache/terapia (canticos-mla)
    write_session(tmp, "a/proj", "2026-10-09T10:00:00", "sid-aaa", "/a/proj", sample_session("sid-aaa"))
    r = run("stats", "--all", env_sessions=tmp)
    assert r.returncode == 0, r.stderr
    assert list((tmp / "cache").glob("*.json")), "run() should point TERAPIA_CACHE inside the fixture dir"


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
