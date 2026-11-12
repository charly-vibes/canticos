# Changelog

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
