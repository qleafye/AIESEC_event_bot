---
quick_id: 260703-0mj
slug: make-source-tag-authoritative-skip-quest
date: 2026-07-03
status: complete
---

# Quick 260703-0mj — Summary

Make the `/create_link` source tag authoritative (fixes UTM being overwritten).

## Problem
A user arriving via `?start=src_<tag>` had `source` pre-set to the tag, but the «Откуда узнал?»
question (`reg_q_source`, ON by default) was still asked and its answer **overwrote** the tag —
so campaign links didn't reliably show up as the source.

## Fix (`handlers/registration.py`)
- `_start_registration_flow`: when a `src_` deep-link tag is present, set `_source_from_tag=True`
  in FSM state alongside `source`.
- `_get_enabled_steps`: skip the `source` step when `_source_from_tag` is set. Organic users
  (no tag) still get asked as before.

Result: tagged users keep their campaign source end-to-end (sheet «Источник» + `/admin → Источники`);
the question stays for organic traffic.

## Verification
- New test `test_source_tag_skips_source_question`: source asked for organic, skipped when tagged.
- `python -m pytest tests/ -q -p no:asyncio` → **110 passed**
