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
import io
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
_TITLE_HINT = "Пришлите название вида, например: Спикер"
_FONT_HINT = "Это не файл шрифта. Пришлите .ttf или .otf, не больше 2 МБ."
_FONT_MAX = 2 * 1024 * 1024
_FIELD_WORDS = {"name": "имя", "date": "дата", "city": "город", "scales": "шкалы"}


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def _who(row: dict) -> str:
    audience = row.get("audience")
    if audience == "checkin":
        return "отметка на входе"
    if audience == "test":
        return "тест компетенций"
    if audience == "nicks":
        return "список ников"
    if audience == "role":
        from services.access.role_registry import ROLES
        label = (ROLES.get(row.get("role_key") or "") or {}).get("label")
        return label or "роль"
    return "кому ещё не выбрано"


def _field_words(row: dict) -> str:
    words = [_FIELD_WORDS[item] for item in row.get("fields") or [] if item in _FIELD_WORDS]
    if row.get("by_day"):
        words.append("версия на один день")
    return ", ".join(words) or "имя"


async def _label(kind: str) -> str:
    if kind in cdb.KIND_SHORT:
        return cdb.KIND_SHORT[kind]
    row = await cdb.get_kind(kind)
    return row["title"] if row else "Сертификат"


async def _people_ok(kind: str) -> bool:
    if kind in _PEOPLE_TITLES:
        return True
    row = await cdb.get_kind(kind)
    return bool(row and row["audience"] == "nicks")


