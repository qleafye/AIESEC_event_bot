"""«Тихо неработающих правил не бывает» (D-14) — реакция на правку настроек анкеты: когда
менеджер выключает вопрос анкеты или убирает вариант ответа, правило автоотказа, которое на
них опиралось, встаёт на паузу САМО (`services.reject_rules.active_rules` уже пересчитывает и
записывает это на каждой загрузке — план 31-04), а держателям права «Настройки» уходит
сообщение, какое правило встало на паузу и почему.

Отдельный файл, не `services/reject_rules.py` — там стоит сторож «модуль не грузит бот-фреймворк»,
а рассылка держателям права идёт через `handlers.admin_caps.notify_by_capability`, которому нужны
исключения и метод отправки самого бот-фреймворка. Второй файл держит эту границу чистой — сам
пересчёт паузы (`active_rules`) по-прежнему живёт в сервисе без этой зависимости, здесь — только
реакция и рассылка.

Хук висит на ВОРОНКЕ ЗАПИСИ настроек (`settings_audit.set_setting_by_admin`/
`delete_setting_by_admin`, план 31-12 задача 2), а не на конкретных экранах: менеджер трогает
вопросы анкеты и списки вариантов ответа с НЕСКОЛЬКИХ экранов (общие настройки, per-city
переопределения, списки вариантов, пресет типа события) — воронка записи одна, второго места,
которое пришлось бы синхронно поддерживать при появлении нового экрана, не заводим. Тот же
довод завёл `settings_audit.py` инцидентом 06.09 (автор записи в логе).

Один экран пишет НАПРЯМУЮ в `database.db.set_setting`, минуя воронку — `reg_presets.
apply_reg_preset` (кнопка пресета события в боте и в Mini App, `settings_ops.
apply_event_type_preset` зовёт тот же bulk-writer). Пресет одним тапом переписывает ДЕСЯТКИ
`reg_q_*`-тумблеров разом — именно тот момент, когда может встать на паузу сразу несколько
правил. `on_settings_written_batch` — второй вход специально для этого пути: пересчёт паузы
происходит ОДИН раз, ПОСЛЕ того как пресет дописал все свои ключи (не на каждый тумблер
отдельно), поэтому держатели права получают ОДНО сообщение на весь пресет, а не по одному на
каждое поставленное на паузу правило.

Повторную рассылку гасит сам контракт `active_rules`/`paused_changed`: он истинен `"on"` РОВНО
в тот вызов, когда пауза правила только что появилась, — на следующей правке настроек правило
уже на паузе, и `paused_changed` для него будет `None`. Здесь НЕ заводится вторая копия этой
логики (эта проверка — единственная причина, по которой повторная запись той же настройки не
шлёт второго сообщения); если когда-нибудь понадобится рассылка «на всякий случай» по всем
паузам — это отдельное решение, не молчаливое расширение этого хука.
"""
from __future__ import annotations

import html
import json
import logging

from cities import split_per_city_key
from database.db import list_reject_rules
from reg_engine import MULTI_CONFIG, SELECT_CONFIG
from services import scheduler as _sched
from services.reject_rules import active_rules, rule_summary
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

CAP = "settings"

# Тот же закрытый набор трековых хвостов, что `services.i18n_sources._TRACK_SUFFIXES` — второй
# копией логики его снятия не считается (это два литерала, не алгоритм), но ПРИЁМ снятия хвоста
# (`cities.split_per_city_key` для городского, сравнение суффикса для трекового) — тот же самый:
# закрытое множество кодов городов, а не произвольная регулярка.
_TRACK_SUFFIXES = ("__party", "__short")

# Ключи списков вариантов вне SELECT_CONFIG/MULTI_CONFIG (reg_engine.options() резолвит их особой
# веткой) — закрытый список, тот же, что перечисляет план.
_EXTRA_OPTION_KEYS = frozenset({"education_status_options", "university_options", "source_options"})


def _option_list_keys() -> frozenset[str]:
    keys = {opt_key for opt_key, _default in SELECT_CONFIG.values()}
    keys.update(opt_key for opt_key, _default in MULTI_CONFIG.values())
    keys.update(_EXTRA_OPTION_KEYS)
    return frozenset(keys)


_OPTION_LIST_KEYS = _option_list_keys()  # SELECT_CONFIG/MULTI_CONFIG — статические литералы модуля


