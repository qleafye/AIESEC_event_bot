"""Phase 28 (28-05, SU-04, СкиллАп 5): развилка резюме R1/R2a/R2b/R2c в чате бота.

Расширение существующего состояния `Registration.resume` (A-03 CONTEXT), НЕ новый узел графа
FSM: R1 — три инлайн-кнопки выбора способа (`regfork:file|link|mini`), R2a — существующий
приём документа/текста (`handlers/reg_flow.py::process_resume*`, не тронут), R2b — приём
ссылки (этот модуль), R2c — три текстовых мини-подшага (`handlers/reg_extra_steps.ask_step`,
показ уже готов планом 28-02, здесь только точка входа с R1).

Своего `Router` НЕТ — импортирует и декорирует напрямую `router`, определённый в
`handlers/registration.py` (13-02 приём). Импортируется В ХВОСТЕ `registration.py`, ПОСЛЕ
`reg_extra_steps` (которая сама идёт после `reg_handoff`) — обработчики этого шва регистрируются
последними, золотой снимок порядка (`tests/test_refac_snapshot_260816.py`) только дополняется.

«Назад» — ЕДИНСТВЕННЫЙ шаг мастера, где кнопка «Назад» ведёт не на предыдущий вопрос анкеты,
а на экран развилки (R1), см. `back_to_fork()`. Закрытый словарь inline-токенов —
`regfork:file`/`regfork:link`/`regfork:mini`/`regfork:back` (T-28-05-01) — фильтр
`Registration.resume` намеренно узкий: R1 и R2a (файл) остаются в этом ЖЕ состоянии, поэтому
один callback-хендлер обслуживает обе точки входа/выхода. Текстовые подшаги (R2b/R2c) НЕ
получают инлайн-«Назад» — они получают reply-кнопку `BACK_LABEL`, добавленную в клавиатуру
`handlers/reg_extra_steps.ask_step()`/`_receive_step()` (см. докстринг там, deviation Rule 3):
это ЕДИНСТВЕННОЕ место показа/приёма этих четырёх шагов независимо от точки входа, поэтому
«Назад» логически обязан жить там, а не здесь.
"""
from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import reg_extra_steps
from handlers.registration import _advance, _progress, _safe_answer, _sync_draft_out, router
from handlers.states import Registration
from handlers import reg_i18n
from settings_schema import get_setting_typed
from reg_engine import (
    help_text, prompt, resume_fork_options, resume_link_whitelist,
    validate_answer, validate_resume_link,
)

# Reply-кнопка «Назад» на текстовых подшагах (resume_link/mini_projects/mini_portfolio/
# mini_direction) — литерал ДОСЛОВНО совпадает с `handlers.reg_extra_steps._FORK_BACK_LABEL`
# (сверено, не общий импорт — тот же приём дублирования служебных литералов, что «Пропустить»/
# «Отмена» в проекте).
BACK_LABEL = "⬅️ Назад"

_FORK_TOKENS = ("file", "link", "mini")


async def ask_fork(message: types.Message, state: FSMContext, progress_prefix: str,
                    participant_type: str | None, city_code: str | None) -> None:
    """R1 (28-UI-SPEC.md §1): три равноправные кнопки-ветки, ни одна не выделена (это точка
    ветвления, не рекомендация — Color «Кнопки .choice-stack» 28-UI-SPEC). Текст — тот же
    реестровый `reg_prompt_resume`, что и в режиме `file_or_text` (движок сам решает дефолт,
    28-04); подписи кнопок — `reg_engine.resume_fork_options()` (реестр, D-01/D-02: код кнопки
    делегату не показывается)."""
    text = f"{progress_prefix}{await prompt('resume', participant_type, city_code)}"
    fork_opts = await resume_fork_options()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=opt["label"], callback_data=f"regfork:{opt['code']}")]
        for opt in fork_opts
    ])
    await _safe_answer(message, text, reply_markup=kb)
    await state.set_state(Registration.resume)


async def back_to_fork(message: types.Message, state: FSMContext) -> None:
    """Единая точка «Назад» из ЛЮБОЙ ветки развилки (A-03 CONTEXT) — единственный шаг мастера,
    где «Назад» ведёт не на предыдущий вопрос анкеты, а на экран выбора способа (R1). Сбрасывает
    `resume_type` (подвисший выбор ветки не должен просачиваться в `enabled_steps` на возврате)
    и пересчитывает префикс прогресса из уже сохранённых `_reg_step`/`_reg_total` (та же пара,
    что использует `_advance`/`_ask_step_or_recall` в `handlers/registration.py`).

    Quick 260910-wb6: сброс синхронизируется и в общий черновик (`_sync_draft_out`, шаг
    `step_key=None` — `upsert_reg_draft` не трогает `step` через `COALESCE`) — иначе отменённая
    ветка воскресала бы при продолжении анкеты из черновика после рестарта (черновик так и не
    узнавал, что делегат нажал «Назад» на развилке)."""
    data = await state.get_data()
    await state.update_data(resume_type=None)
    data["resume_type"] = None
    await _sync_draft_out(message.chat.id, state, data, None, answered_col=("resume_type",))
    participant_type = data.get("participant_type") or "full"
    city_code = data.get("event_city")
    progress_prefix = await _progress(data.get("_reg_step", 1), data.get("_reg_total", 1))
    await ask_fork(message, state, progress_prefix, participant_type, city_code)


