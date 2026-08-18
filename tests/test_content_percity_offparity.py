"""Phase 09.2 (CITY-04/CITY-05/CITY-06) — СВОДНЫЙ MODULE-OFF ПАРИТЕТ.

Назначение файла (контракт, а не просто набор проверок), по образцу
`tests/test_city_offparity_phase72.py`:

    «Фаза 09.2 не изменила поведение бота, пока менеджер не включил модуль городов.»

Фаза 09.2 добавила ОДИН механизм переопределения настроек по городу (09.2-01) и подключила
к нему пять поверхностей:
    (A/B) главное меню (`keyboards/builders.py::get_main_menu_kb`) и экраны
          «ℹ️ Информация»/«📞 Контакты» (`handlers/user_actions.py`);
    (B)   тексты регистрации — приветствие новичку/вернувшемуся, «заявка принята», текст
          одобрения, режим формы (`handlers/registration.py`);
    (D)   маршрутизация вопроса делегата и уведомления о новой заявке по городу
          (`handlers/admin_caps.py::capability_holders`/`notify_by_capability`,
          `handlers/user_actions.py::process_question`, `handlers/registration.py::
          finalize_registration`);
    (C)   админ-экраны — «🏙 Для города…» на редакторе настройки, «🏙 Кнопки по городу» на
          экране кнопок меню, «🏙 Форма по городам» на лендинге настроек, «🏙 N» на экране
          группы (`handlers/admin.py`).

Каждый тест здесь прогоняется на СВЕЖЕЙ базе, где `event_city_enabled` НАМЕРЕННО не
выставляется даже в "off" (см. `tests/test_city_offparity_phase72.py`'s own rationale) —
так тест ловит и случайное изменение дефолта тумблера, и любое безусловное включение
городского слоя. В базе намеренно ЛЕЖАТ переопределения для ВСЕХ городов по ВСЕМ 21
per_city-ключам реестра (`_seed_overrides`, ключи берутся из самого реестра, а не литералом
— новый per_city-ключ автоматически попадает под эту проверку) — и ни одно из них не имеет
права проявиться ни на одной поверхности. Если кто-то в будущем сделает городской слой
безусловным, ЭТОТ файл должен упасть первым.

pytest-asyncio недоступен в этом окружении — каждый async-хелпер гоняется через
asyncio.run(), config.DB_PATH указывает на файл в tmp_path.
"""
import asyncio
import inspect

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import config
from database import db
from settings_schema import SETTINGS_SCHEMA
from handlers import admin as admin_mod
from handlers import admin_caps
from handlers import registration as reg_mod
from handlers import user_actions as ua_mod
from keyboards.builders import get_main_menu_kb, MENU_BUTTONS
import cities


ADMIN_ID = 920701
MSK_MANAGER_ID = 920702
SPB_MANAGER_ID = 920703
SPB_DELEGATE_ID = 920704
DELEGATE_ID = 920705


def _db_ready(tmp_path, name="test_content_percity_offparity.db"):
    """Свежая база. `event_city_enabled` НЕ выставляется — ни на "on", ни на "off"."""
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_ID]


# Отличимые «городские» значения глобальных ключей — если хоть один просочится в вывод
# любой поверхности при выключенном модуле, тест это заметит по буквальному тексту.
_GLOBAL_TEXT = {
    "event_date": "Общая дата",
    "event_time": "Общее время",
    "event_place_name": "Общая площадка",
    "event_place_address": "Общий адрес",
    "contact_person": "@global_manager",
    "contact_vk": "vk.com/global",
    "contact_tg": "@global_tg",
    "start_text": "Общий текст новичку",
    "start_text_registered": "Общий текст для вернувшихся",
    "reg_complete_text": "Общий текст «заявка принята»",
    "approve_text": "Общий текст одобрения",
}