def _strip_reg_key_suffixes(key: str) -> str:
    """Снимает городской, потом трековый хвост — `reg_q_course__party__city__msk` ->
    `reg_q_course`. Тот же приём, что `services.i18n_sources._strip_dynamic_suffixes`."""
    split = split_per_city_key(key)
    base = split[0] if split else key
    for suffix in _TRACK_SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def affects_reject_rules(key: str) -> bool:
    """Чистый предикат: истинно, если запись этого ключа настроек могла сломать (или починить)
    условие правила автоотказа — тумблер вопроса анкеты (`reg_q_*`, с любым городским/трековым
    хвостом) либо список вариантов ответа, известный `SELECT_CONFIG`/`MULTI_CONFIG` или
    `_EXTRA_OPTION_KEYS`. Ложно СРАЗУ для всего остального — подавляющее большинство записей
    настроек (тексты, лимиты, капы, тумблеры не про анкету) к правилам отношения не имеют, и
    хук на воронке записи не имеет права стоить им лишнего запроса в базу."""
    base = _strip_reg_key_suffixes(key)
    if base.startswith("reg_q_"):
        return True
    return base in _OPTION_LIST_KEYS


def _rules_word(count: int) -> str:
    """Русское склонение «правило/правила/правил» по числу — только для количества > 1
    (единственное число собрано отдельной веткой текста, см. `_build_pause_message`)."""
    tail = count % 10
    tens = count % 100
    if tail == 1 and tens != 11:
        return "правило"
    if 2 <= tail <= 4 and not (12 <= tens <= 14):
        return "правила"
    return "правил"


async def _rule_display_name(rule: dict) -> str:
    """Имя правила, если менеджер его задал, иначе автоописание (`rule_summary`) — тот же
    приоритет, что на карточке правила (план 31-08). HTML-экранировано (T-31-12-04): имя и
    автоописание могут нести произвольный текст менеджера, сообщение уходит с
    `parse_mode="HTML"`."""
    name = (rule.get("name") or "").strip()
    if name:
        return html.escape(name)
    return html.escape(await rule_summary(rule))


async def _build_pause_message(rules: list[dict]) -> str:
    """Одно сообщение на все правила, поставленные на паузу этим действием — человеческими
    словами, без единого кодового имени/ключа настройки/шага анкеты (CLAUDE.md «бот для людей»):
    имя/автоописание правила из `_rule_display_name`, подпись вопроса — уже человеческая строка
    `rule["paused_reason"]` (чистый предикат паузы `reg_engine.py` отдаёт её через `label_for`,
    план 31-01/31-04 — второй копии этой логики здесь нет, см. докстринг модуля)."""
    lines = []
    for rule in rules:
        display = await _rule_display_name(rule)
        question = html.escape(rule.get("paused_reason") or "")
        lines.append(f"• «{display}» опиралось на вопрос «{question}», а он сейчас выключен (или из него убран вариант).")
    count = len(rules)
    if count == 1:
        header = "⚠️ Правило автоотказа поставлено на паузу:"
        footer = (
            "Правило не срабатывает, пока вы это не поправите. Включите вопрос обратно или "
            "поправьте правило: 📋 Заявки → 🚫 Правила автоотказа."
        )
    else:
        header = f"⚠️ {count} {_rules_word(count)} автоотказа поставлены на паузу:"
        footer = (
            "Правила не срабатывают, пока вы это не поправите. Включите вопросы обратно или "
            "поправьте правила: 📋 Заявки → 🚫 Правила автоотказа."
        )
    return "\n".join([header, *lines, "", footer])


async def _combos_from_enabled_rules() -> list[tuple[str | None, str | None]]:
    """Реально встречающиеся сочетания «город × трек» среди ВКЛЮЧЁННЫХ правил (`enabled=1`) —
    декартово произведение всего справочника городов/треков здесь не нужно: пауза считается
    только для правил, которые вообще могли бы сработать. Пустые `tracks` трактуются как
    `["full"]` — тот же дефолт (D-05), что использует сам `active_rules`."""
    rows = await list_reject_rules(enabled_only=True)
    combos: set[tuple[str | None, str | None]] = set()
    for row in rows:
        city = row.get("city")
        try:
            tracks = json.loads(row.get("tracks") or "[]") or []
        except (TypeError, ValueError):
            tracks = []
        for track in (tracks or ["full"]):
            combos.add((city, track))
    return sorted(combos, key=lambda combo: (combo[0] or "", combo[1] or ""))


