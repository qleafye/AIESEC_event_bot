---
name: gsd-grok
description: "Cursor-сессия с родителем Grok: куда делегировать субагентам вместо haiku/sonnet/opus из глобальных GSD-скиллов"
---

# GSD + Grok (Cursor)

Глобальные скиллы GSD (`~/.claude/skills/gsd-*`) написаны под Claude Code: там встречаются
`haiku`, `sonnet`, `opus`, иногда `fable`. **В Cursor их в параметр `model` субагента не
подставлять** — slug не резолвится. **Composer не замена Sonnet** — на «обычную» и «сложную»
работу субагента с composer не ставить.

Проверено (11.2026): у субагента `model` резолвится **`composer-2.5-fast`** (только для механики).
Slug `grok-*`, `cursor-grok-*` лаунчер **отвергает**; без `model` субагент **наследует Grok**
(дорого) — для worker/verifier на механике всегда передавать `composer-2.5-fast`.

Запрещены в `model`: grok-slug, `fable`, `claude-fable`, `claude-opus`, `claude-sonnet`, `gemini`,
`gpt`, `muse` и прочие slug Claude Code, которых нет в Cursor.

## Подстановка ролей

| Роль | Кто в Cursor | Субагент | `model` |
|---|---|---|---|
| План фазы, проверка плана | **родитель Grok** | нет | — |
| Обычная задача, сложная задача | **родитель Grok** | нет | — |
| Финальная проверка ветки | **родитель Grok** | нет | — |
| Механика (переименование, ключ по образцу, перевод строк, тест по образцу) | `worker` | да | `composer-2.5-fast` |
| Проверка механики | `verifier` | да | `composer-2.5-fast` |

При провале проверки механики (или `НЕ СМОГ` / системный FAIL) — **родитель Grok**, не grok-slug
в субагенте.

## GSD-скиллы

- `/gsd-plan-phase`, plan-check, `/gsd-verify-work` — **родитель Grok**.
- `/gsd-execute-phase` в связке с orchestrate не вызывать (одна модель профиля).
- Задачи PLAN.md: механика — orchestrate + composer; остальное — родитель.

Глобальные файлы в `~/.claude/skills/` **не переписывать**. Источник правды в Cursor: этот скилл,
`.claude/skills/orchestrate/SKILL.md`, `.claude/orchestrate-project.md`.