def _seed_overrides():
    """Переопределения для ВСЕХ per_city-ключей реестра по ВСЕМ городам — набор ключей
    читается из SETTINGS_SCHEMA/cities.is_per_city, а не литералом (сторож ниже проверяет,
    что множество непусто, иначе файл молча проверял бы пустоту)."""
    for key, meta in SETTINGS_SCHEMA.items():
        if not cities.is_per_city(key):
            continue
        # Зафиксировать известный глобальный текст для текстовых ключей — тесты ниже
        # сравнивают вывод поверхности РОВНО с этим литералом, а не с городским.
        if key in _GLOBAL_TEXT:
            asyncio.run(db.set_setting(key, _GLOBAL_TEXT[key]))
        options = meta.get("options") or []
        for code in cities.city_codes():
            override_key = cities.per_city_key(key, code)
            assert override_key is not None
            if options:
                default = meta.get("default")
                value = next((o for o in options if o != default), options[0])
            else:
                value = f"ГОРОДСКОЕ::{key}::{code}"
            asyncio.run(db.set_setting(override_key, value))


def _per_city_key_sentinel_set() -> set[str]:
    return {k for k in SETTINGS_SCHEMA if cities.is_per_city(k)}


class FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    def __init__(self, uid, text=None):
        self.from_user = FakeUser(uid)
        self.chat = FakeChat(uid)
        self.text = text
        self.answers = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.answers.append(text)

    async def answer_photo(self, photo, caption=None, reply_markup=None, parse_mode=None, *a, **k):
        self.answers.append(caption)


class FakeCallback:
    def __init__(self, uid):
        self.from_user = FakeUser(uid)
        self.message = FakeMessage(uid)

    async def answer(self, *args, **kwargs):
        pass


def _new_state(uid: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))


def _add_delegate(uid, event_city=None):
    asyncio.run(db.add_user({
        "telegram_id": uid,
        "full_name": f"Delegate {uid}",
        "registration_date": "2026-08-17 00:00:00",
        "event_city": event_city,
    }))


class RegFakeCommand:
    def __init__(self, args=None):
        self.args = args


class _FinalizeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))


def _finalize_with_city(uid, event_city, monkeypatch):
    async def _fake_row(data):
        return []

    async def _fake_append(row):
        return None

    monkeypatch.setattr(reg_mod, "_sheet_dispatch", lambda *_a, **_kw: (_fake_row, _fake_append))
    monkeypatch.setattr(reg_mod, "_spawn", lambda coro: coro.close())

    bot = _FinalizeBot()
    state = _new_state(uid)
    data = {"full_name": "Тест Тестов", "username": "@test", "participant_type": "full", "event_city": event_city}
    asyncio.run(state.update_data(**data))
    message = FakeMessage(uid)
    asyncio.run(reg_mod.finalize_registration(message, state, bot))
    return message, bot


# ── Сторож: множество per_city-ключей реестра непусто ──────────────────────────────────────

def test_per_city_key_set_is_not_empty():
    keys = _per_city_key_sentinel_set()
    assert len(keys) >= 20, "реестр без per_city-ключей -- этот файл молча проверял бы пустоту"


# ── Поверхность 1: главное меню ─────────────────────────────────────────────────────────

def test_menu_buttons_unaffected_by_city_overrides(tmp_path):
    _db_ready(tmp_path)
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    before = asyncio.run(get_main_menu_kb(SPB_DELEGATE_ID))
    before_texts = [b.text for row in before.keyboard for b in row]

    _seed_overrides()  # event_city_enabled остаётся невыставленным

    after = asyncio.run(get_main_menu_kb(SPB_DELEGATE_ID))
    after_texts = [b.text for row in after.keyboard for b in row]
    assert after_texts == before_texts
    assert after_texts == [label for _key, label in MENU_BUTTONS], "должны быть ВСЕ 9 кнопок (дефолт on)"


# ── Поверхность 2: контакты/инфо-экраны user_actions.py ────────────────────────────────

def test_contacts_and_info_screens_show_global_text(tmp_path):
    _db_ready(tmp_path)
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    _seed_overrides()

    msg = FakeMessage(SPB_DELEGATE_ID)
    asyncio.run(ua_mod.show_contacts(msg))
    assert _GLOBAL_TEXT["contact_person"] in msg.answers[0]
    assert _GLOBAL_TEXT["contact_vk"] in msg.answers[0]
    assert _GLOBAL_TEXT["contact_tg"] in msg.answers[0]
    assert not any("ГОРОДСКОЕ" in a for a in msg.answers)

    msg2 = FakeMessage(SPB_DELEGATE_ID)
    asyncio.run(ua_mod.show_info_menu(msg2))
    assert _GLOBAL_TEXT["event_place_name"] in msg2.answers[0]
    assert not any("ГОРОДСКОЕ" in a for a in msg2.answers)

    cb = FakeCallback(SPB_DELEGATE_ID)
    asyncio.run(ua_mod.info_date(cb))
    assert _GLOBAL_TEXT["event_date"] in cb.message.answers[0]
    assert "ГОРОДСКОЕ" not in cb.message.answers[0]

    cb2 = FakeCallback(SPB_DELEGATE_ID)
    asyncio.run(ua_mod.info_place(cb2))
    assert _GLOBAL_TEXT["event_place_name"] in cb2.message.answers[0]
    assert "ГОРОДСКОЕ" not in cb2.message.answers[0]


