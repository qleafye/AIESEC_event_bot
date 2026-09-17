"""Приёмка 17.09 (живой прогон стенда, lang=en) — регрессия найденной дыры в
`handlers/reg_types_lookup.py`: вопрос шага и подсказка «напиши первые буквы» склеивались в
ОДНУ строку ДО перевода (`f"{prompt}\\n\\n{hint}"`, потом ОДИН вызов `reg_i18n.say` на всё) —
`services.i18n.tr()` ищет перевод по хешу ВСЕГО текста, склейка не совпадала ни с одним из двух
переводов по отдельности, делегат с lang=en видел русский вопрос и подсказку целиком, хотя обе
строки давно переведены (`services/i18n_form_manual.py`).

Приём — тот же, что `tests/test_i18n_bot_render_27.py::_patch_ctx`: подменяем
`handlers.reg_i18n.ctx_for` на фиксированный `(lang, tr_map)`, без похода в БД за языком
делегата — `reg_types_lookup.py` зовёт именно `reg_i18n.ctx_for`, не `services.i18n.context`
напрямую."""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from handlers import reg_i18n
from handlers import reg_types_lookup
from services.i18n_form_manual import EVENT_TEXTS_260917, FORM_DEFAULT_EN
from services.i18n_miniapp_manual import MANUAL_EN
from services.i18n import src_hash

# reg_lookup_hint_default_text живёт в MANUAL_EN (_FORM_INTRO, Квик 260915-skg), не в
# FORM_DEFAULT_EN — карта теста собрана из всех трёх ручных словарей, как это делает реальный
# `services.i18n.tr()` через `load_map`/`translations` после `seed()`.
_ALL_MANUAL_EN = {**MANUAL_EN, **FORM_DEFAULT_EN, **EVENT_TEXTS_260917}

UID = 260917100


def _use_tmp_db(tmp_path, name="test_reg_lookup_i18n_260917.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    def __init__(self, chat_id):
        self.chat = _FakeChat(chat_id)
        self.text = None
        self.sent = []

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append(text)
        return "sent:%d" % len(self.sent)


def _tr_map_for(*ru_texts) -> dict:
    """Настоящая карта переводов из FORM_DEFAULT_EN/EVENT_TEXTS_260917 — не выдуманные тестовые
    строки, чтобы тест проверял РЕАЛЬНЫЙ словарь, а не свою же фикстуру."""
    return {src_hash(t): _ALL_MANUAL_EN[t] for t in ru_texts}


def _patch_ctx_en(monkeypatch, *ru_texts):
    tr_map = _tr_map_for(*ru_texts)

    async def _ctx(_target):
        return "en", tr_map

    monkeypatch.setattr(reg_i18n, "ctx_for", _ctx)


async def _enable_lookup(**extra):
    await db.set_setting("reg_form_v2_enabled", "on")
    await db.set_setting("reg_form_lookup_search", "on")
    for key, value in extra.items():
        await db.set_setting(key, value)


def test_ask_step_translates_prompt_and_hint_separately(tmp_path, monkeypatch):
    """Раньше: единственная склеенная строка не находила перевод, обе части оставались
    русскими. Теперь: каждая часть переводится по отдельности ДО склейки."""
    _use_tmp_db(tmp_path)
    prompt_ru = "Выбери свой город"
    hint_ru = "Начни вводить — подскажем. Например: «спб», «вшэ», «политех»."
    asyncio.run(db.set_setting("reg_prompt_city", prompt_ru))
    _patch_ctx_en(monkeypatch, prompt_ru, hint_ru)
    uid = UID + 1

    async def go():
        await _enable_lookup()
        state = _state(uid)
        await state.update_data(participant_type="full")
        msg = _FakeMessage(uid)
        await reg_types_lookup.ask_step("city", msg, state, "", "full", None)
        return msg

    msg = asyncio.run(go())
    assert msg.sent, "вопрос шага обязан быть задан"
    text = msg.sent[0]
    assert "Choose your city" in text, f"вопрос не переведён: {text!r}"
    assert "Start typing" in text, f"подсказка не переведена: {text!r}"
    assert "Выбери свой город" not in text
    assert "Начни вводить" not in text


def test_lookup_empty_result_translates_template_and_substitutes_query(tmp_path, monkeypatch):
    """`{query}` — открытое множество (свободный ввод), фиксированной пары для каждого значения
    не завести — шаблон переводится, `{query}` подставляется ПОСЛЕ (тот же порядок, что
    `reg_i18n.tr_fmt`)."""
    _use_tmp_db(tmp_path)
    template_ru = "Ничего не нашли по запросу «{query}»"
    _patch_ctx_en(monkeypatch, template_ru)
    uid = UID + 2

    async def go():
        await _enable_lookup()
        state = _state(uid)
        await state.update_data(
            _lookup_step="university", _lookup_results=[], _lookup_other=False,
            _lookup_chips_enabled=False, _lookup_search_enabled=True,
        )
        search_msg = _FakeMessage(uid)
        search_msg.text = "ЗЗЗЗЗЗ260917"
        await reg_types_lookup.receive_lookup_text(search_msg, state, bot=None)
        return search_msg

    msg = asyncio.run(go())
    assert msg.sent, "ответ на пустой результат поиска обязан прийти"
    text = msg.sent[0]
    assert text == "Nothing found for «ЗЗЗЗЗЗ260917»", text
