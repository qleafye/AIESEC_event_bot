"""«🎓 Сертификаты» — экран хаба «🎪 Форум: функции».

INVARIANT: каждый @router.* — декоратор в ОДНУ строку (golden-снапшот роутеров читает их построчно).

Всё, что менеджеру нужно для выдачи сертификатов, кнопками:
- 🕐 когда открыть выдачу (дата и время по Москве; с этого момента бот сам пишет «готов»);
- 📄 шаблоны: PDF дизайнера на каждый вид и, по желанию, версии на один день форума — бот
  сразу присылает пример с именем менеджера, чтобы было видно, как легло;
- 🙋 волонтёры и ⭐ амбассадоры @никами (участники определяются отметкой на входе);
- 👁 «Проверить на себе» — все загруженные шаблоны с вашим именем.

Форма шва — как у соседей (`handlers/forum/admin_forum_tz.py`): своего `Router()` нет,
`from handlers.admin import router`, импорт из хвоста `handlers/admin.py`. Право —
`moderate_reg`, как у остальных экранов хаба (ADMIN_CAPS: `certadm_*`, `state:CertAdmin:*`)."""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from database import certificates_db as cdb
from database.certificates_db import KIND_AMBASSADOR, KIND_VOLUNTEER, KINDS, VARIANT_ALL
from handlers.admin import router
from handlers.states import CertAdmin
from services.forum import certificates as certs
from services.forum.checkin import ENTRY_POINT
from services.infra.timeutil import msk_now

logger = logging.getLogger(__name__)

_PEOPLE_TITLES = {KIND_VOLUNTEER: "🙋 Волонтёры", KIND_AMBASSADOR: "⭐ Амбассадоры"}
_EXAMPLE_NAME = "Анна Иванова"


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


async def _variants() -> list[str]:
    return [VARIANT_ALL, *await certs.forum_days()]


# ── главный экран ──────────────────────────────────────────────────────────────────────

async def home_text_kb() -> tuple[str, InlineKeyboardMarkup]:
    open_raw = await cdb.get_open_at()
    when = certs.human_open_at(open_raw)
    if not when:
        status = "❌ не назначена — делегаты видят «сертификатов пока нет»"
    elif await certs.is_open():
        status = f"✅ открыта с {when}"
    else:
        status = f"⏳ откроется {when}"

    templates = await cdb.list_templates()
    variants = await _variants()
    tpl_lines = []
    for kind in KINDS:
        marks = ", ".join(
            f"{certs.variant_label(v)} {'✅' if (kind, v) in templates else '—'}"
            for v in certs.kind_variants(kind, variants)
        )
        tpl_lines.append(f"• {certs.KIND_SHORT[kind]}: {marks}")

    volunteers = await cdb.list_people(KIND_VOLUNTEER)
    ambassadors = await cdb.list_people(KIND_AMBASSADOR)
    checked_in = len(await cdb.checked_in_ids(ENTRY_POINT))
    sends = await cdb.send_counts()
    text = (
        "🎓 <b>Сертификаты</b>\n\n"
        f"Выдача: {status} (время московское)\n\n"
        "<b>Кому</b>\n"
        f"• Участник — все, кого отметили на входе: сейчас {checked_in}\n"
        "• Компетенции — отмеченные на входе, кто прошёл тест компетенций: шкалы — их проценты\n"
        f"• Волонтёр — по списку @ников: {len(volunteers)}\n"
        f"• Амбассадор — по списку @ников: {len(ambassadors)}\n"
        "Кто в нескольких ролях — получит все свои сертификаты.\n\n"
        "<b>Шаблоны</b>\n" + "\n".join(tpl_lines) + "\n"
        "<i>Если версии на один день нет — человеку, который был один день, уйдёт «все дни».</i>\n\n"
        f"<b>Выдано</b>: участник {sends.get('participant', 0)}, компетенции {sends.get('competency', 0)}, волонтёр {sends.get('volunteer', 0)}, "
        f"амбассадор {sends.get('ambassador', 0)}. Сообщение «готов» получили: {await cdb.announce_count()}.\n\n"
        "Когда выдача откроется, бот сам напишет каждому, кому положен сертификат, с кнопкой "
        "«🎓 Получить сертификат». Перед отправкой человек проверяет, как написано имя. "
        "Потерявшим сообщение — команда /certificate."
    )
    kb = _kb([
        [("🕐 Когда открыть выдачу", "certadm_open")],
        [("📄 Шаблоны", "certadm_tpls")],
        [(f"🙋 Волонтёры ({len(volunteers)})", f"certadm_ppl:{KIND_VOLUNTEER}"),
         (f"⭐ Амбассадоры ({len(ambassadors)})", f"certadm_ppl:{KIND_AMBASSADOR}")],
        [("👁 Проверить на себе", "certadm_preview")],
        [("◀️ Назад", "admin_forum_functions")],
    ])
    return text, kb