# ── Поверхность 3: тексты регистрации + режим формы ─────────────────────────────────────

def test_registration_welcome_and_complete_texts_are_global(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _seed_overrides()

    # Новичок по deep-link на город с переопределением -- всё равно общий текст.
    new_msg = FakeMessage(920710)
    asyncio.run(reg_mod.cmd_start(new_msg, _new_state(920710), bot=object(), command=RegFakeCommand("city_spb")))
    assert new_msg.answers[0] == _GLOBAL_TEXT["start_text"]

    # Вернувшийся делегат из города с переопределением.
    _add_delegate(SPB_DELEGATE_ID, event_city="spb")
    asyncio.run(db.set_user_status(SPB_DELEGATE_ID, "approved"))
    ret_msg = FakeMessage(SPB_DELEGATE_ID)
    asyncio.run(reg_mod.cmd_start(ret_msg, _new_state(SPB_DELEGATE_ID), bot=object(), command=None))
    assert ret_msg.answers[0] == _GLOBAL_TEXT["start_text_registered"]

    # «Заявка принята» -- finalize_registration для делегата города с переопределением.
    finalize_msg, _bot = _finalize_with_city(920711, "spb", monkeypatch)
    assert finalize_msg.answers[0] == _GLOBAL_TEXT["reg_complete_text"]


def test_registration_approve_text_and_track_are_global(tmp_path):
    _db_ready(tmp_path)
    _seed_overrides()

    # Текст одобрения -- город с переопределением игнорируется, module off.
    text = asyncio.run(reg_mod._approve_text_for("full", "spb"))
    assert text == _GLOBAL_TEXT["approve_text"]

    # Режим формы -- переопределение для spb ("full", т.к. дефолт "short") не должно
    # применяться; резолвер обязан вернуть дефолт реестра.
    track = asyncio.run(reg_mod._resolve_track(None, "spb"))
    assert track == "short"


def test_send_completion_and_bonus_uses_global_text(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    _add_delegate(920712, event_city="spb")
    _seed_overrides()
    bot = _FinalizeBot()
    asyncio.run(reg_mod.send_completion_and_bonus(bot, 920712, with_menu=False, participant_type="full"))
    assert bot.sent and bot.sent[0][1] == _GLOBAL_TEXT["approve_text"]


# ── Поверхность 4: маршрутизация вопроса и уведомления о новой заявке ───────────────────

def test_capability_holders_city_kwarg_is_a_noop_when_module_off(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(MSK_MANAGER_ID, "msk"))
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))
    _seed_overrides()

    unfiltered = asyncio.run(admin_caps.capability_holders("moderate_reg"))
    filtered_spb = asyncio.run(admin_caps.capability_holders("moderate_reg", city="spb"))
    assert filtered_spb == unfiltered
    assert MSK_MANAGER_ID in unfiltered and SPB_MANAGER_ID in unfiltered


