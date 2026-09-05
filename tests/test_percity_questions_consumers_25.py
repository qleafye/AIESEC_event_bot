"""Phase 25 Plan 02 (CITYQ-02) — сторожа потребителей городской оси вопросов анкеты.

25-01 завёл реестр и резолверы (`reg_engine._city_override`/`prompt`/`enabled_steps`/
`resume_mode`), но ни один из них не был подключён к поверхностям, которые реально задают
вопросы делегату. Этот файл закрепляет, что подключение (25-02) работает на ВСЕХ
потребителях разом:

    «Делегат СПб получает набор вопросов и тексты своего города — одинаково в чате бота и
    в форме Mini App; режим резюме «только текст» отклоняет файл на обеих поверхностях
    (и на загрузке файла отдельно) одним и тем же реестровым текстом; карточка заявки не
    печатает строку выключенного вопроса без ответа; выключенный модуль городов — нулевая
    разница с сегодняшним поведением.»

pytest-asyncio недоступен в этом окружении — асинхронщина драйвится через `asyncio.run()`,
БД — временный файл per test (`config.DB_PATH = tmp_path/...`), как у соседей
(`tests/test_percity_questions_25.py`, `tests/test_registration_send_guard_260816.py`).
Настройки задаются только записью в `bot_settings` (`db.set_setting`), как это делает
менеджер через админку — ни один тест не мокает `reg_engine.resume_mode`.
"""
from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db

import moderation_card
import reg_engine as e
from handlers import registration as reg
from handlers import reg_flow

from tests.test_miniapp_routes import (
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _set,
    _standard_seed,
    _use_tmp_db,
)
from tests.test_miniapp_form import _seed_draft, bot_api  # noqa: F401 -- фикстура, не вызов

# ── Общие хелперы (приём tests/test_registration_send_guard_260816.py) ────────────────────

def _db_ready(tmp_path, name):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


async def _module_on():
    await db.set_setting("event_city_enabled", "on")


def _state(uid):
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    """Минимальный message: _ask_step/process_resume* трогают только .chat.id и .answer."""

    def __init__(self, chat_id):
        self.chat = _FakeChat(chat_id)
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return "sent:%d" % len(self.calls)


class _FakeDocument:
    def __init__(self, file_id="BQACfake", file_name="cv.pdf", file_size=1024):
        self.file_id = file_id
        self.file_name = file_name
        self.file_size = file_size


class _FakeResumeMessage(_FakeMessage):
    """process_resume/process_resume_invalid трогают только .chat.id, .answer и .document."""

    def __init__(self, chat_id, document=None):
        super().__init__(chat_id)
        self.document = document


# ── 1) Чат бота: тексты вопроса по городу делегата ─────────────────────────────────────

def test_bot_prompt_engine_call_uses_city_text_for_spb_and_global_for_msk(tmp_path):
    _db_ready(tmp_path, "consumers25_prompt_engine.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_prompt_expectations", "Глобальный текст ожиданий")
        await db.set_setting("reg_prompt_expectations__city__spb", "Ожидания от форума (СПб)")
        assert await e.prompt("expectations", "full", "spb") == "Ожидания от форума (СПб)"
        assert await e.prompt("expectations", "full", "msk") == "Глобальный текст ожиданий"

    asyncio.run(scenario())


def test_ask_step_sends_city_prompt_text_to_spb_delegate_and_global_to_msk(tmp_path):
    """Приём tests/test_registration_send_guard_260816.py: FSM data несёт event_city (как
    его туда кладёт handlers/registration.py:1341-1344), _ask_step сам достаёт city_code и
    передаёт его в prompt() — этот тест ловит регрессию, если кто-то уберёт передачу
    третьим аргументом в один из 42 вызовов."""
    _db_ready(tmp_path, "consumers25_ask_step.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_prompt_expectations__city__spb", "Ожидания от форума (СПб)")

        state_spb = _state(880001)
        await state_spb.update_data(participant_type="full", event_city="spb")
        msg_spb = _FakeMessage(880001)
        await reg._ask_step("expectations", msg_spb, state_spb, 1, 10)
        text_spb, _ = msg_spb.calls[0]
        assert "Ожидания от форума (СПб)" in text_spb

        state_msk = _state(880002)
        await state_msk.update_data(participant_type="full", event_city="msk")
        msg_msk = _FakeMessage(880002)
        await reg._ask_step("expectations", msg_msk, state_msk, 1, 10)
        text_msk, _ = msg_msk.calls[0]
        assert "Ожидания от форума (СПб)" not in text_msk

    asyncio.run(scenario())


# ── 2) Чат бота: гейт «резюме только текстом» по городу ────────────────────────────────

