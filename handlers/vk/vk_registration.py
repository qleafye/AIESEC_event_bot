"""Анкета ВК поверх того же движка, что анкета Telegram.

Согласия не спрашиваем: их таблица ключуется telegram_id, а у этой карточки его нет.
Вилка вечеринки спрашивается, только если модуль её включил. Город решает city_gate.
"""
from __future__ import annotations

import logging

from database.db import create_vk_user, get_vk_user, update_vk_user
from domain.regform.engine import (
    DRAFT_ONLY_COLUMNS,
    REG_STEP_TYPES,
    STEP_TO_COLUMN,
    apply_answer,
    city_fork_options,
    city_gate,
    columns_for_step,
    enabled_steps,
    options,
    party_track_options,
    pre_flow,
    prompt,
    studying_statuses,
    validate_answer,
    validate_city_choice,
    validate_track_choice,
)
from domain.settings.schema import get_setting_typed
from keyboards.vk_builders import vk_main_menu_keyboard
from services.infra.timeutil import msk_now
from services.vk.vk_api_client import send_vk_message
from services.vk.vk_fsm import (
    clear_vk_fsm_state,
    get_vk_fsm_data,
    get_vk_fsm_state,
    set_vk_fsm_data,
    set_vk_fsm_state,
)

logger = logging.getLogger(__name__)

_DONE = "Анкета отправлена."
_START_HINT = "Напишите /start, чтобы заполнить анкету."
_ALREADY = "У вас уже есть анкета."
_CITY_HINT = "Напишите город из списка."
_TRACK_HINT = "Напишите формат участия из списка."
_MULTI_HINT = "Напишите один или несколько вариантов через запятую."
_GATES = frozenset({"city_fork", "party_fork"})


async def start_vk_registration(vk_id: int) -> None:
    """Начать или продолжить анкету. Сданная анкета второй раз не открывается."""
    user = await get_vk_user(vk_id)
    if user and user.get("status") in ("approved", "pending"):
        await send_vk_message(vk_id, _ALREADY)
        return
    data = await get_vk_fsm_data(vk_id)
    answers = dict(data.get("answers") or {})
    state = await get_vk_fsm_state(vk_id)
    if state in _GATES or state:
        await _ask_current(vk_id, state, answers)
        return
    await _begin(vk_id, answers)


async def process_vk_registration_step(vk_id: int, text: str) -> None:
    """Ответ на текущий вопрос. Пустое состояние просит начать с /start."""
    state = await get_vk_fsm_state(vk_id)
    if not state:
        await send_vk_message(vk_id, _START_HINT)
        return
    data = await get_vk_fsm_data(vk_id)
    answers = dict(data.get("answers") or {})
    text = (text or "").strip()
    if state == "city_fork":
        await _take_city(vk_id, text, answers)
        return
    if state == "party_fork":
        await _take_track(vk_id, text, answers)
        return
    raw = text
    if REG_STEP_TYPES.get(state) == "multi":
        raw = [part.strip() for part in text.split(",") if part.strip()]
    value, error = validate_answer(state, raw)
    if error:
        await send_vk_message(vk_id, error)
        return
    statuses = await studying_statuses(answers.get("event_city"))
    answers = apply_answer(answers, state, value, studying_statuses=statuses)
    await _save(vk_id, answers)
    await _ask_next(vk_id, answers)


async def _begin(vk_id: int, answers: dict) -> None:
    kind, code = await city_gate(answers.get("event_city"))
    if kind in ("closed", "all_closed"):
        await send_vk_message(vk_id, await _closed_text(kind, code))
        return
    if kind == "fork":
        await _remember(vk_id, "city_fork", answers)
        await _ask_city(vk_id)
        return
    if code and not answers.get("event_city"):
        answers["event_city"] = code
        await _save(vk_id, answers)
    await _after_city(vk_id, answers)


async def _after_city(vk_id: int, answers: dict) -> None:
    screens = await pre_flow(answers, {
        "event_city": answers.get("event_city"),
        "is_registered": False,
        "party_track": answers.get("participant_type"),
    })
    if "party_fork" in screens and not answers.get("participant_type"):
        await _remember(vk_id, "party_fork", answers)
        await _ask_track(vk_id)
        return
    await _ask_next(vk_id, answers)


