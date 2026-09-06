"""Phase 27 (27-05, Задача 3, LANG-06) — канонизация ввода в пяти точках приёма ответа чата
(`reg_steps._thin_step`/`_store_choice`/`process_education_status`/`process_work_status`,
`reg_flow.process_ambassador`) и служебные слова в фильтрах aiogram (`CANCEL_WORDS`/
`CONFIRM_WORDS`/`EDIT_WORDS`, выведены из `i18n_ui_en.UI_EN` — план 27-02/27-05).

Два независимых слоя тестов:
1. **Роутинг фильтров** — `HandlerObject.check(event, raw_state=...)` на РЕАЛЬНЫХ объектах
   `registration.router.message.handlers` (тот же метод, которым aiogram Dispatcher реально
   резолвит first-match) — не reimplementация фильтра, а прогон настоящего зарегистрированного
   хендлера. `raw_state` — то же, что `StateFilter`/`State.__call__` принимают напрямую (см.
   aiogram.filters.StateFilter.__call__), без надобности в живом FSMContext/Storage.
2. **Канонизация ответа** — вызов хендлеров напрямую (Fake message + `FSMContext(MemoryStorage)`,
   тот же приём, что `tests/test_registration_send_guard_260816.py`), `reg_i18n.ctx_for`
   монкипатчится на фиксированный `(lang, tr_map)` — тот же приём, что
   `tests/test_i18n_bot_render_27.py`.

pytest-asyncio в этом окружении нет — только `asyncio.run()`.
"""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import reg_flow  # noqa: F401 -- регистрирует хендлеры на registration.router
from handlers import reg_steps  # noqa: F401 -- регистрирует хендлеры на registration.router
from handlers import reg_i18n
from handlers import registration as reg
from i18n_ui_en import CANCEL_WORDS, CONFIRM_WORDS, EDIT_WORDS

UID = 830001


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    def __init__(self, text, chat_id=UID):
        self.text = text
        self.chat = _FakeChat(chat_id)
        self.calls = []

    async def answer(self, text=None, **kwargs):
        self.calls.append((text, kwargs))
        return "sent"


def _new_state(uid=UID) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _patch_ctx(monkeypatch, lang, tr_map=None):
    async def _ctx(_target):
        return lang, dict(tr_map or {})

    monkeypatch.setattr(reg_i18n, "ctx_for", _ctx)


class _FakeBot:
    """Некоторые более ранние хендлеры роутера используют `Command(...)` (например,
    `/start`/`/cancel`-подобные фильтры), чья `FilterObject.call` требует параметр `bot` по
    имени (`aiogram.filters.Command.__call__`) — не трогает его содержимое для простого
    префиксного совпадения, поэтому пустышки достаточно."""


async def _first_match(observer, event, **kwargs) -> str | None:
    """Первый хендлер observer'а (в порядке регистрации), чей check() проходит — тот же метод,
    которым aiogram резолвит first-match, не переизобретение фильтра."""
    kwargs.setdefault("bot", _FakeBot())
    for h in observer.handlers:
        ok, _ = await h.check(event, **kwargs)
        if ok:
            return h.callback.__name__
    return None


# ── Служебные слова в фильтрах (маршрутизация) ──────────────────────────────────────────────

def test_english_cancel_routes_to_cancel_handler_mid_registration():
    async def go():
        return await _first_match(reg.router.message, _FakeMessage("Cancel"), raw_state="Registration:age")

    assert asyncio.run(go()) == "cancel_registration"


def test_russian_cancel_still_routes_to_cancel_handler():
    async def go():
        return await _first_match(reg.router.message, _FakeMessage("Отмена"), raw_state="Registration:age")

    assert asyncio.run(go()) == "cancel_registration"


def test_slash_cancel_command_still_routes_to_cancel_handler():
    async def go():
        return await _first_match(reg.router.message, _FakeMessage("/cancel"), raw_state="Registration:age")

    assert asyncio.run(go()) == "cancel_registration"


