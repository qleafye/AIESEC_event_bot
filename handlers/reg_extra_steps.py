"""Phase 28 (28-02, SU-01/SU-04, СкиллАп 5): шов «спроси любой шаг без своей ветки».

Почему этот шов вообще существует: `handlers/registration.py` стоит РОВНО на потолке
размера (`tests/test_module_size_convention_260816.py`) — ещё одна собственная `elif`-ветка
на каждый новый текстовый шаг вернула бы агрегатор обратно к god-файлу. Вместо этого
`_ask_step` (registration.py) получил ОДИН общий хвост-делегат: шаг без собственной ветки
(generic date/select/multi уже покрыты выше) уходит в `ask_step()` этого модуля —
универсальный показ по реестру, без единого литерала текста. Сегодня это пять новых шагов
СкиллАпа (`resume_link`/`mini_projects`/`mini_portfolio`/`mini_direction`/`case_optin`), но
общий хвост закрывает этот класс отказов навсегда — и для будущих шагов тоже.

Своего `Router()` НЕТ — импортирует и декорирует напрямую `router`, определённый в
`handlers/registration.py` (13-02 приём). Импортируется В ХВОСТЕ `registration.py`, ПОСЛЕ
`reg_handoff` — обработчики регистрируются последними, золотой снимок порядка
(`tests/test_refac_snapshot_260816.py`) только дополняется, ничего не переставляется.

Обработчики ответа ниже — тот же контур «канонизировать -> validate_answer -> сохранить под
своей колонкой -> _advance», что образец шва `handlers/reg_steps.py`. `resume_link` показ
получает (через `ask_step` выше), но СВОЕГО ОБРАБОТЧИКА НЕ ПОЛУЧАЕТ — валидация ссылки и
«Назад» на развилку резюме принадлежат плану 28-04.
"""
import logging

from aiogram import Bot, types
from aiogram.fsm.context import FSMContext

from cities import get_setting_typed_for_city
from handlers import reg_i18n
from handlers.registration import _advance, _safe_answer, router
from handlers.states import Registration
from keyboards.builders import get_cancel_kb, get_skip_kb, get_yes_no_kb
from reg_engine import STEP_TO_COLUMN, _SKIP_ALLOWED_STEPS, prompt, validate_answer

logger = logging.getLogger(__name__)

# Шаги, у которых бот показывает пояснение менеджера ПЕРЕД клавиатурой «Да»/«Нет»
# (28-UI-SPEC.md §5) — сегодня только кейс-чемпионат, ключ per_city (settings_schema.py, 28-01).
_STEP_DESCRIPTION_SETTING = {
    "case_optin": "reg_case_optin_description_text",
}


async def ask_step(step_key: str, message: types.Message, state: FSMContext,
                    progress_prefix: str, participant_type: str | None,
                    city_code: str | None) -> None:
    """Универсальный показ шага без собственной ветки в `_ask_step` — полностью по реестру
    (`reg_engine.prompt`/`_SKIP_ALLOWED_STEPS`), без единого литерала текста.

    Fail-soft (T-28-02-01): шага без объявленного `State` в `Registration` быть не может (все
    пять заведены в этом же плане), но опечатка в реестре не должна вешать анкету колом —
    отсутствие логируется, и шаг молча пропускается через `_advance` (следующий вопрос вместо
    вечного «висит без ответа»)."""
    text = f"{progress_prefix}{await prompt(step_key, participant_type, city_code)}"
    if step_key in _STEP_DESCRIPTION_SETTING:
        description = await get_setting_typed_for_city(_STEP_DESCRIPTION_SETTING[step_key], city_code)
        if description:
            text = f"{text}\n\n{description}"
        kb = get_yes_no_kb()
    elif step_key in _SKIP_ALLOWED_STEPS:
        kb = get_skip_kb()
    else:
        kb = get_cancel_kb()

    target_state = getattr(Registration, step_key, None)
    if target_state is None:
        logger.error(
            f"reg_extra_steps.ask_step: нет состояния Registration.{step_key} — шаг пропущен"
        )
        await _advance(step_key, message, state, bot=None)
        return

    await _safe_answer(message, text, reply_markup=kb)
    await state.set_state(target_state)


async def _receive_step(step_key: str, message: types.Message, state: FSMContext, bot: Bot) -> None:
    """Общий контур приёма для четырёх шагов ниже: канонизировать -> `validate_answer` ->
    сохранить под своей колонкой (`STEP_TO_COLUMN`) -> `_advance`. `case_optin` — жёсткие
    «Да»/«Нет» через `reg_engine._MEMBERSHIP_STEPS` (валидатор сам объясняет, что нажать);
    `mini_portfolio` — «Пропустить» уже понимает `validate_answer` (шаг в
    `_SKIP_ALLOWED_STEPS`, пустое значение станет «-»)."""
    canon = await reg_i18n.canonicalize(message, step_key, message.text)
    value, err = validate_answer(step_key, canon)
    if err:
        await reg_i18n.say(message, err)
        return
    await state.update_data(**{STEP_TO_COLUMN.get(step_key, step_key): value})
    await _advance(step_key, message, state, bot)


@router.message(Registration.mini_projects)
async def process_mini_projects(message: types.Message, state: FSMContext, bot: Bot):
    await _receive_step("mini_projects", message, state, bot)


@router.message(Registration.mini_portfolio)
async def process_mini_portfolio(message: types.Message, state: FSMContext, bot: Bot):
    await _receive_step("mini_portfolio", message, state, bot)


@router.message(Registration.mini_direction)
async def process_mini_direction(message: types.Message, state: FSMContext, bot: Bot):
    await _receive_step("mini_direction", message, state, bot)


@router.message(Registration.case_optin)
async def process_case_optin(message: types.Message, state: FSMContext, bot: Bot):
    await _receive_step("case_optin", message, state, bot)