async def _take_city(vk_id: int, text: str, answers: dict) -> None:
    code = await _match_choice(text, await city_fork_options())
    if code is None:
        await send_vk_message(vk_id, _CITY_HINT)
        await _ask_city(vk_id)
        return
    chosen, error = await validate_city_choice(code)
    if error or not chosen:
        await send_vk_message(vk_id, error or _CITY_HINT)
        return
    answers["event_city"] = chosen
    await _save(vk_id, answers)
    await _after_city(vk_id, answers)


async def _take_track(vk_id: int, text: str, answers: dict) -> None:
    code = await _match_choice(text, await party_track_options())
    if code is None:
        await send_vk_message(vk_id, _TRACK_HINT)
        await _ask_track(vk_id)
        return
    track, error = await validate_track_choice(code, answers.get("event_city"))
    if error or not track:
        await send_vk_message(vk_id, error or _TRACK_HINT)
        return
    answers["participant_type"] = track
    await _save(vk_id, answers)
    await _ask_next(vk_id, answers)


async def _ask_next(vk_id: int, answers: dict) -> None:
    step = await _open_step(answers)
    if step is None:
        await _finalize(vk_id, answers)
        return
    await _remember(vk_id, step, answers)
    await _send_question(vk_id, step, answers)


async def _ask_current(vk_id: int, state: str, answers: dict) -> None:
    if state == "city_fork":
        await _ask_city(vk_id)
        return
    if state == "party_fork":
        await _ask_track(vk_id)
        return
    await _send_question(vk_id, state, answers)


async def _open_step(answers: dict) -> str | None:
    if not str(answers.get("full_name") or "").strip():
        return "full_name"
    enabled = await enabled_steps(answers, answers.get("event_city"))
    for step in enabled:
        columns = [c for c in (columns_for_step(step) or [STEP_TO_COLUMN.get(step, step)])
                   if c not in DRAFT_ONLY_COLUMNS]
        if not any(answers.get(column) not in (None, "") for column in columns):
            return step
    return None


async def _send_question(vk_id: int, step: str, answers: dict) -> None:
    city = answers.get("event_city")
    text = await prompt(step, answers.get("participant_type"), city)
    choice = await options(step, city)
    lines = [text]
    if REG_STEP_TYPES.get(step) == "multi":
        lines.append(_MULTI_HINT)
    lines.extend(choice)
    await send_vk_message(vk_id, "\n".join(lines))


async def _ask_city(vk_id: int) -> None:
    text = await get_setting_typed("city_fork_text") or "Напишите город."
    labels = [item["label"] for item in await city_fork_options()]
    await send_vk_message(vk_id, "\n".join([text, *labels]))


async def _ask_track(vk_id: int) -> None:
    text = await get_setting_typed("party_fork_question_text") or "Какой формат участия?"
    labels = [item["label"] for item in await party_track_options()]
    await send_vk_message(vk_id, "\n".join([text, *labels]))


async def _closed_text(kind: str, code: str | None) -> str:
    key = "city_reg_all_closed_text" if kind == "all_closed" else "city_reg_closed_text"
    text = await get_setting_typed(key) or "Регистрация закрыта."
    if code and "{city}" in text:
        from domain.cities import city_label
        text = text.replace("{city}", await city_label(code))
    return text


async def _finalize(vk_id: int, answers: dict) -> None:
    answers = dict(answers)
    answers["status"] = "pending"
    answers["registration_date"] = msk_now().strftime("%Y-%m-%d %H:%M:%S")
    season = await get_setting_typed("event_season")
    if season:
        answers["season"] = str(season)
    await _save(vk_id, answers)
    await clear_vk_fsm_state(vk_id)
    keyboard = await vk_main_menu_keyboard(vk_id)
    await send_vk_message(vk_id, _DONE, keyboard)
    logger.info("VK registration submitted for %s", vk_id)


async def _save(vk_id: int, answers: dict) -> None:
    await _remember(vk_id, await get_vk_fsm_state(vk_id) or "", answers)
    if await get_vk_user(vk_id) is None:
        await create_vk_user(vk_id, answers)
        return
    await update_vk_user(vk_id, answers)


async def _remember(vk_id: int, state: str, answers: dict) -> None:
    await set_vk_fsm_data(vk_id, {"answers": answers})
    if state:
        await set_vk_fsm_state(vk_id, state)


async def _match_choice(text: str, choices: list[dict]) -> str | None:
    folded = text.casefold()
    for item in choices:
        label = str(item.get("label") or "")
        code = str(item.get("code") or "")
        if folded and folded in (label.casefold(), code.casefold()):
            return code
    return None
