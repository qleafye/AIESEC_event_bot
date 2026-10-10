"""Приёмка 09.10, тексты анкеты и админки («бот для людей»):

- «Отметь вариантов: не меньше 1.» — неграмотно;
- одна подсказка справочника с примерами «спб»/«вшэ»/«политех» и у города, и у ВУЗа;
- на `31.02.2007` бот говорил «Формат даты: ДД.ММ.ГГГГ», хотя формат верный — такой даты нет;
- текст мимо кнопок развилки резюме получал «Выбери способ кнопкой выше» без намёка, что
  написать об опыте тоже можно (кнопкой); после «📎 Загрузить файл» повторялась общая подсказка
  про все четыре способа вместо просьбы прислать файл;
- ответ на поиск ВУЗа повторял подсказку «Начни вводить…» над кнопками совпадений;
- подтверждение пресета СкиллАп — жаргон («анкету направлений/стека», «по реф-ссылке»,
  «догонялку»)."""
import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import domain.regform.engine as reg_engine
from config import config
from database import db
from domain.settings.schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

UID = 261009300


def _default(key):
    return SETTINGS_SCHEMA[key]["default"]


def _use_tmp_db(tmp_path, name="uat_texts_261009.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()


# ── мультивыбор ────────────────────────────────────────────────────────────────────────────

def test_multi_min_hint_is_grammatical(tmp_path):
    _use_tmp_db(tmp_path)
    hint = asyncio.run(reg_engine.multi_requirement_hint("goal"))
    assert hint == "Отметь хотя бы один вариант."


# ── справочник ─────────────────────────────────────────────────────────────────────────────

def test_lookup_hint_has_no_entity_specific_examples():
    hint = _default("reg_lookup_hint_default_text")
    for example in ("спб", "вшэ", "политех"):
        assert example not in hint


class _Chat:
    def __init__(self, cid):
        self.id = cid


class _Msg:
    def __init__(self, cid, text):
        self.chat = _Chat(cid)
        self.text = text
        self.sent = []

    async def answer(self, text=None, *a, **k):
        self.sent.append(text)


def test_lookup_results_have_found_header_not_hint(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path)
    from handlers import reg_types_lookup

    async def fake_search(kind, q, limit=10):
        return [{"canonical": "СПбГУ"}, {"canonical": "СПбПУ"}]

    monkeypatch.setattr(reg_types_lookup, "search_lookup", fake_search)

    async def go():
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=UID, user_id=UID))
        await state.update_data(_lookup_step="university", _lookup_search_enabled=True)
        msg = _Msg(UID, "спб")
        await reg_types_lookup.receive_lookup_text(msg, state, None)
        return msg.sent

    sent = asyncio.run(go())
    assert sent == [_default("reg_lookup_found_title_text")]
    assert _default("reg_lookup_found_title_text") == "Нашли — выбери:"


# ── дата ───────────────────────────────────────────────────────────────────────────────────

def test_impossible_date_explained_as_nonexistent():
    _, err = reg_engine.validate_answer("birth_date", "31.02.2007")
    assert err == "Такой даты нет — проверь число и месяц."


def test_garbage_date_still_gets_format_hint():
    _, err = reg_engine.validate_answer("birth_date", "вчера")
    assert err == "Формат даты: ДД.ММ.ГГГГ. Попробуй ещё раз."


def test_impossible_date_text_has_manual_translation():
    from i18n_ui_en import UI_EN
    assert "Такой даты нет — проверь число и месяц." in UI_EN


# ── резюме ─────────────────────────────────────────────────────────────────────────────────

def test_fork_pick_hint_mentions_text_option():
    assert "текстом" in _default("reg_resume_fork_pick_hint_text")


def test_file_branch_in_chat_asks_for_file(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        chat = await reg_engine.help_default("resume", None, "chat_file")
        app = await reg_engine.help_default("resume", None, "app")
        return chat, app

    chat, app = asyncio.run(go())
    assert chat.startswith("Прикрепи файл резюме")
    assert "ссылка на резюме" not in chat
    assert app == reg_engine._STEP_HELP_RESUME_FORK  # приложение не меняется


# ── админка: пресет СкиллАп ────────────────────────────────────────────────────────────────

def test_skillup_preset_confirm_has_no_jargon():
    # Первый абзац — что включит пресет. Ниже цитируются подписи кнопок приложения
    # («🎁 Текст предложения реф-ссылки») — их менеджер ищет глазами, менять там нельзя.
    text = _default("skillup_preset_confirm_text").split("\n\n")[0]
    for jargon in ("направлений/стека", "реф-ссылк", "догонялк"):
        assert jargon not in text


# ── админка: шапка /admin ──────────────────────────────────────────────────────────────────

def test_admin_header_leads_with_buttons_and_folds_commands(tmp_path):
    """Шапка /admin была списком слэш-команд. Человеку — «выберите раздел кнопкой», команды
    остаются (на них завязаны те, кто привык), но свёрнуты в раскрывающуюся цитату."""
    from tests.test_marketing_role_261001 import ADMIN_ID, _admin_help, _ready
    _ready(tmp_path)
    text = _admin_help(ADMIN_ID)[0]
    head, _, folded = text.partition("<blockquote expandable>")
    assert "Выберите раздел кнопкой ниже." in head
    assert "/stats" not in head
    assert "/stats_monthly" in folded and folded.endswith("</blockquote>")


def test_resume_fork_help_texts_have_english_and_are_in_corpus():
    from services import i18n_sources
    from services.i18n_form_manual import _REGISTRY_TEXTS_EN
    corpus = {t for _k, t in i18n_sources.code_literals()}
    for text in (reg_engine._STEP_HELP_RESUME_FORK, reg_engine._STEP_HELP_RESUME_FORK_FILE_CHAT):
        assert text in _REGISTRY_TEXTS_EN, text
        assert text in corpus, text


def test_admin_editor_default_is_fork_help_not_file_branch(tmp_path):
    """Ревью: редактор подсказки в админке (`admin_reg_percity`) зовёт help_default без
    surface — он должен показывать подсказку развилки, а не текст ветки «файл»."""
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        return await reg_engine.help_default("resume", None)

    assert asyncio.run(go()) == reg_engine._STEP_HELP_RESUME_FORK