def test_english_looks_good_routes_like_vsyo_verno():
    async def go():
        return await _first_match(reg.router.message, _FakeMessage("Looks good"), raw_state="Registration:confirm")

    assert asyncio.run(go()) == "process_confirm_ok"


def test_russian_vsyo_verno_still_routes_to_confirm_ok():
    async def go():
        return await _first_match(reg.router.message, _FakeMessage("Всё верно"), raw_state="Registration:confirm")

    assert asyncio.run(go()) == "process_confirm_ok"


def test_english_edit_routes_like_izmenit():
    async def go():
        return await _first_match(reg.router.message, _FakeMessage("Edit"), raw_state="Registration:confirm")

    assert asyncio.run(go()) == "process_confirm_edit"


def test_russian_izmenit_still_routes_to_confirm_edit():
    async def go():
        return await _first_match(reg.router.message, _FakeMessage("Изменить"), raw_state="Registration:confirm")

    assert asyncio.run(go()) == "process_confirm_edit"


def test_random_text_does_not_match_cancel_or_confirm_words():
    async def go():
        return await _first_match(reg.router.message, _FakeMessage("Иванова Мария"), raw_state="Registration:age")

    # "process_age" — обычный текстовый шаг без предметного фильтра, ловит любой текст на
    # своём состоянии. Проверяем, что это НЕ cancel_registration (регресс маршрутизации).
    assert asyncio.run(go()) == "process_age"


def test_service_word_sets_contain_both_languages():
    assert {"Отмена", "Cancel", "/cancel"} <= CANCEL_WORDS
    assert {"Всё верно", "Looks good"} <= CONFIRM_WORDS
    assert {"Изменить", "Edit"} <= EDIT_WORDS


# ── Канонизация: _MEMBERSHIP_STEPS (work_status) ────────────────────────────────────────────

