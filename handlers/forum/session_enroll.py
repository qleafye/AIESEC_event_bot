"""Делегатский поток записи на сессии программы (в чате).

Своего `Router()` нет: хендлеры садятся на `handlers.user_actions.router`, модуль подключён
хвостовым импортом `user_actions.py` до фолбэка `reg_handoff_idle_fallback` (иначе текст кнопки
меню ушёл бы в него). Три входа: кнопка меню (подпись настраивается — `DynamicMenuText`),
deep-link `/start sessions` (`handlers/forum/forum_deeplinks.py`) и callback `se:open` (кнопка из
результата теста).

Поток: картинка повестки -> трек (или «Смешать») -> по слотам дня выбор одной сессии ->
«Моё расписание» -> подтверждение. Все правила (допуск, город, дедлайн, закрытие, лимит,
пересечения) живут в `services.session_enroll` и перепроверяются на КАЖДЫЙ callback: кнопка
в старом сообщении ничего не обходит. Тексты делегату — только из реестра настроек.

Callback'и (числа, <= 64 байт): se:open, se:t:{tid} (0 = смешать), se:s:{tid}:{gi},
se:p:{tid}:{gi}:{sid}, se:r:{tid}:{gi}:{sid}, se:k:{tid}:{gi}, se:c:{tid}:{gi}:{sid},
se:my, se:ok, se:ed. `gi` — глобальный индекс слота в порядке `slots_for_city`."""
import html
import logging
import re

from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram import F

from domain.cities import get_setting_typed_for_city, normalize_city
from database import session_enroll_db as edb
from database.db import get_user
from handlers.i18n import reg_i18n
from handlers.user_actions import _returning_text_if_past_season, ensure_registered, router
from keyboards.menu_dynamic import DynamicMenuText
from services import session_enroll as svc
from services.checkin import checkin_denial
from services.program import program_photo_caption, resolve_program_photo_source

logger = logging.getLogger(__name__)

_DEADLINE_WITH_PREPOSITION = re.compile(r"\s*(?:до|по|в|во|на|с)\s+\{deadline\}")


class _Ctx:
    """Делегат, его город и перевод — всё, что нужно экрану для текстов."""

    def __init__(self, user, city, lang, tr_map):
        self.user, self.city, self.lang, self.tr_map = user, city, lang, tr_map

    async def t(self, key: str, **subs) -> str:
        raw = await get_setting_typed_for_city(key, self.city)
        return reg_i18n.tr_fmt(raw or "", self.lang, self.tr_map, **subs)

    async def with_deadline(self, key: str) -> str:
        """Текст с {deadline}; нет дедлайна — фраза «до {deadline}» выпадает целиком."""
        raw = await get_setting_typed_for_city(key, self.city)
        text = reg_i18n.tr_text(raw or "", self.lang, self.tr_map)
        label = await svc.deadline_label(self.city)
        if label:
            return text.replace("{deadline}", label)
        return _DEADLINE_WITH_PREPOSITION.sub("", text).replace("{deadline}", "").strip()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _day_label(day: str) -> str:
    return f"{day[8:10]}.{day[5:7]}"


def _span(start: str, end: str) -> str:
    return f"{start}–{end}"


async def _show(target: types.Message, text: str, kb, *, edit: bool) -> None:
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb)
            return
        except Exception as e:  # сообщение устарело/не изменилось — просто шлём новое
            logger.info("session_enroll: edit_text не удался: %s", e)
    try:
        await target.answer(text, reply_markup=kb)
    except Exception as e:  # разметка в тексте не разобралась — шлём как обычный текст
        logger.warning("session_enroll: answer с разметкой не удался: %s", e)
        await target.answer(text, reply_markup=kb, parse_mode=None)


async def _ctx_for(event, telegram_id: int) -> tuple[_Ctx | None, str | None]:
    """(контекст, None) для допущенного делегата или (None, текст отказа)."""
    lang, tr_map = await reg_i18n.ctx_for(event)
    user = await get_user(telegram_id)
    city = normalize_city(user.get("event_city")) if user else None
    ctx = _Ctx(user, city, lang, tr_map)
    denial = await checkin_denial(user)
    if denial == "past_season":
        text = await _returning_text_if_past_season(telegram_id, user, lang, tr_map)
        return None, text or await ctx.t("session_enroll_not_approved_text")
    if denial:
        return None, await ctx.t("session_enroll_not_approved_text")
    if not city or not await svc.module_enabled(city):
        return None, await ctx.t("session_enroll_disabled_text")
    return ctx, None


