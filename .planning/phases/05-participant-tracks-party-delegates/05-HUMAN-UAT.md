---
status: partial
phase: 05-participant-tracks-party-delegates
source: [05-VERIFICATION.md, 05-06-SUMMARY.md, 05-REVIEW.md]
started: 2026-07-21
updated: 2026-07-21
---

## Current Test

[awaiting human testing — live Telegram bot + real Google Sheet]

## Setup (before you start)

1. Admin → Settings: «Трек вечеринки» (`party_enabled`) = ВКЛ.
2. Apply the 🎉 Party preset (Тип события → 🎉 Party).
3. «Модерация вечеринки» (`party_approval`) = ручная.
4. Have the real Google Sheet open in a browser tab.

## Tests

### 1. Party deep link — overnight, only party questions
steps: Open `t.me/<bot>?start=party_over` from a test account.
expected: Flow asks ONLY party questions (age, phone, VK, city, allergies, food) + overnight (проживание / общая кровать / сосед). NEVER university or resume.
result: [pending]

### 2. Track survives a bare /start mid-flow
steps: Mid-registration, send a bare `/start` with no parameter.
expected: You stay on the party track, flow continues, track not reset to full.
result: [pending]

### 3. Finalize under manual moderation
steps: Finish the party registration.
expected: «заявка отправлена» message, NO main menu shown (manual moderation).
result: [pending]

### 4. Moderation card shows the track
steps: Admin → «Заявки» queue, find the application.
expected: Card shows «🎉 Трек: вечеринка с ночёвкой». Approve it.
result: [pending]

### 5. Party approval text + party-only tariffs
steps: Observe the approval message and payment options after approve.
expected: Approval message is the party text (if `approve_text__party` set — note: settable only via DB right now, WR-02 backlog). Payment options shown are ONLY party-eligible tariffs.
result: [pending]

### 6. Party Sheet tab — exclusive routing
steps: Open the Google Sheet.
expected: A «Party» tab exists; its header has NO ВУЗ/Курс/Резюме columns; the new row is present with its «Трек» cell filled. Confirm NO corresponding row appeared on the MAIN tab.
result: [pending]

### 7. No-overnight sub-track skips housing
steps: Repeat test 1 with `t.me/<bot>?start=party_noover`.
expected: Housing / bed questions are SKIPPED; other party questions still asked.
result: [pending]

### 8. Full-flow regression (no deep link)
steps: Register once through the ordinary flow (no deep link).
expected: Behaves exactly as before Phase 5; lands on the MAIN sheet tab, not Party.
result: [pending]

### 9. Legacy deep-link regression
steps: Open `t.me/<bot>?start=12345` (referral) and separately `?start=src_vk` (source).
expected: Referral is still recorded; source is still recorded. Behaves as before Phase 5.
result: [pending]

### 10. CR-01 fix — referral survives the party fork
steps: Set `party_fork_question` = ВКЛ (DB). Open `t.me/<bot>?start=<your_referrer_id>` on a FRESH account → the fork screen appears → pick «Полная регистрация» → finish.
expected: The finished user row has `referrer_id` filled (referral NOT lost). Repeat picking a party option — referral still recorded.
notes: This is the CRITICAL bug Fable 5 caught and we fixed (commit 136ea0b). Turn `party_fork_question` back OFF after testing unless you want the fork live.
result: [pending]

## Summary

total: 10
passed: 0
issues: 0
pending: 10
skipped: 0
blocked: 0

## Gaps
