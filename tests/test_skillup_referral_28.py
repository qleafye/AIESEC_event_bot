"""Phase 28 (28-06, СкиллАп 5 P1): реф-ссылка `amb_<id>` — шестой деп-линк-экстрактор,
пропуск «Источника» у пришедших по реф-ссылке, финальный экран «Хочу свою ссылку»/«Позже».

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, стиль Fake-объектов
aiogram — тот же приём, что `tests/test_city_flow_phase71.py`/`tests/test_skillup_resume_fork_ui_28.py`.

Задача 1: `reg_engine.extract_ambassador_ref`/`resolve_referrer` + пропуск шага «Источник»
(`reg_skip_source_for_referred`) + тумблер «засчитывать только амбассадоров»
(`reg_referrer_must_be_ambassador`). Числовой формат `?start=<id>` НЕ проверяется на
существование реферера (D-06 byte-for-byte, tests/test_city_flow_phase71.py::
test_attribution_survives_city_pick_referrer) — `resolve_referrer` применяется ТОЛЬКО к
новому `amb_`-формату (CONTEXT OQ-2).

Задача 2: шов `handlers/reg_ambassador.py` — второе сообщение-предложение после «поздравляем»
(тумблер `reg_offer_ref_link`), «Хочу свою ссылку» ставит `is_ambassador=1` и шлёт голый URL
третьим сообщением, «Позже» ничего не пишет.

Задача 3: паритет в Mini App — `POST /app/api/reg/draft/submit` отдаёт блок `ambassador`
(mode == "new" + тумблер), `POST /app/api/reg/ambassador` ставит `is_ambassador=1` и отдаёт
ссылку; HTTP-харнесс — `tests/test_miniapp_routes.py`/`tests/test_miniapp_form.py`
(`TestClient` + временная БД).
"""
import asyncio

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from config import config
from database import db
import reg_engine
from handlers import registration as reg
from handlers import reg_ambassador

from tests.test_miniapp_routes import DELEGATE_ID, UNREGISTERED_ID, _cfg, _client, _hdr, _set, _standard_seed
from tests.test_miniapp_routes import _use_tmp_db as _use_tmp_http_db
from tests.test_miniapp_form import _seed_draft, bot_api  # noqa: F401 -- фикстура bot_api

UID = 900806000


def _use_tmp_db(tmp_path, name="test_skillup_referral_28.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


def _state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


class _FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class _FakeChat:
    def __init__(self, cid):
        self.id = cid


class _FakeBotMe:
    def __init__(self, username="TestBot"):
        self.username = username


class _FakeBot:
    """`get_me()` — тот же фолбэк-приём, что `services/scheduler.py::_nudge_keyboard`."""

    def __init__(self, username="TestBot", fail=False):
        self._username = username
        self._fail = fail

    async def get_me(self):
        if self._fail:
            raise RuntimeError("get_me boom")
        return _FakeBotMe(self._username)


class _FakeMessage:
    def __init__(self, uid, username=None, bot=None):
        self.from_user = _FakeUser(uid, username)
        self.chat = _FakeChat(uid)
        self.bot = bot or _FakeBot()
        self.sent = []

    async def answer(self, text=None, reply_markup=None, parse_mode=None, *a, **k):
        self.sent.append((text, reply_markup, parse_mode))
        return None

    async def edit_reply_markup(self, reply_markup=None):
        return None

    def model_copy(self, update=None):
        new = _FakeMessage(self.from_user.id, self.from_user.username, bot=self.bot)
        new.sent = self.sent
        if update and "from_user" in update:
            new.from_user = update["from_user"]
        return new


class _FakeCallback:
    def __init__(self, data, uid, bot=None):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid, bot=bot)
        self.bot = bot or self.message.bot
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


class FakeCommand:
    def __init__(self, args=None):
        self.args = args


def _texts(msg: _FakeMessage):
    return [t for (t, _, _) in msg.sent]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: extract_ambassador_ref / resolve_referrer / пропуск «Источника»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_amb_link_sets_referrer(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 1
    referrer = UID + 2

    async def go():
        await db.add_user({
            "telegram_id": referrer, "full_name": "Реферер Тестов",
            "registration_date": "2026-09-07",
        })
        state = _state(uid)
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, state, bot=_FakeBot(), command=FakeCommand(f"amb_{referrer}"))
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("referrer_id") == referrer