async def _flat_slots(city: str) -> list[tuple[str, list[dict]]]:
    return [(d["day"], slot) for d in await svc.slots_for_city(city) for slot in d["slots"]]


def _parse(data: str, count: int) -> list[int] | None:
    """Числа после префикса `se:x:` или None на мусор (T-38-25)."""
    parts = data.split(":")
    if len(parts) != count + 2:
        return None
    try:
        return [int(p) for p in parts[2:]]
    except ValueError:
        return None


# ── Экраны ──────────────────────────────────────────────────────────────────────────────────

async def _tracks_screen(target: types.Message, ctx: _Ctx, *, edit: bool) -> None:
    rows = [[_btn(t["name"], f"se:t:{t['id']}")] for t in await edb.list_tracks(ctx.city)]
    rows.append([_btn(await ctx.t("session_enroll_mix_button"), "se:t:0")])
    rows.append([_btn(await ctx.t("session_enroll_schedule_title"), "se:my")])
    await _show(target, await ctx.t("session_enroll_intro_text"), _kb(rows), edit=edit)


async def _slot_screen(target: types.Message, ctx: _Ctx, tid: int, gi: int, *, edit: bool = True) -> None:
    flat = await _flat_slots(ctx.city)
    if not 0 <= gi < len(flat):
        await _schedule_screen(target, ctx, edit=edit)
        return
    day, slot = flat[gi]
    mine = {s["id"] for s in await edb.list_user_enrollments(ctx.user["telegram_id"], ctx.city)}
    ordered = sorted(slot, key=lambda s: 0 if tid and s.get("track_id") == tid else 1)
    rows, chosen_id = [], None
    for s in ordered:
        mark = ""
        if s["id"] in mine:
            # слот строится транзитивно, запись проверяется попарно: в слоте может быть
            # несколько совместимых записей — отмечаем все, «Снять выбор» снимет их все
            mark, chosen_id = "✅ ", chosen_id if chosen_id is not None else s["id"]
        elif tid and s.get("track_id") == tid:
            mark = "⭐ "
        label = f"{mark}{s['title']}"
        if s.get("open_state") == "closed":
            label += f" · {await ctx.t('session_enroll_closed_label')}"
        elif s.get("open_state") == "full":
            label += f" · {await ctx.t('session_enroll_full_label')}"
        rows.append([_btn(label, f"se:p:{tid}:{gi}:{s['id']}")])
    if chosen_id is not None:
        rows.append([_btn(await ctx.t("session_enroll_clear_button"), f"se:c:{tid}:{gi}:{chosen_id}")])
    nxt = f"se:s:{tid}:{gi + 1}" if gi + 1 < len(flat) else "se:my"
    rows.append([_btn(await ctx.t("session_enroll_next_button"), nxt)])
    rows.append([_btn(await ctx.t("session_enroll_schedule_title"), "se:my")])
    start = min(s["start_time"] for s in slot)
    end = max(s["end_time"] for s in slot)
    text = await ctx.t("session_enroll_slot_text", day=_day_label(day), time=_span(start, end))
    await _show(target, text, _kb(rows), edit=edit)


async def _schedule_text(ctx: _Ctx) -> str:
    title = await ctx.t("session_enroll_schedule_title")
    schedule = await svc.my_schedule(ctx.user["telegram_id"], ctx.city)
    common = await ctx.t("session_enroll_common_label")
    lines = [title]
    chosen_any = False
    for day in schedule:
        lines.append("")
        lines.append(_day_label(day["day"]))
        for item in day["items"]:
            s = item["session"]
            tail = f" ({common})" if item["kind"] == "common" else ""
            chosen_any = chosen_any or item["kind"] == "chosen"
            lines.append(f"{_span(s['start_time'], s['end_time'])} {html.escape(s['title'] or '')}{tail}")
    if not chosen_any:
        lines.append("")
        lines.append(await ctx.t("session_enroll_schedule_empty"))
    return "\n".join(lines)


