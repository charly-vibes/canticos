- 2026-10-09T19:12:48Z [id:77f003b438fa54119abedf486f26cf60fd2dadd59e91e68ef5103a3ff315bcf0] ### 2026-10-09 — terapia shipped (pain rec #2: kill-the-heredoc query CLI)
  - canticos/bin/terapia: NUSHELL query CLI over ~/.pi/agent/sessions (user chose nu over python mid-session; nu 0.116). Commands: list [--project --since --until], tools <id> [--tool --errors-only], errors <id>|--all [--group-by class|tool|project|session], stats <id>|--all, raw <id> [--type]. All output JSON typed via to json.
  - TDD: tests/test_terapia.py synthetic fixture (nu invoked with --no-config-file, TERAPIA_SESSIONS env override), red→green 10/10. Prefix session-id resolution reads ONLY first JSONL line via ^head (cheap); --all commands single-pass scan-file {meta,pairs} — first draft was O(n²) via resolve-session full scans, timed out 600s on real corpus, restructured.
  - Real corpus verified: 1,679 sessions / 189k tool calls / 11,283 errors (exit_nonzero 5,905, edit_mismatch 1,355 ≈ pain report shape) in 85s single pass; list full corpus 38s.
  - NU GOTCHAS banked: (1) else-if chains CANNOT break across lines in nu scripts — parse error 'Command else not found'; (2) $nu.home-path renamed → $nu.home-dir (0.116); (3) param names cannot contain hyphens (session-id invalid); (4) ls with glob in interpolated string = literal (DoNotExpand) → use glob cmd; (5) reject -i deprecated → -o; (6) nu|nu pipe = broken pipe race — verify via file redirect; (7) nu --ide-check always exits 0 — gate must grep '"severity":"Error"' from diagnostics JSON (added nu block to canticos lefthook pre-commit+pre-push); (8) default block-arg evaluates eagerly.
  - README table + terapia usage section + install symlink added; commits 8d47ede + aaf7353; tree clean (untracked .claude/ + mini-zoomit.zip pre-existing).
  - Next: coffee remains usage/costs-focused — no overlap; possible follow-ups: terapia --group-by tool, cross-agent (claude/codex) adapters, or wire terapia into pain/archive-v1 miner comparison.
- 2026-10-09T19:17:07Z [id:b7c99005345d0813f1f706a7c74b034f70338d0f8a2cd76b1671b2ffbf152ab1] ### 2026-10-09 ~18:50 — terapia: incremental cache shipped (user request)
  - Per-file mtime+size-keyed cache: cache-key = md5(path|mtime %s.%f|size) → ~/.cache/terapia/<key>.json (env TERAPIA_CACHE). cached-scan stores {meta, classified error rows}; errors --all + stats --all read from cache, cache-miss re-scans. tools/raw/single-session paths uncached (single file, fast).
  - Real corpus: cold 36s → cached 0.95s. Verified correctness: live session grew +5 tool calls between runs; only that file invalidated/re-scanned, delta picked up — incremental invalidation confirmed on real data.
  - TDD: 2 tests added (cache created+reused identical; append new error → stale cache invalidated, errors 2→3, exit_nonzero=1). Red first: cache-creation test failed pre-impl; invalidation test passed vacuously without cache (noted). 12/12 green.
  - NU: format date '%s.%f' gives nanosecond-ish mtime; ls|first gives size+modified; hash md5 builtin.
  - Symlink was already live (aaf7353). Global AGENTS.md updated: Personal CLI tools section + ~/.config/agents/canticos.md (full reference: commands, cache, tests, nu gotchas). Repo-local .whisper/ committed per fleet convention.
  - Commits: 5f62bc6 (cache), whisper bank commit. charly hub local-only.
## Session snapshot - 2026-10-27

### What was worked on
- Investigated pi parameters controlling context, permissions, and constraints in sessions
- Created and installed `fi` wrapper script in canticos/bin/fi with 9 free model aliases
- Added `--timeout` parameter (default 900s / 15 min) using `timeout` command
- Auto-named sessions via `--name fi-YYYYMMDD-HHMMSS` if not provided
- Updated `.pi/agent/settings.json`: defaultModel → openrouter/free, httpIdleTimeoutMs → 900000 (15 min)

### Key decisions
- Only 9 of 11 tested free model aliases work; nvidia/nemotron-3-super-120b-a12b and nvidia/nemotron-3.5-content-safety:free fail routing
- `openrouter/free` is the validated default; `poolside/laguna-s-2.1:free` most reliable for coding tasks
- All pi CLI parameters forwarded; wrapper validates only `:free` suffixed models

### Next logical continuation
- Test remaining pi features with free models (--thinking levels, tool usage patterns)
- Expand `fi` with model validation/guardrails
- Document all aliases in README; add --model forwarding tests
- Ro5 Universal Review (2026-10-27): bin/fi review completed. VERDICT: NEEDS_REVISION (tests + docs missing). Fixed: CORR-001 guard, TESTS (tests/test_fi.py 4 PASS), CLAR-002 (README.md).
