"""Сертификат после форума, сторона делегата: кнопка «🎓 Получить сертификат» и /certificate.

INVARIANT: каждый @router.* — декоратор в ОДНУ строку (golden-снапшот роутеров читает их построчно).

Поток: нажал кнопку → бот показывает, как будет написано имя → «✅ Да, прислать» или
«✏️ Исправить» → PDF каждого положенного сертификата отдельным файлом. Имя запоминается:
повторное нажатие сразу предлагает исправленное.

Модуль импортируется хвостом `handlers/user_actions.py` до фолбэка `reg_handoff_idle_fallback`,
как соседние швы форума. Колбэки `cert_*` не админские — в ADMIN_CAPS их нет."""
from __future__ import annotations

import html
import logging

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from database import certificates_db as cdb
from handlers.states import CertificateName
from handlers.user_actions import router
from services.forum import certificates as certs

logger = logging.getLogger(__name__)

_NOT_ENTITLED = (
    "Не нашёл тебя среди отмеченных на входе форума. Сертификат участника получают те, кого "
    "отметили по QR.\n\nЕсли ты был на форуме — напиши организаторам через «❓ Задать вопрос», "
    "проверим и добавим."
)


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, прислать", callback_data="cert_ok")],
        [InlineKeyboardButton(text="✏️ Исправить имя", callback_data="cert_edit")],
    ])


async def _closed_text() -> str:
    when = certs.human_open_at(await cdb.get_open_at())
    if when:
        return f"🎓 Сертификаты будут доступны {when} (по Москве). Бот напомнит, когда всё будет готово."
    return "🎓 Сертификатов пока нет — бот напишет, когда они будут готовы."


async def _start(user: types.User, answer) -> None:
    if not await certs.is_open():
        await answer(await _closed_text())
        return
    if not await certs.entitlements(user.id, user.username):
        await answer(_NOT_ENTITLED)
        return
    name = await certs.default_name(user.id, user.full_name)
    await answer(
        "Так будет написано в сертификате:\n\n"
        f"<b>{html.escape(name)}</b>\n\nВсё верно?",
        reply_markup=_confirm_kb(), parse_mode="HTML",
    )


@router.message(Command("certificate"))
async def certificate_command(message: types.Message, state: FSMContext):
    await state.clear()
    await _start(message.from_user, message.answer)


@router.callback_query(F.data == "cert_get")
async def certificate_get(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await _start(callback.from_user, callback.message.answer)
    await callback.answer()


@router.callback_query(F.data == "cert_edit")
async def certificate_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(CertificateName.waiting)
    await callback.message.answer(
        "Напиши имя так, как оно должно быть в сертификате.\n\nНапример: Анна Иванова"
    )
    await callback.answer()


@router.message(StateFilter(CertificateName.waiting), F.text)
async def certificate_name_input(message: types.Message, state: FSMContext):
    name = certs.clean_name(message.text)
    if name is None:
        await message.answer(
            f"Не получилось: имя должно быть одной строкой, от 2 до {certs.NAME_MAX} символов, "
            "без ссылок и @. Напиши ещё раз, например: Анна Иванова"
        )
        return
    await cdb.set_name(message.from_user.id, name)
    await state.clear()
    await _start(message.from_user, message.answer)


@router.callback_query(F.data == "cert_ok")
async def certificate_send(callback: types.CallbackQuery, bot, state: FSMContext):
    await state.clear()  # «✏️ Исправить», а потом «✅ Да» на старом сообщении — имя больше не ждём
    user = callback.from_user
    if not await certs.is_open():
        await callback.answer()
        await callback.message.answer(await _closed_text())
        return
    entitled = await certs.entitlements(user.id, user.username)
    if not entitled:
        await callback.answer()
        await callback.message.answer(_NOT_ENTITLED)
        return
    await callback.answer("Собираю сертификат…")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    name = await certs.default_name(user.id, user.full_name)
    await cdb.set_name(user.id, name)
    templates = await cdb.list_templates()
    sent = 0
    for kind, variant in entitled:
        file_id = certs.pick_template(templates, kind, variant)
        if file_id is None:
            logger.error(f"certificate_send: нет шаблона {kind}/{variant} для {user.id}")
            continue
        try:
            percents = await certs.competency_percents(user.id) if kind == cdb.KIND_COMPETENCY else None
            pdf = await certs.build_pdf(bot, file_id, name, kind, percents)
            await callback.message.answer_document(
                BufferedInputFile(pdf, filename=certs.file_name(kind, name)),
                caption=f"🎓 {certs.KIND_TITLES[kind]}",
            )
            await cdb.record_send(user.id, kind)
            sent += 1
        except Exception as e:
            logger.error(f"certificate_send {user.id} {kind}: {e}")
    if sent == 0:
        await callback.message.answer(
            "Не получилось собрать сертификат. Попробуй ещё раз чуть позже командой /certificate, "
            "а если не выйдет — напиши организаторам через «❓ Задать вопрос»."
        )
    elif sent < len(entitled):
        await callback.message.answer(
            "Часть сертификатов пока не готова — попробуй позже командой /certificate."
        )