async def _schedule_screen(target: types.Message, ctx: _Ctx, *, edit: bool, head: str | None = None) -> None:
    text = await _schedule_text(ctx)
    if head:
        text = f"{head}\n\n{text}"
    rows = []
    if await svc.deadline_passed(ctx.city):
        text += "\n\n" + await ctx.with_deadline("session_enroll_err_deadline")
    else:
        rows.append([_btn(await ctx.t("session_enroll_change_button"), "se:ed"),
                     _btn(await ctx.t("session_enroll_confirm_button"), "se:ok")])
    await _show(target, text, _kb(rows), edit=edit)


async def open_enroll(target: types.Message, telegram_id: int, *, edit: bool = False) -> bool:
    """Общий вход: гейт, повестка картинкой, экран треков. True — экран показан."""
    ctx, refusal = await _ctx_for(target, telegram_id)
    if ctx is None:
        await target.answer(refusal)
        return False
    if not await svc.slots_for_city(ctx.city):
        await target.answer(await ctx.t("session_enroll_no_sessions_text"))
        return False
    if not edit:
        await _send_agenda_photo(target, ctx.city)
    if await svc.deadline_passed(ctx.city):
        await _schedule_screen(target, ctx, edit=False)
        return True
    await _tracks_screen(target, ctx, edit=edit)
    return True


async def _send_agenda_photo(target: types.Message, city: str) -> None:
    """Повестка перед выбором. Сбой фото не ломает поток (fail-soft, как `show_program`)."""
    try:
        source = await resolve_program_photo_source(city)
        if not source:
            return
        caption = await program_photo_caption(city)
        photo = source.get("file_id") or FSInputFile(source["path"])
        await target.answer_photo(photo, caption=html.escape(caption) if caption else None,
                                  parse_mode="HTML")
    except Exception as e:
        logger.warning("session_enroll: фото повестки не отправилось: %s", e)


async def open_from_deeplink(message: types.Message, state: FSMContext) -> bool:
    """`/start sessions`: незарегистрированному — False (идёт обычный /start)."""
    if not await get_user(message.from_user.id):
        return False
    await state.clear()
    await open_enroll(message, message.from_user.id)
    return True


# ── Хендлеры ────────────────────────────────────────────────────────────────────────────────

@router.message(DynamicMenuText("menu_session_enroll"))
async def session_enroll_menu(message: types.Message):
    if not await ensure_registered(message):
        return
    await open_enroll(message, message.from_user.id)


async def _callback_ctx(callback: types.CallbackQuery) -> _Ctx | None:
    """Гейт каждого callback: допуск + модуль. Отказ — alert текстом реестра."""
    ctx, refusal = await _ctx_for(callback, callback.from_user.id)
    if ctx is None:
        await callback.answer(refusal, show_alert=True)
    return ctx


async def _editing_closed(callback: types.CallbackQuery, ctx: _Ctx) -> bool:
    """Дедлайн прошёл: меняться нельзя — alert + расписание без кнопок изменения."""
    if not await svc.deadline_passed(ctx.city):
        return False
    await callback.answer(await ctx.with_deadline("session_enroll_err_deadline"), show_alert=True)
    await _schedule_screen(callback.message, ctx, edit=True)
    return True


@router.callback_query(F.data == "se:open")
async def se_open(callback: types.CallbackQuery):
    await callback.answer()
    await open_enroll(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("se:t:"))
async def se_track(callback: types.CallbackQuery):
    args = _parse(callback.data, 1)
    ctx = await _callback_ctx(callback)
    if ctx is None:
        return
    if args is None or await _editing_closed(callback, ctx):
        if args is None:
            await callback.answer()
        return
    await callback.answer()
    await _slot_screen(callback.message, ctx, args[0], 0)


@router.callback_query(F.data.startswith("se:s:"))
async def se_slot(callback: types.CallbackQuery):
    args = _parse(callback.data, 2)
    ctx = await _callback_ctx(callback)
    if ctx is None:
        return
    await callback.answer()
    if args is None:
        await _schedule_screen(callback.message, ctx, edit=True)
        return
    await _slot_screen(callback.message, ctx, args[0], args[1])


async def _replace_screen(callback, ctx, tid, gi, old, new) -> None:
    text = await ctx.t("session_enroll_replace_question", old=html.escape(old["title"] or ""),
                       new=html.escape(new["title"] or ""))
    rows = [[_btn(await ctx.t("session_enroll_replace_yes"), f"se:r:{tid}:{gi}:{new['id']}")],
            [_btn(await ctx.t("session_enroll_replace_no"), f"se:k:{tid}:{gi}")]]
    await _show(callback.message, text, _kb(rows), edit=True)