async def _ask_file_branch(message: types.Message, state: FSMContext, progress_prefix: str,
                            participant_type: str | None, city_code: str | None) -> None:
    """R2a — существующий приём документа/текста (`handlers/reg_flow.py::process_resume*`)
    не меняется байт-в-байт; здесь только показывается инлайн «⬅️ Назад» (T-28-05-01: тот же
    закрытый токен `regfork:back`, ловится тем же callback-хендлером ниже — R2a остаётся в
    состоянии `Registration.resume`, не заводит своего)."""
    text = f"{progress_prefix}{await help_text('resume', participant_type, city_code) or await prompt('resume', participant_type, city_code)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=BACK_LABEL, callback_data="regfork:back"),
    ]])
    await _safe_answer(message, text, reply_markup=kb)
    await state.set_state(Registration.resume)


@router.callback_query(F.data.startswith("regfork:"), Registration.resume)
async def regfork_pick(callback: types.CallbackQuery, state: FSMContext):
    """R1/R2a — единственная inline-клавиатура закрытого словаря `regfork:*` (T-28-05-01):
    фильтр по состоянию `Registration.resume` НАРОЧНО узкий (R1 и R2a — оба в этом состоянии,
    текстовые подшаги R2b/R2c используют reply-кнопку, не этот callback, см. докстринг модуля).
    `file`/`link`/`mini` кладут `resume_type` в FSM и ведут в свою ветку; `back` (доступен и
    из R2a — та же клавиатура) сбрасывает выбор и возвращает на R1.

    Quick 260910-wb6: выбор ветки синхронизируется в общий черновик В МОМЕНТ ТАПА
    (`_sync_draft_out`), а не на шаге — ветки «ссылка»/«мини-профиль» уходят в свои шаги и
    через `_advance("resume")` никогда не проходят, так что `resume_type` иначе терялся бы при
    продолжении анкеты из черновика после рестарта (не только живой FSM-сессии)."""
    raw = callback.data or ""
    token = raw.split(":", 1)[1] if ":" in raw else ""
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})

    if token == "back":
        await back_to_fork(tap_message, state)
        return
    if token not in _FORK_TOKENS:
        # Закрытый словарь (T-28-05-01) — незнакомый токен молча игнорируется, клавиатура уже
        # погашена выше, анкету колом не вешает.
        return

    await state.update_data(resume_type=token)
    data = await state.get_data()
    await _sync_draft_out(tap_message.chat.id, state, data, None, answered_col=("resume_type",))
    participant_type = data.get("participant_type") or "full"
    city_code = data.get("event_city")
    p = await _progress(data.get("_reg_step", 1), data.get("_reg_total", 1))

    if token == "file":
        await _ask_file_branch(tap_message, state, p, participant_type, city_code)
    elif token == "link":
        await reg_extra_steps.ask_step("resume_link", tap_message, state, p, participant_type, city_code)
    else:  # "mini"
        await reg_extra_steps.ask_step("mini_projects", tap_message, state, p, participant_type, city_code)


@router.message(Registration.resume_link)
async def process_resume_link(message: types.Message, state: FSMContext, bot):
    """R2b — приём ссылки (SU-04). `validate_answer` уже проверяет и схему `http(s)://`, и
    длину (T-28-04-04); `verified` достаём отдельно тем же `validate_resume_link` (значение
    ИНФОРМАЦИОННОЕ — сервер пересчитывает его заново на финале и никогда не читает клиентское,
    T-28-04-01/T-28-05-02 — здесь только для паритета с тем, что кладёт в `reg_drafts.answers`
    PATCH из Mini App)."""
    if message.text == BACK_LABEL:
        await back_to_fork(message, state)
        return
    whitelist = await resume_link_whitelist()
    value, err = validate_answer("resume_link", message.text, whitelist=whitelist)
    if err:
        text = await get_setting_typed("reg_resume_link_invalid_text") or err
        await reg_i18n.say(message, text)
        return
    _, verified, _ = validate_resume_link(message.text, whitelist)
    await state.update_data(resume_link=value, link_verified=verified)
    await _advance("resume_link", message, state, bot)
