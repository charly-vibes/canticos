# Changelog

## 2026-11-13

### Added
- `interview-live`: **question lifecycle events** (canticos-vvc) — `question_shown` fires the first frame a pending question enters the rendered view (TUI queue pane or a plain stdout round), exactly once per question lifetime; `question_asked` on the existing space/`mark_asked` path; `question_dismissed` via the new `Queue.mark_dismissed` (API only until a TUI key binds it). Payloads carry `key`/`text`/`priority`/`score`. The queue now tracks `status` + a `shown` flag and exposes `pending_questions()`, so asked/dismissed questions never re-enter pending views or re-emit as shown. `TuiState` takes an injectable `emit` sink (default no-op) so layout tests need no EventLog; the coordinator wires `self.log.emit`. Tests: 5 new (4 unit, 1 fake-pi e2e).
- `interview-live`: **paraphrase dedupe** (canticos-4pi) — `Queue._ingest` merges pending questions whose token sets score `JACCARD_MERGE = 0.7` after a light Spanish stemmer (accent fold, stopwords, suffix strip); merged items keep the latest wording, bump `mentions`, keep `added_at` and the best priority. Exact-key dedupe is unchanged.
- `interview-live`: **auditable suggestion batches** (canticos-a6q) — every `suggest_result` event now carries `input_lines` (the exact joined transcript string sent to pi) and `raw_output` (pre-parse stdout), so prompt-vs-context failures are measurable from `events.jsonl` alone.
- `interview-live`: **freshness decay + auto-expiry** (canticos-q9j) — pure `decay_weight` / `expires_at` (P-13 60s half-life, P-14 0.15 threshold) applied at render time; stale pending questions flip to `expired`, leave every pending view, and emit `question_dismissed` with `reason='expired'` exactly once. Fresh evidence resets the clock; `queue.json` gains `decay_weight`/`expires_at`.
- `interview-live`: **two-tier questioner** (canticos-7h9) — optional `--question-model` (or `$INTERVIEW_QUESTION_MODEL`) runs the strong model with a 45s budget and falls back once to `--pi-model`, emitting `suggest_fallback` and showing `done (fallback)`; unset, the pi argv is byte-identical.
- `interview-live`: **replay toolkit** (canticos-e7a, 93w-a/b/c) — `bin/interview_live_metrics.py` prints Q:T ratios, no-question/topic-only batches, questions/min, median cadence, acted-on and repair rates, time-to-first-question (`--json`); `bin/interview_live_replay.py` folds `events.jsonl` deterministically (`--json`), runs parameter sweeps (`--sweep`), and emits an optional turu-format knowledge ledger (`--knowledge`). Baseline fixture: `tests/fixtures/interview-20261113-slice.jsonl`.

### Changed
- `interview-live`: **conversation-driven suggestion cadence** (canticos-2iv) — new pure `bin/interview_live_trigger.py` (`TriggerPolicy`, injected clock) replaces the per-round enqueue: batches now fire on guest turn end (1.2s quiet floor P-01, verdict in audio time or wall clock), on a guest monologue cap (25s P-02), on the TUI `f` force key, or on the end-of-run flush — so cadence follows the interview instead of the `--every` timer. The suggester thread drops stale queued batches when a newer one is pending but carries their lines into the newest batch (no segment loss), logging `suggest_trigger` / `suggest_stale_drop` events. Tests: 7 new (`-k trigger`), including a stale-drop integration run proving 8 triggers coalesce into 2 pi calls with every transcript line preserved.

### Docs
- `interview-live` session UX documented end-to-end (canticos-4zn): `--out` as the sessions ROOT with `<out>/<session>/` layout, `--session`, `--list`/`--list --json` (newest-first), `--adopt` for legacy flat roots, `--speaker`/`--set-speaker`, `--replay` gold harness, `--diarize` (pyannote in an ephemeral `uv` env, `HF_TOKEN`), plus defaults (`--language es`, whisper `large-v3-turbo` auto-preference, `--every 5`) and the conversation-driven trigger (guest turn end, monologue cap, TUI `f`, end-of-run flush; stale batches coalesce without losing lines).
- Corrected stale architecture claims from the proposal reviews (canticos-5rs, CORR-001/002/004): pi runs in a background thread (async since b233541) so suggestions never stall transcription; `--every` defaults to 5.0, not 10; suggestions cover questions and topics, not “+40 topic labels”; replay exists (`--from`/`--replay`), not “none”; the suite is 137 passed with 12 pre-existing `test_terapia` fixture errors. Reference metrics now cite the measured baseline `~/Interviews/interview-20261113-084517` instead of external proposal figures.

## 2026-11-12

### Added
- `interview-live`: **event log** — chronological `events.jsonl` audit trail (session_start, capture_start, transcript_segment, whisper_error, suggest_result/error, participants_set, session_end) appended thread-safely in both plain and TUI modes; enables replay, debugging and post-hoc analysis. Unit + e2e tests (typed JSONL lines, append-not-truncate, replay timeline).
- `interview-live`: **modularized architecture** (Ro5 review, TypeSafe-verified) — the 398-line monolith split into `interview_live_capture.py`, `interview_live_whisper.py`, `interview_live_suggest.py`, `interview_live_events.py` around existing `core.py`/`tui.py`; `interview-live` is now a thin coordinator. Fixes: participant count now reaches the pi system prompt via the TUI answer (effective_participants), whisper TimeoutExpired no longer crashes the loop (skips round + logs), TUI capture status updates live/replay/stopped; dead `--live` flag and `self.font` removed; `fmt_ms` single-sourced in core. Tests: 6 new interview-live cases; the repo suite currently runs 137 passed with 12 pre-existing `test_terapia` fixture errors.

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