async def _people_heading(kind: str) -> str:
    if kind in _PEOPLE_TITLES:
        return _PEOPLE_TITLES[kind]
    row = await cdb.get_kind(kind)
    return row["title"] if row else "Список"


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
    custom = await cdb.list_kinds()
    custom_lines = ""
    if custom:
        custom_lines = "\n".join(
            f"• {html.escape(row['title'])} — {html.escape(_who(row))}, {html.escape(_field_words(row))}"
            for row in custom
        ) + "\n"
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
        f"{custom_lines}"
        "Кто в нескольких ролях — получит все свои сертификаты.\n\n"
        "<b>Шаблоны</b>\n" + "\n".join(tpl_lines) + "\n"
        "<i>Если версии на один день нет — человеку, который был один день, уйдёт «все дни».</i>\n\n"
        f"<b>Выдано</b>: участник {sends.get('participant', 0)}, компетенции {sends.get('competency', 0)}, волонтёр {sends.get('volunteer', 0)}, "
        f"амбассадор {sends.get('ambassador', 0)}. Сообщение «готов» получили: {await cdb.announce_count()}.\n\n"
        "Когда выдача откроется, бот сам напишет каждому, кому положен сертификат, с кнопкой "
        "«🎓 Получить сертификат». Участник и компетенции проверяют имя перед файлом. "
        "Волонтёру и амбассадору бот сам присылает PDF — имя берётся из списка. "
        "Потерявшим сообщение — команда /certificate."
    )
    rows = [
        [("🕐 Когда открыть выдачу", "certadm_open")],
        [("📄 Шаблоны", "certadm_tpls")],
        [(f"🙋 Волонтёры ({len(volunteers)})", f"certadm_ppl:{KIND_VOLUNTEER}"),
         (f"⭐ Амбассадоры ({len(ambassadors)})", f"certadm_ppl:{KIND_AMBASSADOR}")],
        [("➕ Новый вид", "certadm_kind_new"), ("🔤 Шрифт имени", "certadm_font")],
    ]
    for row in custom:
        rows.append([(row["title"], f"certadm_kind:{row['code']}")])
    rows.append([("👁 Проверить на себе", "certadm_preview")])
    rows.append([("◀️ Назад", "admin_forum_functions")])
    return text, _kb(rows)


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
    variants = await _variants()
    rows = []
    for kind in KINDS:
        for variant in certs.kind_variants(kind, variants):
            mark = "✅" if (kind, variant) in templates else "➕"
            rows.append([(
                f"{mark} {certs.KIND_SHORT[kind]} · {certs.variant_label(variant)}",
                f"certadm_tpl:{kind}:{variant}",
            )])
    for row in await cdb.list_kinds():
        for variant in certs.kind_row_variants(row, variants):
            mark = "✅" if (row["code"], variant) in templates else "➕"
            rows.append([(
                f"{mark} {row['title']} · {certs.variant_label(variant)}",
                f"certadm_tpl:{row['code']}:{variant}",
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


async def _parse_tpl(data: str) -> tuple[str, str] | None:
    parts = data.split(":")
    if parts[3:] == ["go"]:  # `certadm_tpldel:<вид>:<вариант>:go` — подтверждённое удаление
        parts = parts[:3]
    if len(parts) != 3:
        return None
    kind, variant = parts[1], parts[2]
    if variant != VARIANT_ALL and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", variant):
        return None
    if kind in KINDS:
        return kind, variant
    if cdb.kind_id_from_code(kind) and await cdb.get_kind(kind):
        return kind, variant
    return None


@router.callback_query(F.data.startswith("certadm_tpl:"))
async def certadm_tpl(callback: types.CallbackQuery, state: FSMContext):
    parsed = await _parse_tpl(callback.data)
    if parsed is None:
        await callback.answer("Не понял, какой шаблон — откройте список заново.", show_alert=True)
        return
    kind, variant = parsed
    await state.set_state(CertAdmin.template)
    await state.update_data(kind=kind, variant=variant)
    title = f"{await certs.kind_title(kind)} · {certs.variant_label(variant)}"
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
    parsed = await _parse_tpl(callback.data)
    if parsed is None:
        await callback.answer("Не понял, какой шаблон — откройте список заново.", show_alert=True)
        return
    kind, variant = parsed
    if not callback.data.endswith(":go"):
        await callback.message.answer(
            f"Убрать шаблон «{await certs.kind_title(kind)} · {certs.variant_label(variant)}»? Пока не загрузите "
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
    if kind not in KINDS and not (cdb.kind_id_from_code(kind or "") and await cdb.get_kind(kind)):
        await state.clear()
        await message.answer("Не понял, для какого шаблона файл — откройте «📄 Шаблоны» заново.")
        return
    name = await certs.default_name(message.from_user.id, message.from_user.full_name) or _EXAMPLE_NAME
    try:
        pdf = await certs.build_pdf(
            bot, doc.file_id, name, kind,
            await certs.competency_percents(message.from_user.id),
            telegram_id=message.from_user.id,
        )
    except Exception as e:
        logger.error(f"certadm_tpl_file: шаблон не читается: {e}")
        await message.answer("Не получилось открыть этот PDF. Пересохраните его из редактора и пришлите ещё раз.")
        return
    cleared = await cdb.set_template(kind, variant, doc.file_id, message.from_user.id)
    await state.clear()
    label = await certs.kind_title(kind)
    caption = f"✅ Шаблон «{label} · {certs.variant_label(variant)}» сохранён. Вот пример."
    if cleared:
        caption += " Рамки с прошлого файла сняты. Поставьте их заново на сайте."
    await message.answer_document(
        BufferedInputFile(pdf, filename=certs.file_name(kind, name, label)),
        caption=caption,
    )
    await _back_home(message)


@router.message(CertAdmin.template)
async def certadm_tpl_not_file(message: types.Message):
    await message.answer("Жду PDF-файл шаблона. Пришлите его как документ — или нажмите «◀️ Назад».")


# ── волонтёры и амбассадоры ────────────────────────────────────────────────────────────

async def _people_text_kb(kind: str) -> tuple[str, InlineKeyboardMarkup]:
    people = await certs.people_screen(kind)
    lines = []
    rows = []
    for person in people:
        bit = f"@{html.escape(person['username'])}"
        if person["list_name"]:
            bit += f" — {html.escape(person['list_name'])}"
        if person["mark"]:
            bit += f" — {html.escape(person['mark'])}"
        if person["form_surname"]:
            bit += f" (в анкете: {html.escape(person['form_surname'])})"
        lines.append(bit)
        if person["conflict"]:
            rows.append([
                ("Да, это он", f"certadm_yes:{kind}:{person['username']}"),
                ("Убрать", f"certadm_rm:{kind}:{person['username']}"),
            ])
    shown = "\n".join(lines) if lines else "пока никого"
    text = (
        f"{html.escape(await _people_heading(kind))} — <b>{len(people)}</b>\n\n{shown}\n\n"
        "Пришлите людей строками или файлом Excel/CSV.\n"
        "Строка: @nick — Фамилия Имя Отчество. Ник без имени тоже добавляется.\n"
        "В файле две колонки: ник и имя. Пример строки:\n"
        "@ivanov — Иванова Анна\n\n"
        "Файл дополняет список, целиком его не заменяет. "
        "Чтобы убрать одного — пришлите его ник с минусом: -@ivanov"
    )
    if people:
        rows.append([("🗑 Очистить весь список", f"certadm_clr:{kind}")])
    rows.append([("◀️ Назад", "certadm_home")])
    return text, _kb(rows)


def _parse_person_cb(data: str) -> tuple[str, str] | None:
    parts = data.split(":")
    if len(parts) != 3:
        return None
    if parts[1] not in _PEOPLE_TITLES and cdb.kind_id_from_code(parts[1]) is None:
        return None
    if not re.fullmatch(r"[a-z0-9_]{4,32}", parts[2]):
        return None
    return parts[1], parts[2]


@router.callback_query(F.data.startswith("certadm_ppl:"))
async def certadm_ppl(callback: types.CallbackQuery, state: FSMContext):
    kind = callback.data.split(":", 1)[1]
    if not await _people_ok(kind):
        await callback.answer("Не понял, какой список — откройте «🎓 Сертификаты» заново.", show_alert=True)
        return
    await state.set_state(CertAdmin.people)
    await state.update_data(kind=kind)
    text, kb = await _people_text_kb(kind)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


_TABLE_MAX = 2 * 1024 * 1024


def _people_result(added: int, adds: list, remove: list, bad: list[str]) -> str | None:
    """`None` — никого не разобрали, ответ уже со словом «пропустил», если было что пропустить."""
    if not adds and not remove:
        reply = (
            "Не нашёл ни одного ника. Пришлите строку @nick — Фамилия Имя Отчество, "
            "@ники через пробел или файл .xlsx/.csv с колонками «ник» и «имя»."
        )
        if bad:
            reply += "\n\nНе похоже на ник, пропустил: " + ", ".join(html.escape(b) for b in bad[:10])
        return reply
    parts = [f"✅ Добавлено: {added}"]
    if len(adds) > added:
        parts.append(f"уже были в списке: {len(adds) - added}")
    if remove:
        parts.append(f"убрано: {len(remove)}")
    reply = ", ".join(parts) + "."
    if bad:
        reply += "\n\nНе похоже на ник, пропустил: " + ", ".join(html.escape(b) for b in bad[:10])
    return reply


async def _answer_people(message: types.Message, kind: str, reply: str) -> None:
    await message.answer(reply)
    text, kb = await _people_text_kb(kind)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(CertAdmin.people, F.text)
async def certadm_ppl_input(message: types.Message, state: FSMContext):
    kind = (await state.get_data()).get("kind")
    if not await _people_ok(kind):
        await state.clear()
        await message.answer("Не понял, какой список — откройте «🎓 Сертификаты» заново.")
        return
    added, adds, remove, bad = await certs.apply_people_paste(kind, message.text or "")
    reply = _people_result(added, adds, remove, bad)
    if not adds and not remove:
        await message.answer(reply)
        return
    await _answer_people(message, kind, reply)


@router.message(CertAdmin.people, F.document)
async def certadm_ppl_file(message: types.Message, state: FSMContext, bot):
    kind = (await state.get_data()).get("kind")
    if not await _people_ok(kind):
        await state.clear()
        await message.answer("Не понял, какой список — откройте «🎓 Сертификаты» заново.")
        return
    doc = message.document
    if (doc.file_size or 0) > _TABLE_MAX:
        await message.answer("Файл больше 2 МБ. Для списка людей это много — пришлите файл поменьше.")
        return
    buf = await bot.download(doc.file_id)
    raw = buf.read(_TABLE_MAX + 1)
    if len(raw) > _TABLE_MAX:
        await message.answer("Файл больше 2 МБ. Для списка людей это много — пришлите файл поменьше.")
        return
    try:
        adds, bad = certs.people_from_file(raw, doc.file_name or "")
    except certs.PeopleTableError as e:
        await message.answer(str(e))
        return
    added = await certs.apply_people_changes(kind, adds, [])
    reply = _people_result(added, adds, [], bad)
    if not adds:
        await message.answer(reply)
        return
    reply += "\n\nФайл дополнил список. Кто уже был — имя обновилось. Целиком список не заменён."
    await _answer_people(message, kind, reply)


@router.message(CertAdmin.people)
async def certadm_ppl_other(message: types.Message):
    await message.answer(
        "Жду людей строками или файлом .xlsx/.csv. "
        "В таблице — колонка с @ником и колонка с именем."
    )


@router.callback_query(F.data.startswith("certadm_yes:"))
async def certadm_yes(callback: types.CallbackQuery, state: FSMContext):
    parsed = _parse_person_cb(callback.data)
    if parsed is None:
        await callback.answer("Не понял, кого подтвердить — откройте список заново.", show_alert=True)
        return
    kind, username = parsed
    if not await _people_ok(kind):
        await callback.answer("Не понял, какой список — откройте «🎓 Сертификаты» заново.", show_alert=True)
        return
    await cdb.confirm_list_name(kind, username)
    await state.set_state(CertAdmin.people)
    await state.update_data(kind=kind)
    await callback.answer("Да, отправлю это имя")
    text, kb = await _people_text_kb(kind)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("certadm_rm:"))
async def certadm_rm(callback: types.CallbackQuery, state: FSMContext):
    parsed = _parse_person_cb(callback.data)
    if parsed is None:
        await callback.answer("Не понял, кого убрать — откройте список заново.", show_alert=True)
        return
    kind, username = parsed
    if not await _people_ok(kind):
        await callback.answer("Не понял, кого убрать — откройте список заново.", show_alert=True)
        return
    await cdb.remove_person(kind, username)
    await state.set_state(CertAdmin.people)
    await state.update_data(kind=kind)
    await callback.answer("Убрал из списка")
    text, kb = await _people_text_kb(kind)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("certadm_clr:"))
async def certadm_clr(callback: types.CallbackQuery):
    kind = callback.data.split(":", 1)[1]
    if not await _people_ok(kind):
        await callback.answer("Не понял, какой список.", show_alert=True)
        return
    count = len(await cdb.list_people(kind))
    heading = await _people_heading(kind)
    await callback.message.answer(
        f"Удалить всех {count} из списка «{heading}»? Сертификат по этой роли им не придёт, "
        "пока вы не добавите их снова. Уже выданные файлы у людей останутся.",
        reply_markup=_kb([[("🗑 Да, очистить", f"certadm_clrgo:{kind}"), ("Отмена", f"certadm_ppl:{kind}")]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("certadm_clrgo:"))
async def certadm_clrgo(callback: types.CallbackQuery, state: FSMContext):
    kind = callback.data.split(":", 1)[1]
    if not await _people_ok(kind):
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
            pdf = await certs.build_pdf(
                bot, file_id, name, kind,
                await certs.competency_percents(callback.from_user.id),
                telegram_id=callback.from_user.id,
            )
        except Exception as e:
            logger.error(f"certadm_preview {kind}/{variant}: {e}")
            label = await certs.kind_title(kind)
            await callback.message.answer(f"❌ {label} · {certs.variant_label(variant)}: не собрался.")
            continue
        label = await certs.kind_title(kind)
        await callback.message.answer_document(
            BufferedInputFile(pdf, filename=certs.file_name(kind, name, label)),
            caption=f"{label} · {certs.variant_label(variant)}",
        )


# ── свой вид и шрифт имени ────────────────────────────────────────────────────────────

def _font_ok(raw: bytes, filename: str) -> bool:
    name = (filename or "").lower()
    if not name.endswith((".ttf", ".otf")) or not raw or len(raw) > _FONT_MAX:
        return False
    try:
        from PIL import ImageFont
        ImageFont.truetype(io.BytesIO(raw), 32)
    except Exception:
        return False
    return True


def _tick(on: bool, label: str) -> str:
    return f"✅ {label}" if on else label


async def _kind_text_kb(row: dict) -> tuple[str, InlineKeyboardMarkup]:
    code = row["code"]
    audience = row["audience"]
    fields = set(row["fields"])
    day = "Один день: сертификат этого дня" if row["by_day"] else "Один день: шаблон «все дни»"
    was = (
        "Кто был один день, получит сертификат этого дня."
        if row["by_day"] else "Кто был один день, получит шаблон «все дни»."
    )
    text = (
        f"Вид «{html.escape(row['title'])}»\n\n"
        f"Кому: {html.escape(_who(row))}\n"
        f"Поля: {html.escape(_field_words(row))}\n"
        f"{was}\n\n"
        "Кому он положен, какие поля печатать и нужна ли версия на один день — кнопками ниже."
    )
    rows = [
        [(_tick(audience == "checkin", "Отметка на входе"), f"certadm_kind_aud:{code}:checkin"),
         (_tick(audience == "test", "Тест компетенций"), f"certadm_kind_aud:{code}:test")],
        [(_tick(audience == "nicks", "Список ников"), f"certadm_kind_aud:{code}:nicks"),
         (_tick(audience == "role", "Роль"), f"certadm_kind_role:{code}")],
        [(_tick("name" in fields, "Имя"), f"certadm_kind_fld:{code}:name"),
         (_tick("date" in fields, "Дата"), f"certadm_kind_fld:{code}:date")],
        [(_tick("city" in fields, "Город"), f"certadm_kind_fld:{code}:city"),
         (_tick("scales" in fields, "Шкалы"), f"certadm_kind_fld:{code}:scales")],
        [(day, f"certadm_kind_day:{code}")],
    ]
    if audience == "nicks":
        rows.append([("Список @ников", f"certadm_ppl:{code}")])
    rows.append([("Убрать этот вид", f"certadm_kind_del:{code}")])
    rows.append([("Готово", "certadm_home")])
    return text, _kb(rows)


async def _answer_kind(message: types.Message, row: dict) -> None:
    text, kb = await _kind_text_kb(row)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "certadm_kind_new")
async def certadm_kind_new(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(CertAdmin.kind_title)
    await callback.message.answer(
        f"Как назвать вид?\n\n{_TITLE_HINT}",
        reply_markup=_kb([[("◀️ Отмена", "certadm_home")]]),
    )
    await callback.answer()


@router.message(CertAdmin.kind_title, F.text)
async def certadm_kind_title(message: types.Message, state: FSMContext):
    title = (message.text or "").strip()
    if len(title) < 2 or len(title) > 40:
        await message.answer(_TITLE_HINT)
        return
    # Пока «кому» не выбрано, вид никого не награждает: пустой список ников.
    row = await cdb.insert_kind(title, "nicks")
    await state.clear()
    await _answer_kind(message, row)


@router.message(CertAdmin.kind_title)
async def certadm_kind_title_other(message: types.Message):
    await message.answer(_TITLE_HINT)


@router.callback_query(F.data.startswith("certadm_kind_aud:"))
async def certadm_kind_aud(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[2] not in ("checkin", "test", "nicks"):
        await callback.answer("Не понял, кому положен вид — откройте его заново.", show_alert=True)
        return
    row = await cdb.set_kind_audience(parts[1], parts[2])
    if row is None:
        await callback.answer("Не понял, какой вид — откройте список заново.", show_alert=True)
        return
    await _answer_kind(callback.message, row)
    await callback.answer()


@router.callback_query(F.data.startswith("certadm_kind_role:"))
async def certadm_kind_role(callback: types.CallbackQuery):
    rest = callback.data.split(":", 1)[1]
    parts = rest.split(":")
    code = parts[0]
    if await cdb.get_kind(code) is None:
        await callback.answer("Не понял, какой вид — откройте список заново.", show_alert=True)
        return
    if len(parts) == 1:
        from services.access.role_registry import ROLES
        rows = [[(meta["label"], f"certadm_kind_role:{code}:{key}")] for key, meta in ROLES.items()]
        rows.append([("◀️ Назад", f"certadm_kind:{code}")])
        await callback.message.answer("Какая роль получает этот сертификат?", reply_markup=_kb(rows))
        await callback.answer()
        return
    row = await cdb.set_kind_audience(code, "role", role_key=parts[1])
    if row is None:
        await callback.answer("Не понял, какую роль — откройте список заново.", show_alert=True)
        return
    await _answer_kind(callback.message, row)
    await callback.answer()


@router.callback_query(F.data.startswith("certadm_kind_fld:"))
async def certadm_kind_fld(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[2] not in cdb.KIND_FIELDS:
        await callback.answer("Не понял, какое поле — откройте вид заново.", show_alert=True)
        return
    if parts[2] == "name":
        await callback.answer("Имя нужно: без него печатать нечего.", show_alert=True)
        return
    row = await cdb.toggle_kind_field(parts[1], parts[2])
    if row is None:
        await callback.answer("Не понял, какой вид — откройте список заново.", show_alert=True)
        return
    await _answer_kind(callback.message, row)
    await callback.answer()


@router.callback_query(F.data.startswith("certadm_kind_day:"))
async def certadm_kind_day(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    row = await cdb.toggle_kind_day(code)
    if row is None:
        await callback.answer("Не понял, какой вид — откройте список заново.", show_alert=True)
        return
    await _answer_kind(callback.message, row)
    await callback.answer()


@router.callback_query(F.data.startswith("certadm_kind_del:"))
async def certadm_kind_del(callback: types.CallbackQuery, state: FSMContext):
    raw = callback.data.split(":", 1)[1]
    go = raw.endswith(":go")
    code = raw[:-3] if go else raw
    if code in KINDS or await cdb.get_kind(code) is None:
        await callback.answer("Этот вид убрать нельзя.", show_alert=True)
        return
    row = await cdb.get_kind(code)
    if not go:
        await callback.message.answer(
            f"Убрать вид «{row['title']}»? Пропадут его шаблоны и список ников. "
            "Файлы, которые люди уже получили, останутся у них.",
            reply_markup=_kb([[
                ("Да, убрать", f"certadm_kind_del:{code}:go"),
                ("Отмена", f"certadm_kind:{code}"),
            ]]),
        )
        await callback.answer()
        return
    await cdb.delete_kind(code)
    await state.clear()
    await callback.answer("Вид убран")
    await _back_home(callback.message)


@router.callback_query(F.data.startswith("certadm_kind:"))
async def certadm_kind_open(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":", 1)[1]
    row = await cdb.get_kind(code)
    if row is None:
        await callback.answer("Не понял, какой вид — откройте список заново.", show_alert=True)
        return
    await state.clear()
    await _answer_kind(callback.message, row)
    await callback.answer()


@router.callback_query(F.data == "certadm_font")
async def certadm_font(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(CertAdmin.font)
    rows = []
    if await cdb.get_font_file_id():
        rows.append([("Убрать свой шрифт", "certadm_font_clear")])
    rows.append([("◀️ Отмена", "certadm_home")])
    await callback.message.answer(
        "Пришлите файл шрифта .ttf или .otf. Им будет написано имя на всех сертификатах.",
        reply_markup=_kb(rows),
    )
    await callback.answer()


@router.callback_query(F.data == "certadm_font_clear")
async def certadm_font_clear(callback: types.CallbackQuery, state: FSMContext):
    await cdb.set_font_file_id(None)
    await state.clear()
    await callback.answer("Снова шрифт сборки")
    await _back_home(callback.message)


@router.message(CertAdmin.font, F.document)
async def certadm_font_file(message: types.Message, state: FSMContext, bot):
    doc = message.document
    if (doc.file_size or 0) > _FONT_MAX:
        await message.answer(_FONT_HINT)
        return
    buf = io.BytesIO()
    await bot.download(doc.file_id, destination=buf)
    if not _font_ok(buf.getvalue(), doc.file_name or ""):
        await message.answer(_FONT_HINT)
        return
    await cdb.set_font_file_id(doc.file_id)
    await state.clear()
    await message.answer("✅ Шрифт имени сохранён. Им пишутся все сертификаты.")
    await _back_home(message)


@router.message(CertAdmin.font)
async def certadm_font_other(message: types.Message):
    await message.answer(_FONT_HINT)