async def _after_pick(callback, ctx, tid: int, gi: int) -> None:
    flat = await _flat_slots(ctx.city)
    if gi + 1 < len(flat):
        await _slot_screen(callback.message, ctx, tid, gi + 1)
    else:
        await _schedule_screen(callback.message, ctx, edit=True)


async def _do_enroll(callback: types.CallbackQuery, *, confirm_replace: bool) -> None:
    args = _parse(callback.data, 3)
    ctx = await _callback_ctx(callback)
    if ctx is None:
        return
    if args is None:
        await callback.answer()
        await _schedule_screen(callback.message, ctx, edit=True)
        return
    tid, gi, sid = args
    out = await svc.enroll(callback.from_user.id, sid, confirm_replace=confirm_replace)
    if out.status in ("ok", "already"):
        await callback.answer()
        await _after_pick(callback, ctx, tid, gi)
    elif out.status == "conflict":
        await callback.answer()
        await _replace_screen(callback, ctx, tid, gi, out.conflicts[0], out.session)
    else:
        await _alert_refusal(callback, ctx, out)


async def _alert_refusal(callback: types.CallbackQuery, ctx: _Ctx, out) -> None:
    title = (out.session or {}).get("title", "")
    if out.status == "deadline":
        text = await ctx.with_deadline("session_enroll_err_deadline")
    elif out.status in ("closed", "full"):
        text = await ctx.t(out.text_key, title=title)
    else:  # чужой город, сессия без трека, сессия удалена тоже несут свой ключ реестра
        text = await ctx.t(out.text_key or "session_enroll_unavailable_text")
    await callback.answer(text, show_alert=True)


@router.callback_query(F.data.startswith("se:p:"))
async def se_pick(callback: types.CallbackQuery):
    await _do_enroll(callback, confirm_replace=False)


@router.callback_query(F.data.startswith("se:r:"))
async def se_replace(callback: types.CallbackQuery):
    await _do_enroll(callback, confirm_replace=True)


@router.callback_query(F.data.startswith("se:k:"))
async def se_keep(callback: types.CallbackQuery):
    args = _parse(callback.data, 2)
    ctx = await _callback_ctx(callback)
    if ctx is None:
        return
    await callback.answer()
    if args is None:
        await _schedule_screen(callback.message, ctx, edit=True)
        return
    await _slot_screen(callback.message, ctx, args[0], args[1])


@router.callback_query(F.data.startswith("se:c:"))
async def se_clear(callback: types.CallbackQuery):
    args = _parse(callback.data, 3)
    ctx = await _callback_ctx(callback)
    if ctx is None:
        return
    if args is None:
        await callback.answer()
        await _schedule_screen(callback.message, ctx, edit=True)
        return
    tid, gi, sid = args
    flat = await _flat_slots(ctx.city)
    slot_ids = {s["id"] for s in flat[gi][1]} if 0 <= gi < len(flat) else {sid}
    mine = {s["id"] for s in await edb.list_user_enrollments(ctx.user["telegram_id"], ctx.city)}
    # снимаем ВСЕ записи делегата внутри показанного слота, а не только одну
    for session_id in sorted((slot_ids & mine) or {sid}):
        out = await svc.unenroll(callback.from_user.id, session_id)
        if out.status != "ok":
            await _alert_refusal(callback, ctx, out)
            return
    await callback.answer()
    await _slot_screen(callback.message, ctx, tid, gi)


@router.callback_query(F.data == "se:my")
async def se_my(callback: types.CallbackQuery):
    ctx = await _callback_ctx(callback)
    if ctx is None:
        return
    await callback.answer()
    await _schedule_screen(callback.message, ctx, edit=True)


@router.callback_query(F.data == "se:ed")
async def se_edit(callback: types.CallbackQuery):
    ctx = await _callback_ctx(callback)
    if ctx is None or await _editing_closed(callback, ctx):
        return
    await callback.answer()
    await _tracks_screen(callback.message, ctx, edit=True)


@router.callback_query(F.data == "se:ok")
async def se_confirm(callback: types.CallbackQuery):
    ctx = await _callback_ctx(callback)
    if ctx is None or await _editing_closed(callback, ctx):
        return
    await svc.confirm(callback.from_user.id, ctx.city)
    await callback.answer()
    await _schedule_screen(callback.message, ctx, edit=True,
                           head=await ctx.with_deadline("session_enroll_confirmed_text"))
