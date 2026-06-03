# Changelog

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
