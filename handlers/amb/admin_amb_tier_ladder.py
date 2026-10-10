"""Экран «🪜 Лестница ступеней» (раздел «🎓 Ступени амбассадоров», право moderate_game).

Менеджер любого события собирает лестницу кнопками, без разработчика:
- строка на каждую ступень: порог, награды на ступени (выдано / ждут), начало текста;
- «➕ Добавить ступень» (до 5) и «➖ Убрать последнюю» — с подтверждением, где сказано, сколько
  человек уже получили её и что у них она останется;
- «Квота» ступени включается кнопкой; число наград, порог и тексты правит общий редактор
  настроек (`settings_edit:<ключ>`, право «⚙️ Настройки»): кнопки видит только его держатель;
- галочка «Ступени только амбассадору с одобренной заявкой»;
- «🚫 Снять ступень» — ручное решение менеджера (накрутка): ступень пропадает, место в квоте
  освобождается, человеку ничего не шлётся; автоматика её (и старшие) больше не выдаёт;
- «↩️ Вернуть ступень» — список снятых вручную, кнопка снимает запрет и сразу пересчитывает;
- «🎁 Отдать место» — освободившееся место первому из листа ожидания, кнопкой и с
  подтверждением; автопродвижения из листа нет.

Подпись квоты здесь — «наград на ступени»: это не «мест в команде амбассадоров» из раздела
«🤝 Амбассадоры» (там лимит самой команды).

Правила выдачи и снятия — в `services/amb_tiers.py`; модуль только показывает и зовёт их.
Шов: своего `Router()` нет, декорирует общий `handlers.admin.router`; подключается хвостовым
импортом `handlers/amb/admin_amb_journal.py`.
"""
from __future__ import annotations

import html
import logging
import re

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from shared.amb_tier_keys import MAX_TIERS, tier_key
from database import amb_tiers_db
from database import db
from handlers.admin import router
from handlers.amb.admin_amb_tiers import _edit_or_send, _is_cancel, _person_label, _resolve_person_input
from handlers.access.admin_caps import has_capability
from handlers.states import AmbTierRevoke
from services import amb_tiers
from services.settings.audit import set_setting_by_admin
from domain.settings.schema import get_setting_typed

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")
_PREVIEW = 40
_STALE = "Кнопка устарела — откройте экран заново"
_NO_SETTINGS_RIGHT = (
    "Пороги, числа наград и тексты правит менеджер с правом «⚙️ Настройки» — у вас его нет."
)


def _preview(text: str) -> str:
    plain = " ".join(_TAG_RE.sub("", text or "").split())
    if not plain:
        return "не задан"
    return plain if len(plain) <= _PREVIEW else plain[:_PREVIEW].rstrip() + "…"


def _short_date(stamp: str | None) -> str:
    """`2026-10-12 10:00:00` -> `12.10`."""
    parts = (stamp or "")[:10].split("-")
    return f"{parts[2]}.{parts[1]}" if len(parts) == 3 else "—"


def _people(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return f"{n} человек"
    return f"{n} человека" if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else f"{n} человек"


def _back_kb(data: str = "ambl:main", text: str = "← К лестнице ступеней") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)]])


