#!/usr/bin/env python3
"""TDD tests for bin/fi — free model wrapper. Red→green pipeline."""

import subprocess, sys, os, shlex

BIN = os.path.join(os.path.dirname(__file__), "..", "bin", "fi")

def run(args):
    return subprocess.run([BIN] + args, capture_output=True, text=True)

def test_alias_resolution():
    r = run(["doctor"])
    assert r.returncode == 0, "doctor fails"
    assert "laguna" in r.stdout

def test_default_model_fallback():
    r = run(["list"])
    assert r.returncode == 0
    assert "openrouter/free" in r.stdout

def test_list_subcommand():
    r = run(["ls"])
    assert r.returncode == 0
    assert "laguna" in r.stdout

def test_help():
    r = run(["--help"])
    assert r.returncode == 0
    assert "Free model wrapper" in r.stdout
