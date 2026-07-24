---
phase: 06-settings-schema-registry
verified: 2026-07-24T00:00:00Z
status: passed
score: 4/4 success criteria verified
overrides_applied: 0
human_verification:
  - test: "06-SMOKE-CHECKLIST.md §3 — live per-toggle-button before/after comparison + restart-persistence spot check"
    expected: "Every migrated group sub-screen and all 14 toggle buttons behave identically pre/post-migration on the live bot with real bot_settings data"
    why_human: "Requires a live bot instance with real ~590-user bot_settings data; explicitly DEFERRED to post-SumMeet by orchestrator/user decision (same pattern as Phase 5's deferred live UAT, accepted at close). Not a phase failure — automated regression net (397/397, 4-layer proof: parse-equivalence + render-snapshot + coverage + consumer-wiring) is the full extent of pre-forum automated proof available (project has no CI/linter per CLAUDE.md)."
---

# Phase 6: Settings-schema Registry — Verification Report

**Phase Goal:** Единый `SETTINGS_SCHEMA`-реестр — источник метаданных (type/group/label/default/parse) для ключей `bot_settings`; потребители и админ-UI читают через него, инкрементально, без ломки на ~590 живых юзерах.

**Verified:** 2026-07-24
**Status:** passed (with one deferred human-verification item, explicitly accepted by prior user decision — see below)
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths / Success Criteria (ROADMAP §Phase 6)