def _parse_tier(data: str | None) -> int | None:
    try:
        n = int((data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        return None
    return n if 1 <= n <= MAX_TIERS else None


async def _order_problem(cfg) -> tuple[int, int] | None:
    """`(ступень, порог предыдущей)`, если порог ступени не больше порога предыдущей."""
    for prev, cur in zip(cfg, cfg[1:]):
        if cur.threshold <= prev.threshold:
            return cur.n, prev.threshold
    return None


# ── главный экран ────────────────────────────────────────────────────────────────────────

async def _ladder_screen(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    cfg = await amb_tiers.tiers_config()
    summary = await amb_tiers_db.tiers_summary()
    require = await amb_tiers.require_approved_on()
    can_edit = await has_capability(user_id, "settings")

    lines = [
        "<b>🪜 Лестница ступеней</b>",
        "Чем больше прошедших отбор приглашённых, тем выше ступень. «Наград на ступени» — "
        "сколько амбассадоров получат награду; это не «мест в команде» из раздела «🤝 Амбассадоры».",
        "",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for c in cfg:
        stat = summary.get(c.n, {"reached": 0, "granted": 0, "waitlist": 0})
        if c.quota is None:
            quota = f"наград без ограничения (получили {stat['reached']})"
        else:
            quota = f"наград на ступени {c.quota} (выдано {stat['granted']}, ждут {stat['waitlist']})"
        text = await get_setting_typed(c.text_key)
        lines.append(f"🎓 {c.n}: за {c.threshold} прошедших · {quota} · текст: «{html.escape(_preview(str(text)))}»")

        quota_label = "✅ вкл" if c.quota is not None else "❌ выкл"
        if can_edit:
            rows.append([
                InlineKeyboardButton(text=f"✏️ Порог {c.n}", callback_data=f"settings_edit:{tier_key(c.n, 'threshold')}"),
                InlineKeyboardButton(text=f"📝 Текст {c.n}", callback_data=f"settings_edit:{c.text_key}"),
            ])
        quota_row = [InlineKeyboardButton(
            text=f"🎁 Ограничить награды {c.n}: {quota_label}", callback_data=f"ambl_quota:{c.n}")]
        rows.append(quota_row)
        if c.quota is not None and can_edit:
            rows.append([
                InlineKeyboardButton(text=f"✏️ Наград на ступени {c.n}", callback_data=f"settings_edit:{tier_key(c.n, 'quota')}"),
                InlineKeyboardButton(text=f"📝 Лист ожидания {c.n}", callback_data=f"settings_edit:{c.waitlist_key}"),
            ])
        if c.quota is not None and stat["waitlist"] > 0 and stat["granted"] < c.quota:
            rows.append([InlineKeyboardButton(
                text=f"🎁 Отдать место · ступень {c.n}", callback_data=f"ambl_prom:{c.n}")])

    problem = await _order_problem(cfg)
    if problem:
        lines += ["", f"⚠️ Порог ступени {problem[0]} должен быть больше, чем у ступени {problem[0] - 1} ({problem[1]})."]
    lines += [
        "",
        f"Ступени только амбассадору с одобренной заявкой: <b>{'✅ да' if require else '❌ нет'}</b>",
    ]
    if not can_edit:
        lines += ["", _NO_SETTINGS_RIGHT]

    rows.append([InlineKeyboardButton(
        text=f"Только с одобренной заявкой: {'✅ да' if require else '❌ нет'}", callback_data="ambl_req")])
    add_del: list[InlineKeyboardButton] = []
    if len(cfg) < MAX_TIERS:
        add_del.append(InlineKeyboardButton(text="➕ Добавить ступень", callback_data="ambl_add"))
    if len(cfg) > 1:
        add_del.append(InlineKeyboardButton(text="➖ Убрать последнюю", callback_data="ambl_del"))
    if add_del:
        rows.append(add_del)
    rows.append([InlineKeyboardButton(text="🚫 Снять ступень у амбассадора", callback_data="ambl_rev")])
    revoked = await amb_tiers_db.list_revocations(await amb_tiers.current_season())
    if revoked:
        rows.append([InlineKeyboardButton(
            text=f"↩️ Вернуть ступень ({len(revoked)})", callback_data="ambl_unrev")])
    rows.append([InlineKeyboardButton(text="← Ступени амбассадоров", callback_data="admin_amb_tiers")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(message: types.Message, user_id: int) -> None:
    text, kb = await _ladder_screen(user_id)
    await _edit_or_send(message, text, kb)


@router.callback_query(F.data == "ambl:main")
async def show_ladder(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await _show(callback.message, callback.from_user.id)
    await callback.answer()


# ── число ступеней ───────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "ambl_add")
async def ladder_add(callback: types.CallbackQuery):
    count = await amb_tiers.tiers_count()
    if count >= MAX_TIERS:
        await callback.answer(f"Больше {MAX_TIERS} ступеней не бывает.", show_alert=True)
        return
    await set_setting_by_admin(callback.from_user.id, "amb_tiers_count", str(count + 1))
    new = count + 1
    prev_threshold = int(await get_setting_typed(tier_key(count, "threshold")))
    threshold = int(await get_setting_typed(tier_key(new, "threshold")))
    if threshold <= prev_threshold:
        await callback.answer(
            f"Ступень {new} добавлена. Задайте порог ступени {new} больше, чем у ступени "
            f"{count} ({prev_threshold}) — кнопка «✏️ Порог {new}».", show_alert=True)
    else:
        await callback.answer(f"Ступень {new} добавлена: порог {threshold}. Проверьте текст и порог.")
    await _show(callback.message, callback.from_user.id)


@router.callback_query(F.data == "ambl_del")
async def ladder_del(callback: types.CallbackQuery):
    count = await amb_tiers.tiers_count()
    if count <= 1:
        await callback.answer("Последнюю ступень убрать нельзя — нужна хотя бы одна.", show_alert=True)
        return
    got = (await amb_tiers_db.tiers_summary()).get(count, {}).get("reached", 0)
    if got:
        who = (f"Её уже получили {_people(got)} — у них она останется, новые выдаваться не будут.")
    else:
        who = "Её пока никто не получил."
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Убрать", callback_data="ambl_del_go"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="ambl:main"),
    ]])
    await _edit_or_send(callback.message, f"<b>Убрать ступень {count}?</b>\n{who}", kb)
    await callback.answer()


@router.callback_query(F.data == "ambl_del_go")
async def ladder_del_go(callback: types.CallbackQuery):
    count = await amb_tiers.tiers_count()
    if count <= 1:
        await callback.answer("Последнюю ступень убрать нельзя.", show_alert=True)
        return
    await set_setting_by_admin(callback.from_user.id, "amb_tiers_count", str(count - 1))
    await callback.answer(f"Ступень {count} убрана.")
    await _show(callback.message, callback.from_user.id)


# ── квота, галочка одобренной заявки ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("ambl_quota:"))
async def ladder_quota_toggle(callback: types.CallbackQuery):
    n = _parse_tier(callback.data)
    if n is None or n > await amb_tiers.tiers_count():
        await callback.answer(_STALE, show_alert=True)
        return
    key = tier_key(n, "quota_on")
    turn_on = await get_setting_typed(key) != "on"
    await set_setting_by_admin(callback.from_user.id, key, "on" if turn_on else "off")
    if turn_on:
        limit = int(await get_setting_typed(tier_key(n, "quota")))
        note = (f"Награды ступени {n} ограничены: {limit}. Следующие амбассадоры попадут в лист "
                "ожидания. Число меняется кнопкой «Наград на ступени».")
    else:
        note = (f"Награды ступени {n} без ограничения. Кто уже в листе ожидания, остаётся в нём.")
    await callback.answer(note, show_alert=True)
    await _show(callback.message, callback.from_user.id)


@router.callback_query(F.data == "ambl_req")
async def ladder_require_toggle(callback: types.CallbackQuery):
    turn_on = not await amb_tiers.require_approved_on()
    await set_setting_by_admin(callback.from_user.id, "amb_tiers_require_approved", "on" if turn_on else "off")
    note = (
        "Теперь ступени получат только амбассадоры с одобренной заявкой." if turn_on
        else "Теперь ступени получат все амбассадоры, даже без заявки."
    )
    await callback.answer(note, show_alert=True)
    await _show(callback.message, callback.from_user.id)


# ── снять ступень ────────────────────────────────────────────────────────────────────────

_REVOKE_PROMPT = (
    "У кого снять ступень? Пришлите @username амбассадора, его telegram id (число) или "
    "перешлите сюда любое его сообщение.\n\nПередумали — нажмите «❌ Отмена»."
)


def _revoke_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ambl_rev_cancel")],
    ])


@router.callback_query(F.data == "ambl_rev")
async def revoke_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AmbTierRevoke.waiting_for_person)
    await callback.message.answer(_REVOKE_PROMPT, reply_markup=_revoke_cancel_kb())
    await callback.answer()


@router.callback_query(F.data == "ambl_rev_cancel")
async def revoke_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Отменено, ступень не снята.")
    text, kb = await _ladder_screen(callback.from_user.id)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.message(AmbTierRevoke.waiting_for_person)
async def revoke_person_step(message: types.Message, state: FSMContext):
    if _is_cancel(message):
        await state.clear()
        await message.answer("Отменено, ступень не снята.")
        return
    tid, username, error = _resolve_person_input(message)
    if error:
        await message.answer(error.replace("кого исключить", "у кого снять ступень"),
                             reply_markup=_revoke_cancel_kb())
        return
    user = await db.get_user_by_username(username) if username else await db.get_user(tid)
    if not user:
        await message.answer(
            "Не нашёл такого человека среди зарегистрированных. Пришлите @username, telegram id "
            "или перешлите его сообщение.", reply_markup=_revoke_cancel_kb())
        return
    person_id = int(user["telegram_id"])
    tiers = await amb_tiers_db.list_tiers(person_id)
    if not tiers:
        await message.answer(
            f"У {_person_label(user)} нет ступеней — снимать нечего. Пришлите другого человека.",
            parse_mode="HTML", reply_markup=_revoke_cancel_kb())
        return
    await state.update_data(person_id=person_id, person_label=_person_label(user))
    await state.set_state(AmbTierRevoke.waiting_for_pick)
    rows = [[InlineKeyboardButton(text=f"Ступень {t['tier']}", callback_data=f"ambl_rev_pick:{t['tier']}")]
            for t in tiers]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="ambl_rev_cancel")])
    await message.answer(
        f"<b>{_person_label(user)}</b>\nКакую ступень снять?",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(AmbTierRevoke.waiting_for_pick)
async def revoke_pick_hint(message: types.Message, state: FSMContext):
    if _is_cancel(message):
        await state.clear()
        await message.answer("Отменено, ступень не снята.")
        return
    await message.answer("Выберите ступень кнопкой выше или нажмите «❌ Отмена».",
                         reply_markup=_revoke_cancel_kb())


async def _revoke_context(callback: types.CallbackQuery, state: FSMContext) -> tuple[dict, int] | None:
    tier = _parse_tier(callback.data)
    data = await state.get_data()
    if tier is None or await state.get_state() != AmbTierRevoke.waiting_for_pick.state or not data.get("person_id"):
        await callback.answer(_STALE, show_alert=True)
        return None
    return data, tier


@router.callback_query(F.data.startswith("ambl_rev_pick:"))
async def revoke_pick(callback: types.CallbackQuery, state: FSMContext):
    ctx = await _revoke_context(callback, state)
    if ctx is None:
        return
    data, tier = ctx
    row = next((t for t in await amb_tiers_db.list_tiers(int(data["person_id"])) if int(t["tier"]) == tier), None)
    if row is None:
        await callback.answer("Этой ступени у человека уже нет.", show_alert=True)
        return
    quota_part = (
        " Место в квоте освободится — его можно отдать первому из листа ожидания."
        if row.get("o2o_status") == "granted" else ""
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Снять ступень", callback_data=f"ambl_rev_go:{tier}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="ambl_rev_cancel"),
    ]])
    await _edit_or_send(
        callback.message,
        f"<b>Снять ступень {tier} у {data['person_label']}?</b>\n"
        f"Ступень пропадёт из её прогресса и выгрузки, место в квоте освободится.{quota_part} "
        "Ей ничего не придёт.\n\n"
        f"Автоматически ступень {tier} и старшие этому человеку больше не выдаются, даже если "
        "приглашённых хватает — пока вы сами не нажмёте «↩️ Вернуть ступень» на экране "
        "«🪜 Лестница ступеней».\n"
        "Накрученных приглашённых исключите отдельно («🎓 Ступени амбассадоров» → «🚫 Исключить»).",
        kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ambl_rev_go:"))
async def revoke_go(callback: types.CallbackQuery, state: FSMContext):
    ctx = await _revoke_context(callback, state)
    if ctx is None:
        return
    data, tier = ctx
    done = await amb_tiers.revoke_tier(int(data["person_id"]), tier, by=callback.from_user.id)
    await state.clear()
    await callback.answer(
        f"Ступень {tier} снята." if done else "Этой ступени у человека уже не было.", show_alert=not done)
    await _show(callback.message, callback.from_user.id)


# ── отдать место из листа ожидания ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("ambl_prom:"))
async def promote_confirm(callback: types.CallbackQuery):
    tier = _parse_tier(callback.data)
    first = await amb_tiers_db.first_waitlisted(tier) if tier else None
    cfg = next((c for c in await amb_tiers.tiers_config() if c.n == tier), None)
    if first is None or cfg is None or cfg.quota is None:
        await callback.answer("В листе ожидания этой ступени никого нет.", show_alert=True)
        return
    granted = (await amb_tiers_db.tiers_summary()).get(tier, {}).get("granted", 0)
    if granted >= cfg.quota:
        await callback.answer("Свободных мест на этой ступени нет.", show_alert=True)
        return
    person = await db.get_user(int(first["telegram_id"]))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отдать место", callback_data=f"ambl_prom_go:{tier}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="ambl:main"),
    ]])
    await _edit_or_send(
        callback.message,
        f"<b>Отдать освободившееся место на ступени {tier} первому в листе ожидания — "
        f"{_person_label(person, int(first['telegram_id']))} (дошёл {_short_date(first['reached_at'])})?</b>\n"
        "Ему придёт текст ступени.",
        kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ambl_prom_go:"))
async def promote_go(callback: types.CallbackQuery):
    tier = _parse_tier(callback.data)
    if tier is None:
        await callback.answer(_STALE, show_alert=True)
        return
    result = await amb_tiers.promote_waitlist(tier, by=callback.from_user.id)
    if result.startswith("promoted:"):
        person = await db.get_user(int(result.split(":", 1)[1]))
        await callback.answer(
            f"Место отдано: {(person or {}).get('full_name') or result.split(':', 1)[1]}. "
            "Ему придёт текст ступени.", show_alert=True)
    else:
        note = {
            "no_slot": "Свободных мест на этой ступени уже нет.",
            "empty": "В листе ожидания этой ступени никого не осталось.",
            "no_quota": "У этой ступени нет ограничения наград — лист ожидания не нужен.",
        }.get(result, _STALE)
        await callback.answer(note, show_alert=True)
    await _show(callback.message, callback.from_user.id)


# ── вернуть снятую ступень ───────────────────────────────────────────────────────────────

_UNREVOKE_LIMIT = 20


@router.callback_query(F.data == "ambl_unrev")
async def unrevoke_list(callback: types.CallbackQuery):
    rows = (await amb_tiers_db.list_revocations(await amb_tiers.current_season()))[:_UNREVOKE_LIMIT]
    if not rows:
        await callback.answer("Снятых вручную ступеней нет.", show_alert=True)
        await _show(callback.message, callback.from_user.id)
        return
    buttons = []
    lines = ["<b>↩️ Вернуть ступень</b>",
             "Эти ступени сняты вручную и сами не вернутся. Нажмите на человека — ступень "
             "снова станет доступна, и если приглашённых хватает, выдастся сразу.", ""]
    for r in rows:
        tid, tier = int(r["telegram_id"]), int(r["tier"])
        label = _person_label(await db.get_user(tid), tid)
        lines.append(f"• {label} — ступень {tier}, снята {_short_date(r['revoked_at'])}")
        buttons.append([InlineKeyboardButton(
            text=f"↩️ {tier} · {html.unescape(label)}"[:60], callback_data=f"ambl_unrev_go:{tid}:{tier}")])
    buttons.append([InlineKeyboardButton(text="← К лестнице ступеней", callback_data="ambl:main")])
    await _edit_or_send(callback.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("ambl_unrev_go:"))
async def unrevoke_go(callback: types.CallbackQuery):
    try:
        _, tid_raw, tier_raw = (callback.data or "").split(":")
        tid, tier = int(tid_raw), int(tier_raw)
    except ValueError:
        await callback.answer(_STALE, show_alert=True)
        return
    granted = await amb_tiers.unrevoke_tier(tid, tier, by=callback.from_user.id)
    label = _person_label(await db.get_user(tid), tid)
    if granted:
        note = f"Ступень {tier} возвращена: {html.unescape(label)} получит уведомление."
    else:
        note = (f"Запрет на ступень {tier} снят. Пока приглашённых не хватает для неё — "
                "выдастся сама, когда наберётся.")
    await callback.answer(note, show_alert=True)
    remaining = await amb_tiers_db.list_revocations(await amb_tiers.current_season())
    if remaining:
        await unrevoke_list(callback)
    else:
        await _show(callback.message, callback.from_user.id)


# Экран «💰 Баллы и приватность» (admin_amb_points, ambpt_*) — хвост admin.router после
# хендлеров этого файла.
from handlers.amb import admin_amb_points  # noqa: E402,F401
