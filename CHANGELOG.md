# Changelog

## 2026-11-12

### Added
- `interview-live`: **event log** — chronological `events.jsonl` audit trail (session_start, capture_start, transcript_segment, whisper_error, suggest_result/error, participants_set, session_end) appended thread-safely in both plain and TUI modes; enables replay, debugging and post-hoc analysis. Unit + e2e tests (typed JSONL lines, append-not-truncate, replay timeline).
- `interview-live`: **modularized architecture** (Ro5 review, TypeSafe-verified) — the 398-line monolith split into `interview_live_capture.py`, `interview_live_whisper.py`, `interview_live_suggest.py`, `interview_live_events.py` around existing `core.py`/`tui.py`; `interview-live` is now a thin coordinator. Fixes: participant count now reaches the pi system prompt via the TUI answer (effective_participants), whisper TimeoutExpired no longer crashes the loop (skips round + logs), TUI capture status updates live/replay/stopped; dead `--live` flag and `self.font` removed; `fmt_ms` single-sourced in core. Tests: 36 interview-live (6 new).

### Added
- `interview-live`: **reactive TUI dashboard** (auto-enabled on a TTY; `--no-tui`/`--tui` to override) — prompts for participant count at start, live transcript pane, and a prioritized queue pane (pi questions + important topics deduped with mention counts, so recurring signals float up as the interview progresses); observe-only with optional keys (`q` quit, `space` mark top question asked, `PgUp`/`PgDn` scroll). Worker: pi system prompt now asks for JSON `{questions, topics[high|med|low]}` with a tolerant legacy plain-line fallback; new `queue.json` artifact; `--participants N` feeds the roster and the pi system prompt.
- Tests: `tests/test_interview_live_tui.py` (12: layout panes, participants, JSON/plain parsing, recurrence ranking via new pure module `bin/interview_live_core.py`), plus e2e tests in `tests/test_interview_live.py` (participants→pi prompt, queue.json dedupe/recurrence, plain fallback, validation).

### Added
- `interview-live`: live Mom Test interview copilot — rolling whisper-cli diarized transcription (mic/speaker 0 vs system/speaker 1) fed to pi (`--session-id` continuity, Mom Test methodology system prompt) for 2–4 grounded follow-up questions per batch; `--from <recording>` replay mode handles both meeting-rec two-stream and plain stereo inputs; default model `qwen-token-plan-individual/qwen3.8-flash` (override `INTERVIEW_PI_MODEL` / `--pi-model`).
- Tests: `tests/test_interview_live.py` (9) pin capture argv, whisper `-ot` offset progression, transcript dedup, pi session continuity and failure handling.

## 2026-11-12

### Added
- `meeting-rec`: records meeting audio (default mic + system monitor) into one Opus `.mka` with separate tracks; auto-detects PipeWire devices via pactl; `MEETING_REC_DIR` / `-o` for output location.
- Graceful Ctrl+C path: job control isolates ffmpeg's process group and a single forwarded SIGINT preserves the matroska trailer (double-INT and tty-write paths observed to corrupt the file).
- Tests: `tests/test_meeting_rec.py` pins the ffmpeg argv contract, output-path defaults, `-m`/`-s` modes, interrupt handling and failure modes with fake ffmpeg/pactl.

## 2026-06-02

### Added
- `llm-dashboard` as the new primary entrypoint for the local AI dashboard.
- Multi-source analytics for Claude Code and Pi session logs.
- Activity-only analytics for Amp file-change history.
- Activity-only analytics for Gemini tmp/history artifacts.

### Changed
- `claude-dashboard` now acts as a backward-compatible alias to `llm-dashboard`.
- Installer and docs now reference `llm-dashboard` as the primary command.
- Dashboard UI now reports source breakdowns, usage-capable sessions, and activity/file-change counts.