def test_amb_self_link_ignored(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 3

    async def go():
        await db.add_user({
            "telegram_id": uid, "full_name": "Сам Себе Реферер",
            "registration_date": "2026-09-07",
        })
        state = _state(uid)
        msg = _FakeMessage(uid, "u")
        await reg.cmd_start(msg, state, bot=_FakeBot(), command=FakeCommand(f"amb_{uid}"))
        return await state.get_data()

    data = asyncio.run(go())
    assert data.get("referrer_id") is None


def test_amb_unknown_user_falls_through(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 4
    nonexistent = UID + 999

    async def go():
        state = _state(uid)
        msg = _FakeMessage(uid, "u")
        # Не должно падать и не должно проставлять referrer_id — обычный путь без ошибки.
        await reg.cmd_start(msg, state, bot=_FakeBot(), command=FakeCommand(f"amb_{nonexistent}"))
        return await state.get_data(), msg

    data, msg = asyncio.run(go())
    assert data.get("referrer_id") is None
    assert msg.sent, "делегат должен увидеть обычный экран приветствия, не тишину"


def test_source_step_skipped_for_referred_when_toggle_on(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await db.set_setting("reg_skip_source_for_referred", "on")
        return await reg_engine.enabled_steps({"referrer_id": 123, "participant_type": "full"})

    steps = asyncio.run(go())
    assert "source" not in steps


def test_source_step_asked_when_toggle_off(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        # Дефолт off (D-06) — прежнее поведение YL/РилТолк.
        return await reg_engine.enabled_steps({"referrer_id": 123, "participant_type": "full"})

    steps = asyncio.run(go())
    assert "source" in steps


def test_referrer_must_be_ambassador_gate(tmp_path):
    _use_tmp_db(tmp_path)
    plain_referrer = UID + 10
    amb_referrer = UID + 11

    async def go():
        await db.add_user({
            "telegram_id": plain_referrer, "full_name": "Обычный Реферер",
            "registration_date": "2026-09-07",
        })
        await db.add_user({
            "telegram_id": amb_referrer, "full_name": "Амбассадор Реферер",
            "registration_date": "2026-09-07",
        })
        await db.update_user_answers(
            amb_referrer, {"is_ambassador": 1}, allowed_columns=["is_ambassador"],
        )
        await db.set_setting("reg_referrer_must_be_ambassador", "on")
        plain_result = await reg_engine.resolve_referrer(plain_referrer)
        amb_result = await reg_engine.resolve_referrer(amb_referrer)
        return plain_result, amb_result

    plain_result, amb_result = asyncio.run(go())
    assert plain_result is None
    assert amb_result == amb_referrer


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 2: финальный экран — «Хочу свою ссылку» / «Позже»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_offer_hidden_when_toggle_off(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 20

    async def go():
        msg = _FakeMessage(uid)
        await reg_ambassador.offer_ref_link(msg, uid)
        return msg

    msg = asyncio.run(go())
    assert not msg.sent


def test_offer_shown_when_toggle_on(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 21

    async def go():
        await db.set_setting("reg_offer_ref_link", "on")
        msg = _FakeMessage(uid)
        await reg_ambassador.offer_ref_link(msg, uid)
        return msg

    msg = asyncio.run(go())
    assert len(msg.sent) == 1
    text, markup, _ = msg.sent[0]
    assert isinstance(markup, InlineKeyboardMarkup)
    datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert datas == ["regamb:want", "regamb:later"]


def test_want_sets_is_ambassador_and_sends_bare_link(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 22

    async def go():
        await db.add_user({
            "telegram_id": uid, "full_name": "Хочет Ссылку",
            "registration_date": "2026-09-07",
        })
        callback = _FakeCallback("regamb:want", uid)
        await reg_ambassador.regamb_want(callback)
        user = await db.get_user(uid)
        return callback, user

    callback, user = asyncio.run(go())
    assert user["is_ambassador"] == 1
    assert not user.get("is_ambassador_candidate")
    texts = _texts(callback.message)
    # Приёмка 17.09 (п.1): ссылка (сырая) + пояснение, где её найти потом — второе сообщение.
    assert len(texts) == 2
    link_text, link_markup, parse_mode = callback.message.sent[0]
    assert link_text == f"https://t.me/TestBot?start=amb_{uid}"
    assert parse_mode is None
    assert link_markup is None
    from settings_schema import SETTINGS_SCHEMA
    note_text, _note_markup, _note_parse_mode = callback.message.sent[1]
    assert note_text == SETTINGS_SCHEMA["miniapp_form_ambassador_link_note_text"]["default"]


def test_later_writes_nothing(tmp_path):
    _use_tmp_db(tmp_path)
    uid = UID + 23

    async def go():
        await db.add_user({
            "telegram_id": uid, "full_name": "Потом Решит",
            "registration_date": "2026-09-07",
        })
        callback = _FakeCallback("regamb:later", uid)
        await reg_ambassador.regamb_later(callback)
        user = await db.get_user(uid)
        return callback, user

    callback, user = asyncio.run(go())
    assert not user.get("is_ambassador")
    assert not callback.message.sent, "«Позже» не шлёт новых сообщений — только гасит клавиатуру"


def test_offer_failure_does_not_break_finalize(tmp_path):
    """get_me() падает (бот без доступа к своему username) — offer_ref_link молча ничего не
    шлёт, исключение наружу не улетает (сбой предложения не должен ронять заявку)."""
    _use_tmp_db(tmp_path)
    uid = UID + 24

    async def go():
        await db.set_setting("reg_offer_ref_link", "on")
        msg = _FakeMessage(uid, bot=_FakeBot(fail=True))
        await reg_ambassador.offer_ref_link(msg, uid)
        return msg

    msg = asyncio.run(go())
    assert not msg.sent


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 3: паритет в Mini App — POST /app/api/reg/draft/submit + POST /app/api/reg/ambassador
# ══════════════════════════════════════════════════════════════════════════════════════════

def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def http_db_path(tmp_path):
    path = _use_tmp_http_db(tmp_path, "test_skillup_referral_28_http.db")
    _standard_seed()
    return path


@pytest.fixture
def http_client(http_db_path):
    return _client(_cfg(http_db_path))


def test_submit_has_no_block_when_disabled(http_client, bot_api):
    _seed_draft(UNREGISTERED_ID, kind="new", patch={"age": 22, "full_name": "Иван Иванов"})
    resp = http_client.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    assert "ambassador" not in resp.json()


def test_submit_returns_ambassador_block_when_enabled(http_client, bot_api):
    _set("reg_offer_ref_link", "on")
    _seed_draft(UNREGISTERED_ID, kind="new", patch={"age": 22, "full_name": "Иван Иванов"})
    resp = http_client.post("/app/api/reg/draft/submit", headers=_hdr(UNREGISTERED_ID))
    assert resp.status_code == 200, resp.text
    amb = resp.json()["ambassador"]
    assert amb["heading"]
    assert amb["cta"]
    assert amb["later"]


def test_ambassador_endpoint_sets_flag_and_returns_link(http_client):
    resp = http_client.post("/app/api/reg/ambassador", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["link"] == f"https://t.me/YouLead_test_bot?start=amb_{DELEGATE_ID}"
    assert body["heading"]
    assert body["copy_button"]
    assert body["copied_toast"]
    assert body["note"]  # Приёмка 17.09 (п.1): пояснение, где ссылку найти потом
    user = _run(db.get_user(DELEGATE_ID))
    assert user["is_ambassador"] == 1


def test_ambassador_endpoint_requires_form_section(http_client):
    _set("miniapp_section_form", "off")
    resp = http_client.post("/app/api/reg/ambassador", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "section_off", "section": "form"}


def test_link_rendered_as_text_node_not_anchor():
    """Accessibility (28-UI-SPEC.md): ссылка в блоке «Ваша ссылка» — текстовый узел `.text`
    у `h("div", ...)`, НЕ `h("a", {href: ...})` — структурный сторож исходника фронта."""
    from pathlib import Path

    from tests.test_miniapp_frontend import _js_without_comments

    form_js = Path(__file__).resolve().parent.parent / "miniapp" / "static" / "js" / "screens" / "form.js"
    src = _js_without_comments(form_js)
    assert 'h("div", { class: "ambassador-link-box", text: res.link' in src
    assert "href" not in src.split("ambassador-link-box")[1].split("\n")[0]


def test_ambassador_link_screen_renders_note():
    """Приёмка 17.09 (п.1): пояснение под ссылкой — `renderAmbassadorLink` рисует `res.note`
    (когда он есть) отдельным абзацем внутри того же блока «Ваша ссылка»."""
    from pathlib import Path

    from tests.test_miniapp_frontend import _js_without_comments

    form_js = Path(__file__).resolve().parent.parent / "miniapp" / "static" / "js" / "screens" / "form.js"
    src = _js_without_comments(form_js)
    fn_start = src.index("function renderAmbassadorLink(")
    fn_end = src.index("\n  }", fn_start)
    body = src[fn_start:fn_end]
    assert "res.note" in body