async def _recompute_and_notify() -> None:
    """Общее тело обоих входов ниже: пересчитать паузу `active_rules`'ом по каждому реальному
    сочетанию «город × трек» (второй копии правила паузы здесь нет, пересчёт — целиком в
    `services.reject_rules.active_rules`), собрать правила с `paused_changed == "on"`, отправить
    держателям права «Настройки» ОДНО сообщение НА ГОРОД правила (`notify_by_capability(city=...)`
    сам сужает адресатов до привязанных к этому городу + непривязанных + суперадминов — держатель,
    привязанный к другому городу, сообщения не увидит); правило «все города» (`city is None`)
    уходит БЕЗ фильтра — всем держателям, включая городских (T-31-12-05)."""
    combos = await _combos_from_enabled_rules()
    if not combos:
        return

    newly_paused: dict[int, dict] = {}
    for city, track in combos:
        try:
            rules = await active_rules(event_city=city, participant_type=track)
        except Exception as exc:  # noqa: BLE001 — один сорвавшийся пересчёт не топит остальные
            logger.error(
                "services.reject_rules_notify: пересчёт паузы сорвался (city=%r, track=%r): %s",
                city, track, exc,
            )
            continue
        for rule in rules:
            if rule.get("paused_changed") == "on":
                newly_paused[rule["id"]] = rule

    if not newly_paused:
        return

    bot = _sched._bot
    if bot is None:
        logger.error(
            "services.reject_rules_notify: бот ещё не поднят — уведомление о паузе (%d правил) не отправлено",
            len(newly_paused),
        )
        return

    by_city: dict[str | None, list[dict]] = {}
    for rule in newly_paused.values():
        by_city.setdefault(rule.get("city"), []).append(rule)

    from handlers.admin_caps import notify_by_capability  # lazy: aiogram-зависимость, см. докстринг модуля

    for city, rules_in_city in by_city.items():
        text = await _build_pause_message(rules_in_city)
        sent = await notify_by_capability(bot, CAP, text, parse_mode="HTML", city=city)
        if not sent:
            # notify_by_capability уже падает на config.ADMIN_IDS, если держателей права нет
            # вовсе (owner-аудит 19.09) — sent == 0 означает, что даже фолбэк никого не нашёл
            # (пустой ADMIN_IDS) ИЛИ отправка каждому получателю сорвалась. Тихо неотправленного
            # уведомления быть не должно — лог фиксирует это явно.
            logger.error(
                "services.reject_rules_notify: уведомление о паузе не нашло ни одного получателя "
                "(city=%r, rule_ids=%s)",
                city, [rule["id"] for rule in rules_in_city],
            )


async def on_setting_written(key: str) -> None:
    """Точка входа воронки записи настроек (план 31-12 задача 2) — одна запись, один ключ.
    Полностью fail-soft (T-31-12-02): сохранение настройки менеджера важнее уведомления и не
    имеет права упасть из-за него ни на каком шаге."""
    try:
        if not affects_reject_rules(key):
            return
        if not await get_setting_typed("reject_rules_enabled"):
            return  # выключенный модуль автоотказа не шумит (D-15)
        await _recompute_and_notify()
    except Exception as exc:  # noqa: BLE001
        logger.error("services.reject_rules_notify.on_setting_written(%r) failed: %s", key, exc)


async def on_settings_written_batch(keys: list[str]) -> None:
    """Второй вход — для писателей вне воронки, которые пишут МНОГО ключей одним действием
    (сегодня единственный такой — `reg_presets.apply_reg_preset`, тап пресета типа события).
    Зовётся ОДИН раз ПОСЛЕ всех записей пресета, не на каждый ключ по отдельности — иначе
    десяток вопросов, выключенных одним тапом, дал бы десяток отдельных пересчётов и рисковал
    прислать держателям права сообщение за сообщением вместо одного (T-31-12-03). Тоже
    полностью fail-soft — см. `on_setting_written`."""
    try:
        if not any(affects_reject_rules(key) for key in keys):
            return
        if not await get_setting_typed("reject_rules_enabled"):
            return
        await _recompute_and_notify()
    except Exception as exc:  # noqa: BLE001
        logger.error("services.reject_rules_notify.on_settings_written_batch(...) failed: %s", exc)