def test_process_resume_document_rejected_in_text_only_mode_for_spb(tmp_path, monkeypatch):
    _db_ready(tmp_path, "consumers25_resume_text_only.db")
    advance_calls = []

    async def fake_advance(*args, **kwargs):
        advance_calls.append(args)

    monkeypatch.setattr(reg_flow, "_advance", fake_advance)

    async def scenario():
        await _module_on()
        await db.set_setting("reg_resume_mode__city__spb", "text_only")
        state = _state(880011)
        await state.update_data(event_city="spb")
        msg = _FakeResumeMessage(880011, document=_FakeDocument())
        await reg_flow.process_resume(msg, state, bot=None)

        expected = await db.get_setting("reg_form_resume_text_only_text") or (
            "Здесь резюме принимается текстом — напиши коротко в ответном сообщении."
        )
        text, _ = msg.calls[0]
        assert text == expected
        data = await state.get_data()
        assert "resume_file_id" not in data, "файл не должен попадать в FSM в режиме text_only"
        assert advance_calls == [], "шаг не продвигается — делегат отвечает текстом сам"

    asyncio.run(scenario())


def test_process_resume_document_accepted_in_default_file_or_text_mode(tmp_path, monkeypatch):
    _db_ready(tmp_path, "consumers25_resume_file_or_text.db")
    advance_calls = []

    async def fake_advance(*args, **kwargs):
        advance_calls.append(args)

    monkeypatch.setattr(reg_flow, "_advance", fake_advance)

    async def scenario():
        await _module_on()
        # reg_resume_mode не задан вовсе -- дефолт file_or_text, поведение прежнее.
        state = _state(880012)
        await state.update_data(event_city="spb")
        msg = _FakeResumeMessage(880012, document=_FakeDocument(file_id="BQACresume", file_name="cv.pdf"))
        await reg_flow.process_resume(msg, state, bot=None)

        data = await state.get_data()
        assert data.get("resume_file_id") == "BQACresume"
        assert data.get("resume_file_name") == "cv.pdf"
        assert len(advance_calls) == 1, "файл принят -- шаг продвигается как обычно"

    asyncio.run(scenario())


def test_process_resume_invalid_reuses_same_text_only_registry_text(tmp_path):
    """`process_resume_invalid` (не документ, не текст) в режиме text_only отдаёт ТОТ ЖЕ
    реестровый текст, что и гейт документа -- не отдельный литерал про файл."""
    _db_ready(tmp_path, "consumers25_resume_invalid.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_resume_mode__city__spb", "text_only")
        await db.set_setting("reg_form_resume_text_only_text", "Тут только текстом, без файла.")
        state = _state(880013)
        await state.update_data(event_city="spb")
        msg = _FakeResumeMessage(880013, document=None)
        await reg_flow.process_resume_invalid(msg, state)
        text, _ = msg.calls[0]
        assert text == "Тут только текстом, без файла."

    asyncio.run(scenario())


# ── 3) Веб-форма: набор шагов и тип поля резюме по городу ──────────────────────────────

def test_form_spec_hides_disabled_question_for_spb_shows_for_msk(tmp_path):
    _db_ready(tmp_path, "consumers25_form_spec_steps.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_q_formats", "on")
        await db.set_setting("reg_q_formats__city__spb", "off")

        spec_spb = await e.form_spec({}, "full", "spb")
        spec_msk = await e.form_spec({}, "full", "msk")
        keys_spb = {step["key"] for step in spec_spb["steps"]}
        keys_msk = {step["key"] for step in spec_msk["steps"]}
        assert "formats" not in keys_spb
        assert "formats" in keys_msk

    asyncio.run(scenario())


def test_form_spec_resume_step_is_textarea_text_only_for_spb_file_for_msk(tmp_path):
    _db_ready(tmp_path, "consumers25_form_spec_resume.db")

    async def scenario():
        await _module_on()
        await db.set_setting("reg_q_resume", "on")
        await db.set_setting("reg_resume_mode__city__spb", "text_only")

        spec_spb = await e.form_spec({}, "full", "spb")
        spec_msk = await e.form_spec({}, "full", "msk")
        resume_spb = next(s for s in spec_spb["steps"] if s["key"] == "resume")
        resume_msk = next(s for s in spec_msk["steps"] if s["key"] == "resume")

        assert resume_spb["type"] == "textarea"
        assert resume_spb["resume_mode"] == "text_only"
        assert resume_msk["type"] == "file"
        assert resume_msk["resume_mode"] == "file_or_text"

    asyncio.run(scenario())


# ── 4) Веб-загрузка: 409 resume_text_only РАНЬШЕ проверки формата/размера ──────────────

@pytest.fixture
def db_path(tmp_path):
    path = _use_tmp_db(tmp_path, "consumers25_upload.db")
    _standard_seed()
    return path