@router.callback_query(F.data == "certadm_home")
async def certadm_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await home_text_kb()
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _back_home(message: types.Message) -> None:
    text, kb = await home_text_kb()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── когда открыть ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "certadm_open")
async def certadm_open(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(CertAdmin.open_at)
    when = certs.human_open_at(await cdb.get_open_at())
    await callback.message.answer(
        (f"Сейчас: {when}.\n\n" if when else "")
        + "Когда открыть выдачу? Пришлите дату и время по Москве: ДД.ММ.ГГГГ ЧЧ:ММ\n\n"
        "Пример: 02.11.2026 12:00\n\n"
        "В это время бот напишет всем, кому положен сертификат. «-» — снять дату (выдача закрыта).",
        reply_markup=_kb([[("◀️ Отмена", "certadm_home")]]),
    )
    await callback.answer()


@router.message(CertAdmin.open_at, F.text)
async def certadm_open_input(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if raw == "-":
        await cdb.set_open_at(None)
        await state.clear()
        await message.answer("✅ Дата снята — выдача закрыта.")
        await _back_home(message)
        return
    try:
        at = datetime.strptime(raw, "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer(
            "Не понял дату. Пришлите в виде ДД.ММ.ГГГГ ЧЧ:ММ, например: 02.11.2026 12:00 — или «-», чтобы снять."
        )
        return
    await cdb.set_open_at(at.strftime(certs.OPEN_AT_FORMAT))
    await state.clear()
    now = msk_now().replace(tzinfo=None)
    note = (
        "Время уже наступило — бот начнёт рассылку «сертификат готов» в ближайшие 5 минут."
        if at <= now else "В это время бот начнёт рассылку «сертификат готов»."
    )
    await message.answer(f"✅ Выдача откроется {at.strftime('%d.%m.%Y в %H:%M')}. {note}")
    await _back_home(message)


# ── шаблоны ────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "certadm_tpls")
async def certadm_tpls(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    templates = await cdb.list_templates()
    rows = []
    for kind in KINDS:
        for variant in certs.kind_variants(kind, await _variants()):
            mark = "✅" if (kind, variant) in templates else "➕"
            rows.append([(
                f"{mark} {certs.KIND_SHORT[kind]} · {certs.variant_label(variant)}",
                f"certadm_tpl:{kind}:{variant}",
            )])
    rows.append([("◀️ Назад", "certadm_home")])
    await callback.message.answer(
        "📄 <b>Шаблоны сертификатов</b>\n\n"
        "Нажмите на строку и пришлите PDF от дизайнера. «все дни» — основной шаблон, он нужен "
        "обязательно. Версии на один день — по желанию: их получат участники, которых отметили "
        "только в этот день.\n\nИмя бот впишет в белую полосу под «Настоящий сертификат подтверждает, что». "
        "В сертификате компетенций ещё и заполнит пять шкал процентами из теста компетенций — "
        "шаблон нужен с пустыми шкалами «0%».",
        parse_mode="HTML", reply_markup=_kb(rows),
    )
    await callback.answer()


def _parse_tpl(data: str) -> tuple[str, str] | None:
    parts = data.split(":")
    if parts[3:] == ["go"]:  # `certadm_tpldel:<вид>:<вариант>:go` — подтверждённое удаление
        parts = parts[:3]
    if len(parts) != 3 or parts[1] not in KINDS:
        return None
    if parts[2] != VARIANT_ALL and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[2]):
        return None
    return parts[1], parts[2]


@router.callback_query(F.data.startswith("certadm_tpl:"))
async def certadm_tpl(callback: types.CallbackQuery, state: FSMContext):
    parsed = _parse_tpl(callback.data)
    if parsed is None:
        await callback.answer("Не понял, какой шаблон — откройте список заново.", show_alert=True)
        return
    kind, variant = parsed
    await state.set_state(CertAdmin.template)
    await state.update_data(kind=kind, variant=variant)
    title = f"{certs.KIND_TITLES[kind]} · {certs.variant_label(variant)}"
    rows = []
    if (kind, variant) in await cdb.list_templates():
        rows.append([("🗑 Убрать этот шаблон", f"certadm_tpldel:{kind}:{variant}")])
    rows.append([("◀️ Назад", "certadm_tpls")])
    await callback.message.answer(
        f"Пришлите PDF-шаблон «{title}» одним файлом (как документ, не фото).\n\n"
        "Бот сразу пришлёт пример с вашим именем — проверьте, как легло.",
        reply_markup=_kb(rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("certadm_tpldel:"))
async def certadm_tpldel(callback: types.CallbackQuery, state: FSMContext):
    parsed = _parse_tpl(callback.data)
    if parsed is None:
        await callback.answer("Не понял, какой шаблон — откройте список заново.", show_alert=True)
        return
    kind, variant = parsed
    if not callback.data.endswith(":go"):
        await callback.message.answer(
            f"Убрать шаблон «{certs.KIND_TITLES[kind]} · {certs.variant_label(variant)}»? Пока не загрузите "
            "новый, этот сертификат не соберётся (версия на один день заменится на «все дни»). "
            "Уже выданные файлы у людей останутся.",
            reply_markup=_kb([[("🗑 Да, убрать", f"certadm_tpldel:{kind}:{variant}:go"),
                               ("Отмена", f"certadm_tpl:{kind}:{variant}")]]),
        )
        await callback.answer()
        return
    await cdb.delete_template(kind, variant)
    await state.clear()
    await callback.answer("Шаблон убран")
    await _back_home(callback.message)


@router.message(CertAdmin.template, F.document)
async def certadm_tpl_file(message: types.Message, state: FSMContext, bot):
    doc = message.document
    if not ((doc.mime_type or "") == "application/pdf" or (doc.file_name or "").lower().endswith(".pdf")):
        await message.answer("Это не PDF. Пришлите файл шаблона в формате PDF.")
        return
    data = await state.get_data()
    kind, variant = data.get("kind"), data.get("variant")
    if kind not in KINDS:
        await state.clear()
        await message.answer("Не понял, для какого шаблона файл — откройте «📄 Шаблоны» заново.")
        return
    name = await certs.default_name(message.from_user.id, message.from_user.full_name) or _EXAMPLE_NAME
    try:
        pdf = await certs.build_pdf(bot, doc.file_id, name, kind, await certs.competency_percents(message.from_user.id))
    except Exception as e:
        logger.error(f"certadm_tpl_file: шаблон не читается: {e}")
        await message.answer("Не получилось открыть этот PDF. Пересохраните его из редактора и пришлите ещё раз.")
        return
    await cdb.set_template(kind, variant, doc.file_id, message.from_user.id)
    await state.clear()
    await message.answer_document(
        BufferedInputFile(pdf, filename=certs.file_name(kind, name)),
        caption=f"✅ Шаблон «{certs.KIND_TITLES[kind]} · {certs.variant_label(variant)}» сохранён. Вот пример.",
    )
    await _back_home(message)


@router.message(CertAdmin.template)
async def certadm_tpl_not_file(message: types.Message):
    await message.answer("Жду PDF-файл шаблона. Пришлите его как документ — или нажмите «◀️ Назад».")


# ── волонтёры и амбассадоры ────────────────────────────────────────────────────────────

async def _people_text_kb(kind: str) -> tuple[str, InlineKeyboardMarkup]:
    people = await cdb.list_people(kind)
    shown = ", ".join(f"@{html.escape(p)}" for p in people) if people else "пока никого"
    text = (
        f"{_PEOPLE_TITLES[kind]} — <b>{len(people)}</b>\n\n{shown}\n\n"
        "Чтобы добавить — пришлите @ники через пробел, запятую или с новой строки.\n"
        "Пример: @ivanov @maria_k\n\n"
        "Чтобы убрать одного — пришлите его ник с минусом: -@ivanov"
    )
    rows = []
    if people:
        rows.append([("🗑 Очистить весь список", f"certadm_clr:{kind}")])
    rows.append([("◀️ Назад", "certadm_home")])
    return text, _kb(rows)


@router.callback_query(F.data.startswith("certadm_ppl:"))
async def certadm_ppl(callback: types.CallbackQuery, state: FSMContext):
    kind = callback.data.split(":", 1)[1]
    if kind not in _PEOPLE_TITLES:
        await callback.answer("Не понял, какой список — откройте «🎓 Сертификаты» заново.", show_alert=True)
        return
    await state.set_state(CertAdmin.people)
    await state.update_data(kind=kind)
    text, kb = await _people_text_kb(kind)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.message(CertAdmin.people, F.text)
async def certadm_ppl_input(message: types.Message, state: FSMContext):
    kind = (await state.get_data()).get("kind")
    if kind not in _PEOPLE_TITLES:
        await state.clear()
        await message.answer("Не понял, какой список — откройте «🎓 Сертификаты» заново.")
        return
    add, remove, bad = [], [], []
    for token in re.split(r"[\s,;]+", message.text.strip()):
        if not token:
            continue
        minus = token.startswith("-")
        name = cdb.normalize_username(token[1:] if minus else token)
        if not re.fullmatch(r"[a-z0-9_]{4,32}", name):
            bad.append(token)
            continue
        (remove if minus else add).append(name)
    if not add and not remove:
        await message.answer(
            "Не нашёл ни одного ника. Пришлите @ники через пробел, например: @ivanov @maria_k"
        )
        return
    added = await cdb.add_people(kind, add)
    for name in remove:
        await cdb.remove_person(kind, name)
    parts = [f"✅ Добавлено: {added}"]
    if len(add) > added:
        parts.append(f"уже были в списке: {len(add) - added}")
    if remove:
        parts.append(f"убрано: {len(remove)}")
    reply = ", ".join(parts) + "."
    if bad:
        reply += "\n\nНе похоже на ник, пропустил: " + ", ".join(html.escape(b) for b in bad[:10])
    await message.answer(reply)
    text, kb = await _people_text_kb(kind)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("certadm_clr:"))
async def certadm_clr(callback: types.CallbackQuery):
    kind = callback.data.split(":", 1)[1]
    if kind not in _PEOPLE_TITLES:
        await callback.answer("Не понял, какой список.", show_alert=True)
        return
    count = len(await cdb.list_people(kind))
    await callback.message.answer(
        f"Удалить всех {count} из списка «{_PEOPLE_TITLES[kind]}»? Сертификат по этой роли им не придёт, "
        "пока вы не добавите их снова. Уже выданные файлы у людей останутся.",
        reply_markup=_kb([[("🗑 Да, очистить", f"certadm_clrgo:{kind}"), ("Отмена", f"certadm_ppl:{kind}")]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("certadm_clrgo:"))
async def certadm_clrgo(callback: types.CallbackQuery, state: FSMContext):
    kind = callback.data.split(":", 1)[1]
    if kind not in _PEOPLE_TITLES:
        await callback.answer("Не понял, какой список.", show_alert=True)
        return
    await cdb.clear_people(kind)
    await state.clear()
    await callback.answer("Список очищен")
    await _back_home(callback.message)


# ── проверить на себе ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "certadm_preview")
async def certadm_preview(callback: types.CallbackQuery, bot):
    templates = await cdb.list_templates()
    if not templates:
        await callback.answer("Шаблонов пока нет — загрузите их в «📄 Шаблоны».", show_alert=True)
        return
    await callback.answer("Собираю…")
    name = await certs.default_name(callback.from_user.id, callback.from_user.full_name) or _EXAMPLE_NAME
    for (kind, variant), file_id in sorted(templates.items()):
        try:
            pdf = await certs.build_pdf(bot, file_id, name, kind, await certs.competency_percents(callback.from_user.id))
        except Exception as e:
            logger.error(f"certadm_preview {kind}/{variant}: {e}")
            await callback.message.answer(f"❌ {certs.KIND_TITLES[kind]} · {certs.variant_label(variant)}: не собрался.")
            continue
        await callback.message.answer_document(
            BufferedInputFile(pdf, filename=certs.file_name(kind, name)),
            caption=f"{certs.KIND_TITLES[kind]} · {certs.variant_label(variant)}",
        )