| # | Success Criterion | Status | Evidence |
|---|---|---|---|
| 1 | `SETTINGS_SCHEMA` registry exists; each migrated key `{type, group, label, default, parse}`; `REG_DEFAULTS` absorbed (not duplicated) | ✓ VERIFIED | `settings_schema.py:37-436` — 94 keys, each entry has `type`/`group`/`label`/`default` (`prompt` present for admin-editable fields, optional `parse` override per D-03). Type breakdown: toggle=43, text=19, enum=15, list=10, photo=4, file=1, int=1, date=1 across 7 groups (event/reg/pay/party/consent/reg_questions/toggles). `handlers/registration.py:207-209` — `REG_DEFAULTS = {k: v["default"] for k, v in SETTINGS_SCHEMA.items() if v["type"] == "toggle"}` — a dict comprehension over the registry, NOT a duplicated literal. `test_reg_defaults_parity` pins byte-for-byte parity against the frozen pre-migration 43-key oracle; full suite green. |
| 2 | Registry is source of truth for parse/defaults; consumers (`reminders.py`/`scheduler.py`/`admin.py`/`builders.py`) read migrated keys via registry; on/off/default semantics byte-for-byte | ✓ VERIFIED | Direct grep across all 6 consumer files (`handlers/admin.py`, `handlers/registration.py`, `handlers/payment.py`, `services/scheduler.py`, `services/reminders.py`, `keyboards/builders.py`) for `get_setting(<toggle-or-enum-key>)` NOT wrapped by `get_setting_typed` found **zero** live decision-branch bypasses. Every one of the 14 enum/toggle feature-switch keys plus all 43 `reg_q_*` toggles resolve exclusively through `get_setting_typed`/`_is_question_on` (which itself delegates to `get_setting_typed`, `admin.py:2115-2119`). `_parse_setting`'s enum branch (`settings_schema.py:464-469`) is `raw if raw else default` — proven byte-for-byte identical to the live `get_setting(k) or "<default>"` idiom for BOTH `None` and `""` by `test_parse_setting_enum_falsy_to_default`, `test_enum_feature_switch_defaults`, and 8 additional consumer-specific gate-equivalence tests in `tests/test_settings_consumers_phase6.py` (edu_conditional, party_enabled/fork, payment_enabled, payment_reminders, reg_bonus_enabled, is_module_enabled, registration_mode/reg_university_mode, full/short/party_approval, pending_notify_mode) — each driving the REAL consumer function across `[None, "", <option-A>, <option-B>]` and asserting behavior parity with the frozen `raw or default` oracle. One flagged completeness gap (registration_mode read in `render_settings_text` + 3 generic toggle helpers) was found, RED-confirmed, and closed test-first in 06-07 (commits `a4710e0`/`602a611`) — now zero remaining raw-idiom decision sites. **Minor residual (non-blocking):** `handlers/registration.py:2335` still reads `get_setting('registration_mode') or 'short'` raw, but only inside a `logger.info(...)` diagnostic string, not a decision branch — cosmetic-only, does not affect on/off/default behavior. |
| 3 | Migration incremental & non-breaking; old (`SETTINGS_GROUPS`/`SETTINGS_FIELDS`) + new (generated) render coexist; bot working at every step; no user record lost, no setting reset | ✓ VERIFIED | `handlers/admin.py:347-418` — `SETTINGS_FIELDS`/`SETTINGS_GROUPS` for the 5 migrated groups (event/reg/pay/party/consent) are now list-comprehensions computed from `SETTINGS_SCHEMA` (`_EVENT_FIELDS`, `_REG_FIELDS`, etc.), spliced together into the same names old consumers still import — no call-site rewrites needed (D-14). `git show --stat` across all 7 phase-6 commits (`87ee5a2`..`602a611`) confirms **zero changes to `database/db.py`** — no `ALTER TABLE`/`DROP TABLE`/schema change touching the `users`/`bot_settings` tables; the only destructive-looking DDL in the codebase (`ALTER TABLE ... ADD COLUMN` in `_ensure_column`, `CREATE TABLE IF NOT EXISTS`) is pre-existing infra untouched by this phase. `import handlers.admin, handlers.registration, handlers.payment, services.reminders, services.scheduler, keyboards.builders, settings_schema, main` succeeds cleanly (import smoke). Full automated suite: **397/397 passing** (verified live in this session, matches 06-07-SUMMARY's claimed count). |
| 4 | Admin-UI renders from registry (order/groups/label/render-by-type) for migrated groups; editing one key requires editing only the registry entry | ✓ VERIFIED | `render_settings_group_text`/`build_settings_group_keyboard` (`admin.py:603-664`) source field order, label text, and group membership entirely from `SETTINGS_FIELDS`/`_settings_group_keys()` (both registry-generated) for the 5 migrated groups — proven byte-for-byte unchanged by 6 render-snapshot tests (`test_event_render_snapshot`, `test_render_snapshot_reg/pay/party/consent`). For these 5 groups, adding/relabeling a field requires editing only its `SETTINGS_SCHEMA` entry (order arrays `_EVENT_FIELD_ORDER` etc. reference keys, not literal label/prompt text). |

**Score:** 4/4 success criteria verified.

### Automated Regression Suite

```
python -m pytest -q
397 passed in 107.83s
```
Matches the count claimed in `06-07-SUMMARY.md`/`06-SMOKE-CHECKLIST.md` — independently re-run in this verification session, not taken on faith.

### Key Link Verification

| From | To | Via | Status | Details |
|---|---|---|---|---|
| `services/reminders.py` (pending_reminder_loop) | `settings_schema.get_setting_typed` | direct import + await call | ✓ WIRED | `test_reminders_loop_reads_interval_via_registry` monkeypatches the module-level name and asserts it was actually invoked with `pending_reminder_interval`. |
| `services/scheduler.py` (send_payment_reminder / sweep_payment_overdue) | `settings_schema.get_setting_typed` | direct import + await call | ✓ WIRED | `test_payment_reminders_gate_equiv` — real gate function driven through tmp DB; wiring re-confirmed via monkeypatch call-capture. |
| `keyboards/builders.py` (get_source_kb) | `settings_schema.get_setting_typed` | direct import + await call | ✓ WIRED | `test_source_kb_reads_via_registry`. |
| `handlers/admin.py` (render_settings_text, build_settings_keyboard, 3 generic toggle helpers, `_is_question_on`) | `settings_schema.get_setting_typed` | direct import + await call | ✓ WIRED | `test_generic_toggle_helpers_wired_to_registry` (source-inspection gate) + `test_toggle_current_value_equiv_across_generic_helpers` (behavior gate); confirmed via direct grep (zero raw bypasses on decision paths, see SC#2 evidence). |
| `handlers/registration.py` (`_get_enabled_steps`, `_should_show_fork`, `_progress`, `_is_module_enabled`, `send_completion_and_bonus`, `process_full_name`, `finalize_registration`, `_ask_step`) | `settings_schema.get_setting_typed` | direct import + await call | ✓ WIRED | 9 dedicated gate-equivalence + source-inspection tests in `tests/test_settings_consumers_phase6.py`. |
| `handlers/payment.py` (should_offer_receipt_upload) | `settings_schema.get_setting_typed` | direct import + await call | ✓ WIRED | `test_payment_enabled_gate_equiv`. |
| `handlers/admin.py` `SETTINGS_FIELDS`/`SETTINGS_GROUPS` (5 migrated groups) | `settings_schema.SETTINGS_SCHEMA` | module-level list comprehension | ✓ WIRED | Direct source read (`admin.py:347-418`); `test_settings_groups_cover_every_field_key` + 6 render-snapshot tests. |

### Requirements Coverage

| Requirement | Description | Status | Evidence |
|---|---|---|---|
| REG-01 | Единый реестр `SETTINGS_SCHEMA` | ✓ SATISFIED | See SC#1. |
| REG-02 | Потребители читают через реестр | ✓ SATISFIED | See SC#2. |
| REG-03 | Админ-UI рендерится из реестра | ✓ SATISFIED | See SC#4. |

No orphaned requirements found — REQUIREMENTS.md traceability table lists REG-01/02/03 as the only phase-6-scoped items, all three claimed `[x]` and independently confirmed above.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|---|---|---|---|---|
| `handlers/registration.py` | 2335 | Raw `get_setting('registration_mode') or 'short'` inside a `logger.info(...)` diagnostic string only | ℹ️ INFO | Cosmetic only — not a decision branch, does not affect on/off/default behavior. Log line could theoretically show a stale mode label if this were ever promoted to a real branch, but as-is it's inert. |
| `handlers/admin.py` | 451-460 (`PHOTO_FIELDS`/`FILE_FIELDS`) vs `settings_schema.py` | 93-112 | Label/prompt strings for the 5 photo/file keys (`program`/`speakers`/`start`/`venue`/`reg_bonus`) are literal-duplicated between the registry and `admin.py`'s hardcoded `PHOTO_FIELDS`/`FILE_FIELDS` tables (byte-identical strings in both places) | ℹ️ INFO (documented, deliberate) | Editing a photo/file field's label today still requires touching 2 files, partially reintroducing the "one file" coordination tax the phase's own north star (D-09) explicitly targeted for elimination. However this is NOT an oversight — `06-01-PLAN.md:177` explicitly instructs "leave `build_settings_group_keyboard`'s event branch unchanged" and D-10 explicitly scopes photo/file registry entries as metadata-only (upload-flow mechanics stay special-cased). Documented, in-scope deviation — not a violation of any stated Success Criterion (SC#1 only names `REG_DEFAULTS` as the "must not duplicate" target, which IS correctly absorbed). |
| `handlers/registration.py` | 211-255 (`REG_LABELS`) vs `settings_schema.py` | 328-370 (`reg_q_*` `label` fields) | `reg_q_*` toggle labels are duplicated between the registry and the pre-existing `REG_LABELS` dict (admin's questions-screen UI still reads `REG_LABELS`, not the registry's `label` field) | ℹ️ INFO (documented, deliberate) | `settings_schema.py:322-327`'s own comment explains this is a forced duplication due to an import-cycle constraint (registration.py imports settings_schema, so the reverse import would cycle — T-06-14) — copied byte-for-byte, not independently authored. Same caveat as above: documented, not an oversight, and does not affect the stated byte-for-byte behavioral guarantee (labels, not values). |

No `TBD`/`FIXME`/`XXX` markers found in any of the 7 phase-6 commits' touched files (`settings_schema.py`, `handlers/admin.py`, `handlers/registration.py`).

### Human Verification Required

1 item, previously identified and already explicitly accepted/deferred by the user/orchestrator (see frontmatter `human_verification`), following the same precedent as Phase 5's deferred live UAT (accepted at close per ROADMAP.md). Not treated as a phase-blocking gap per the explicit instruction accompanying this verification task.

### Gaps Summary

No blocking gaps found. All 4 ROADMAP success criteria are independently verified against the live codebase (not SUMMARY claims): the registry exists with correct 5-field metadata shape and 94 migrated keys; `REG_DEFAULTS` is a genuine derived re-export; all 6 consumer files route toggle/enum/int/date value-reads through `get_setting_typed` with zero remaining raw-idiom bypasses on any decision branch (one closed 4-site boundary from 06-05/06-06 was resolved test-first in 06-07, re-confirmed here); no destructive DB migration touched `database/db.py` in any of the 7 phase-6 commits; and the admin-UI's 5 migrated group sub-screens render order/label/group entirely from registry-derived generated views.

Two documented, deliberate (non-blocking) scope carve-outs remain — photo/file field metadata and `reg_q_*` toggle labels are still literal-duplicated for reasons explicitly recorded in the phase's own planning artifacts (D-10 upload-flow special-casing; T-06-14 import-cycle constraint) — these do not violate any stated Success Criterion but are noted for future-group awareness (e.g., if a future v2 group wants full "one file" purity for photo/file or reg_q labels, a small follow-up would be needed).

The only open item is the live-bot smoke checklist (06-SMOKE-CHECKLIST.md §3), explicitly and knowingly DEFERRED to post-SumMeet by prior user/orchestrator decision — this is expected per the task brief, not a verification failure.

---

*Verified: 2026-07-24*
*Verifier: Claude (gsd-verifier)*
