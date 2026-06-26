# Phase 4 — Resume Disk Storage / File Browser Integration (design seed)

**Captured:** 2026-06-26
**Status:** DESIGN SEED — open sub-choices flagged. Not full phase context.

## Problem
Admins want to browse the full base of uploaded resumes via an existing **File Browser** instance on Ubuntu (already deployed, lives OUTSIDE the project root).

## Key constraint
Phase 1 stores resumes as **`file_id` only** (decision D-10 — no download, no disk write; files live on Telegram servers). **There are no resume files on disk to duplicate.** To expose resumes in File Browser, the bot MUST download them from Telegram into the directory File Browser serves.

## Decisions seeded (confirm at Phase 4)

- **R-01 — Bot writes directly into File Browser's served directory.** Do NOT move/host File Browser inside the project. Keep FB where it is. Bot calls `bot.download(file_id)` and writes the file into `RESUME_DIR` (= FB's data dir), path via env/setting. Single write, no duplication. Bot and FB stay decoupled.
- **R-02 — Optional disk layer behind a toggle.** This reverses the file_id-only stance for resumes specifically; gate it with `resume_disk_save` (on|off) so events that don't need it keep the zero-disk behavior.
- **R-03 — Filename:** sanitized `{telegram_id}_{full_name}.ext` (strip path-unsafe chars).
- **R-04 — SECURITY (PII):** File Browser will serve personal data (resumes). MUST have strong auth and not be exposed in a public web path without login. Treat as personal-data handling.

## Open sub-choices (decide at plan time)
- **When to download:** on upload (simple, but also pulls incomplete/rejected applicants' files) vs on approval (only confirmed delegates). Leaning approval-time once Phase 2 approval flow exists.
- **Cleanup/retention:** resumes accumulate in `RESUME_DIR`; manual retention via FB is probably fine for now — confirm.
- Alternative considered: on-demand `/export_resumes` batch download command (materialize only when admin asks) — lower constant disk churn, but not a live browsable base.

## Context
RusCo launches now on the free flow; this is a later enhancement. Fits Phase 4 (universal modules — optional storage module with a toggle).

Relates to Phase 1 D-08..D-11 (resume upload, file_id) and ROADMAP Phase 4 module toggles.