@pytest.fixture
def client(db_path):
    return _client(_cfg(db_path))


def test_upload_resume_rejected_409_before_format_check_in_text_only_mode(client, bot_api):
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("reg_resume_mode__city__spb", "text_only"))
    _seed_draft(DELEGATE_ID, kind="edit", event_city="spb")

    # Файл валидного формата и размера -- отказ обязан случиться по режиму, а не по формату.
    resp = client.post(
        "/app/api/uploads?target=resume",
        files={"file": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["reason"] == "resume_text_only"
    assert resp.json()["text"]
    assert bot_api.documents == [], "файл не должен уходить в Bot API -- гейт раньше Telegram"


def test_upload_resume_accepted_when_msk_stays_file_or_text(client, bot_api):
    asyncio.run(db.set_setting("event_city_enabled", "on"))
    asyncio.run(db.set_setting("reg_resume_mode__city__spb", "text_only"))
    _seed_draft(DELEGATE_ID, kind="edit", event_city="msk")

    resp = client.post(
        "/app/api/uploads?target=resume",
        files={"file": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")},
        headers=_hdr(DELEGATE_ID),
    )
    assert resp.status_code == 200, resp.text
    assert bot_api.documents, "МСК не в режиме text_only -- файл принимается как обычно"


# ── 5) Карточка заявки: пустое поле выключенного вопроса не печатается ────────────────

def test_card_answers_omits_empty_disabled_question_for_spb_delegate():
    """Контракт `moderation_card.card_answers` (не меняется этим планом, только закрепляется
    тестом фазы): делегат СПб без ответа на выключенный в его городе вопрос "formats" не
    получает строку этого вопроса в карточке, а ответившие вопросы печатаются как обычно."""
    user = {
        "telegram_id": 1,
        "event_city": "spb",
        "formats": "",  # выключенный в СПб вопрос -- ответа нет и быть не может
        "expectations": "Хочу нетворкинг и новые знакомства",
        "age": "19",
    }
    steps = ["formats", "expectations", "age"]
    rows = moderation_card.card_answers(user, steps, limit=None)
    labels = [label for label, _value in rows]

    assert moderation_card.CARD_STEPS["formats"] not in labels
    assert moderation_card.CARD_STEPS["expectations"] in labels
    assert moderation_card.CARD_STEPS["age"] in labels


# ── 6) Паритет: выключенный модуль городов -- нулевая разница на всех поверхностях ─────

def test_module_off_parity_across_bot_web_and_upload_gate(tmp_path, monkeypatch):
    """Свежая база: `event_city_enabled` НЕ выставляется вовсе. Городские переопределения на
    все кандидаты этого плана посеяны, но ни один резолвер потребителя не должен их увидеть."""
    _db_ready(tmp_path, "consumers25_module_off.db")
    advance_calls = []

    async def fake_advance(*args, **kwargs):
        advance_calls.append(args)

    monkeypatch.setattr(reg_flow, "_advance", fake_advance)

    async def scenario():
        await db.set_setting("reg_prompt_expectations__city__spb", "Ожидания от форума (СПб)")
        await db.set_setting("reg_q_formats", "on")
        await db.set_setting("reg_q_formats__city__spb", "off")
        await db.set_setting("reg_q_resume", "on")
        await db.set_setting("reg_resume_mode__city__spb", "text_only")

        # Бот: текст вопроса -- глобальный, override города не виден.
        prompt_spb = await e.prompt("expectations", "full", "spb")
        prompt_global = await e.prompt("expectations", "full", None)
        assert prompt_spb == prompt_global
        assert "СПб" not in prompt_spb

        # Бот: гейт резюме -- всегда file_or_text, документ принимается.
        state = _state(880021)
        await state.update_data(event_city="spb")
        msg = _FakeResumeMessage(880021, document=_FakeDocument(file_id="BQACoff"))
        await reg_flow.process_resume(msg, state, bot=None)
        data = await state.get_data()
        assert data.get("resume_file_id") == "BQACoff"
        assert len(advance_calls) == 1

        # Веб: набор шагов -- одинаковый для города и без города.
        spec_spb = await e.form_spec({}, "full", "spb")
        spec_global = await e.form_spec({}, "full", None)
        keys_spb = {s["key"] for s in spec_spb["steps"]}
        keys_global = {s["key"] for s in spec_global["steps"]}
        assert keys_spb == keys_global
        assert "formats" in keys_spb

        resume_spb = next(s for s in spec_spb["steps"] if s["key"] == "resume")
        assert resume_spb["type"] == "file"
        assert resume_spb["resume_mode"] == "file_or_text"

    asyncio.run(scenario())