def test_membership_step_english_yes_canonicalizes_to_true(monkeypatch):
    _patch_ctx(monkeypatch, "en", {})
    msg = _FakeMessage("Yes")
    state = _new_state()

    asyncio.run(reg_steps.process_work_status(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert data.get("work_status") is True


def test_membership_step_english_no_canonicalizes_to_false(monkeypatch):
    _patch_ctx(monkeypatch, "en", {})
    msg = _FakeMessage("No")
    state = _new_state()

    asyncio.run(reg_steps.process_work_status(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert data.get("work_status") is False


def test_membership_step_russian_da_still_works(monkeypatch):
    # Двуязычие, не замена -- модуль выключен (lang=ru), русский литерал ведёт себя как раньше.
    _patch_ctx(monkeypatch, "ru", {})
    msg = _FakeMessage("Да")
    state = _new_state()

    asyncio.run(reg_steps.process_work_status(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert data.get("work_status") is True


def test_membership_step_english_yes_with_module_off_still_canonicalizes():
    # EN_TO_RU (ярус A) — fallback в canonical_option, НЕ завязан на lang: срабатывает даже
    # если ctx_for настоящим образом резолвит lang="ru" (модуль выключен) -- собственный
    # мышечный ввод "Yes" делегата на русском боте не должен запереть его в ошибке.
    msg = _FakeMessage("Yes")
    state = _new_state()

    asyncio.run(reg_steps.process_work_status(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert data.get("work_status") is True


def test_membership_step_invalid_text_still_errors(monkeypatch):
    _patch_ctx(monkeypatch, "en", {})
    msg = _FakeMessage("Maybe")
    state = _new_state()

    asyncio.run(reg_steps.process_work_status(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert "work_status" not in data
    assert msg.calls  # ошибка отправлена (say() -> _safe_answer -> message.answer)


# ── Канонизация: _CHOICE_STEPS (english_level, через _store_choice/_thin_step) ──────────────

def test_choice_step_english_label_canonicalizes_to_russian_canon(monkeypatch):
    tr_map = {}
    _patch_ctx(monkeypatch, "en", tr_map)
    msg = _FakeMessage("Advanced")  # UI_EN не покрывает предметные подписи -- см. ниже монки
    state = _new_state()

    # english_level options -- машинный перевод (ярус B), не ярус A. Подменяем option_pairs
    # напрямую результатом, который дал бы реальный tr_map (contents неважны -- тестируем
    # канонизацию, не сам перевод, который покрыт tests/test_i18n_options_roundtrip_27.py).
    async def _fake_option_pairs(step_key, lang, _tr_map):
        if step_key == "english_level":
            return [
                ("Начальный", "Beginner"), ("Средний", "Intermediate"),
                ("Продвинутый", "Advanced"), ("Свободный", "Fluent"),
            ]
        return []

    monkeypatch.setattr(reg_i18n, "option_pairs", _fake_option_pairs)

    asyncio.run(reg_steps.process_english_level(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert data.get("english_level") == "Продвинутый"


def test_choice_step_free_text_other_kept_verbatim(monkeypatch):
    _patch_ctx(monkeypatch, "en", {})
    msg = _FakeMessage("Native speaker fluency")  # не совпадает ни с одним вариантом
    state = _new_state()

    asyncio.run(reg_steps.process_english_level(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert data.get("english_level") == "Native speaker fluency"


def test_choice_step_russian_canon_roundtrips_with_module_off():
    # Модуль выключен -- поведение байт-в-байт прежнее: русский вариант остаётся собой.
    msg = _FakeMessage("Средний")
    state = _new_state()

    asyncio.run(reg_steps.process_english_level(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert data.get("english_level") == "Средний"


# ── Канонизация: process_education_status ───────────────────────────────────────────────────

def test_education_status_english_yes_variant_canonicalizes(monkeypatch):
    tr_map = {}
    _patch_ctx(monkeypatch, "en", tr_map)
    msg = _FakeMessage("Yes, full-time")
    state = _new_state()

    async def _fake_option_pairs(step_key, lang, _tr_map):
        if step_key == "education_status":
            return [("Да, очно", "Yes, full-time"), ("Нет", "No")]
        return []

    monkeypatch.setattr(reg_i18n, "option_pairs", _fake_option_pairs)

    asyncio.run(reg_steps.process_education_status(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert data.get("education_status") == "Да, очно"


# ── Канонизация: process_ambassador (пятая точка, reg_flow.py) ─────────────────────────────

def test_ambassador_english_yes_canonicalizes(monkeypatch):
    tr_map = {}
    _patch_ctx(monkeypatch, "en", tr_map)
    msg = _FakeMessage("Yes!")
    state = _new_state()

    async def _fake_option_pairs(step_key, lang, _tr_map):
        if step_key == "ambassador":
            return [("Да!", "Yes!"), ("Пока нет", "Not yet")]
        return []

    monkeypatch.setattr(reg_i18n, "option_pairs", _fake_option_pairs)

    asyncio.run(reg_flow.process_ambassador(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert data.get("is_ambassador_candidate") is True


def test_ambassador_russian_still_works_module_off():
    msg = _FakeMessage("Пока нет")
    state = _new_state()

    asyncio.run(reg_flow.process_ambassador(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert data.get("is_ambassador_candidate") is False


# ── Модуль выключен: все пять точек ведут себя как до фазы ──────────────────────────────────

def test_module_off_choice_step_english_label_not_recognized_as_domain_option():
    # lang=="ru" (реальный ctx_for, БЕЗ монки) -- option_pairs никогда не предлагает
    # английские подписи предметных вариантов (та же лестница резолюции, что везде в фазе),
    # поэтому "Advanced" НЕ канонизируется -- ложится как свободный текст, ТОЧНО так же, как
    # вело бы себя ядро до всей фазы 27 (byte-for-byte, A-05).
    msg = _FakeMessage("Advanced")
    state = _new_state()

    asyncio.run(reg_steps.process_english_level(msg, state, bot=None))

    data = asyncio.run(state.get_data())
    assert data.get("english_level") == "Advanced"