def test_process_question_recipients_match_unfiltered_holders(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(db.add_staff(MSK_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))
    _add_delegate(DELEGATE_ID, event_city="spb")
    _seed_overrides()

    class _QBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            self.sent.append((chat_id, text))

    bot = _QBot()
    state = _new_state(DELEGATE_ID)
    message = FakeMessage(DELEGATE_ID, text="Когда дедлайн?")
    asyncio.run(ua_mod.process_question(message, state, bot))

    recipients = [chat_id for chat_id, _t in bot.sent]
    expected = asyncio.run(admin_caps.capability_holders("moderate_reg"))
    assert recipients == expected
    assert SPB_MANAGER_ID in recipients  # unfiltered -- everyone with the capability


def test_finalize_registration_notify_matches_unfiltered_holders(tmp_path, monkeypatch):
    _db_ready(tmp_path)
    asyncio.run(db.add_staff(SPB_MANAGER_ID, "reg_manager", ADMIN_ID))
    asyncio.run(db.set_staff_city(SPB_MANAGER_ID, "spb"))
    _seed_overrides()

    delegate_uid = 920713
    _msg, bot = _finalize_with_city(delegate_uid, "spb", monkeypatch)
    # bot.sent also carries the delegate's own completion message -- the fan-out recipients
    # are everything else that was sent the SAME admin_text.
    admin_texts = {t for chat_id, t in bot.sent if chat_id != delegate_uid}
    assert len(admin_texts) == 1, "expected exactly one distinct admin notification text"
    recipients = [chat_id for chat_id, t in bot.sent if t in admin_texts]
    expected = asyncio.run(admin_caps.capability_holders("moderate_reg"))
    assert recipients == expected
    assert SPB_MANAGER_ID in recipients


# ── Поверхность 5: админ-экраны ─────────────────────────────────────────────────────────

class FakeAdminMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class FakeAdminCallback:
    def __init__(self, data, user_id=ADMIN_ID):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeAdminMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeAdminState:
    def __init__(self):
        self.data = {}

    async def set_state(self, state):
        pass

    async def update_data(self, **kwargs):
        self.data.update(kwargs)


def _flat_callback_data(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _flat_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def test_admin_screens_have_no_percity_rows_when_module_off(tmp_path):
    _db_ready(tmp_path)
    _seed_overrides()

    # settings_edit:{per_city key} -- no «🏙 Для города…» row, no override summary line.
    for key in _per_city_key_sentinel_set():
        if SETTINGS_SCHEMA.get(key, {}).get("type") != "text":
            continue
        cb = FakeAdminCallback(f"settings_edit:{key}")
        state = FakeAdminState()
        asyncio.run(admin_mod.settings_edit_start(cb, state))
        assert not any(d and d.startswith("settings_city:") for d in _flat_callback_data(cb.message.markup)), key
        assert "🏙" not in cb.message.text, key

    # menu-buttons screen -- no «🏙 Кнопки по городу» row.
    kb = asyncio.run(admin_mod.build_menu_keyboard())
    assert "menu_city" not in _flat_callback_data(kb)
    assert not any("🏙" in t for t in _flat_texts(kb))

    menu_cb = FakeAdminCallback("menu_city")
    asyncio.run(admin_mod.menu_city_list(menu_cb))
    assert menu_cb.message.edit_calls == 0
    assert menu_cb.answers and menu_cb.answers[0][1] is True

    # settings landing -- no «🏙 Форма по городам» shortcut.
    landing_kb = asyncio.run(admin_mod.build_settings_keyboard())
    assert "settings_city:registration_mode" not in _flat_callback_data(landing_kb)
    assert not any("🏙" in t for t in _flat_texts(landing_kb))

    # group screens -- no «🏙 N» override-count marker anywhere. NOTE: the "reg" group
    # legitimately contains an UNRELATED pre-existing field literally labeled
    # "🏙 Города (варианты)" (settings_schema.py, city-selector question OPTIONS list, not
    # this phase's per_city mechanism) -- so the check must target the marker's exact
    # " · 🏙 {N}" shape, not bare 🏙 presence.
    for token in ("event", "reg"):
        text = asyncio.run(admin_mod.render_settings_group_text(token))
        assert " · 🏙" not in text, token

    # Phase 09.3 (05, CITY-09): group screen now resolves the header, but module off ->
    # admin_selected_city collapses to None regardless of who's asking -- an admin_id call
    # must produce the SAME text as the admin_id=None call above, not just the same absence
    # of the "🏙 N" marker.
    for token in ("event", "reg"):
        text_none = asyncio.run(admin_mod.render_settings_group_text(token))
        text_admin = asyncio.run(admin_mod.render_settings_group_text(token, ADMIN_ID))
        assert text_admin == text_none, token
        kb_none = asyncio.run(admin_mod.build_settings_group_keyboard(token))
        kb_admin = asyncio.run(admin_mod.build_settings_group_keyboard(token, ADMIN_ID))
        assert _flat_callback_data(kb_admin) == _flat_callback_data(kb_none), token
