"""«🔎 Найти настройку» в админке бота: слово -> до восьми настроек кнопками -> тот же экран
правки, что из раздела.

pytest-asyncio в окружении нет — async через `asyncio.run()`, БД — `tests/_dbtpl.fast_init_db`.
"""
import asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User

from config import config
from database import db
from handlers import admin_settings, admin_settings_search as ss
from handlers.admin_caps import required_capability
from handlers.states import SettingsSearch
from domain.settings.search import Candidate, match_word, search, search_terms, words
from domain.settings.synonyms import SETTINGS_SYNONYMS
from tests._dbtpl import fast_init_db

ADMIN = 900261019
MANAGER = 900261020


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="settings_search.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [ADMIN]


# ── общий модуль поиска ───────────────────────────────────────────────────────────────────

def test_words_normalize_case_and_yo():
    assert words("Ёлка, ПРИВЕТСТВИЕ!") == ["елка", "приветствие"]


def test_match_word_prefix_exact_and_one_typo():
    assert match_word("оплата", "оплата") == 2
    assert match_word("опл", "оплата") == 1
    assert match_word("привествие", "приветствие") == 1  # пропущена буква
    assert match_word("при", "плата") == 0
    assert match_word("прив", "прев") == 0  # короткое слово — без опечаток


def test_search_ranks_label_over_synonym_over_help():
    cands = [
        Candidate(key="in_help", label="Что-то", help="тут про оплату"),
        Candidate(key="in_terms", label="Другое", terms=["оплата"]),
        Candidate(key="in_label", label="💳 Оплата: реквизиты"),
    ]
    assert [c.key for c in search(cands, "оплата")] == ["in_label", "in_terms", "in_help"]


def test_search_requires_every_word_and_empty_query_finds_nothing():
    cands = [Candidate(key="a", label="Текст приветствия"), Candidate(key="b", label="Текст отказа")]
    assert [c.key for c in search(cands, "текст приветствия")] == ["a"]
    assert search(cands, "   ") == []
    assert search(cands, "!!!") == []


def test_search_terms_shared_with_miniapp():
    key = next(iter(SETTINGS_SYNONYMS))
    assert search_terms(key) == SETTINGS_SYNONYMS[key]
    assert search_terms(f"{key}__city__msk") == SETTINGS_SYNONYMS[key]
    assert search_terms("нет_такого_ключа") == []
    import miniapp.routers.settings as web

    assert web.search_terms is search_terms  # роутер Mini App берёт синонимы отсюда же


# ── кандидаты бота ────────────────────────────────────────────────────────────────────────

def test_candidates_cover_every_bot_group_key_and_media():
    cands = ss.candidates()
    keys = {c.key for c in cands}
    for _label, token in admin_settings._settings_nav_groups():
        assert set(admin_settings._settings_group_keys(token)) <= keys, token
    for prefix, _l, _p in admin_settings.PHOTO_FIELDS + admin_settings.FILE_FIELDS:
        assert prefix in keys
    for c in cands:
        assert c.extra["cb"].split(":", 1)[0] in ("settings_edit", "settings_photo", "settings_file")
        assert required_capability(callback_data=c.extra["cb"]) == "settings"
        assert len(c.extra["cb"].encode()) <= 64, c.extra["cb"]
        assert c.extra["section"] and c.label


def test_search_finds_greeting_and_payment(tmp_path):
    _ready(tmp_path)
    shown, total = _run(ss.find(ADMIN, "приветствие"))
    assert "start_text" in [c.key for c in shown]
    shown, total = _run(ss.find(ADMIN, "оплата"))
    assert shown and total >= len(shown)
    assert len(shown) <= ss.RESULT_LIMIT
    assert all(c.extra["section"] for c in shown)


def test_results_screen_buttons_and_not_found(tmp_path):
    _ready(tmp_path)
    text, kb = _run(ss.results_screen(ADMIN, "приветствие"))
    assert "«приветствие»" in text
    cbs = [row[0].callback_data for row in kb.inline_keyboard]
    assert cbs[-2:] == ["settings_search", "settings_search_cancel"]
    assert "settings_edit:start_text" in cbs
    assert all(len(row[0].text) <= ss._BUTTON_MAX for row in kb.inline_keyboard)
    assert _run(ss.results_screen(ADMIN, "зюзябра")) is None


def test_city_keys_hidden_when_manager_cannot_edit_shared_value(tmp_path, monkeypatch):
    """Менеджер с одним городом в правах и «Все города» в шапке не видит городских настроек —
    их общее значение ему не принадлежит (то же правило, что `editable` в Mini App)."""
    _ready(tmp_path)
    _run(db.set_setting("event_city_enabled", "on"))
    import domain.cities as cities
    import domain.settings.ops as settings_ops

    async def _visible(_admin):
        return [cities.city_codes()[0]]

    async def _header(_admin):
        return cities.ALL_CITIES

    monkeypatch.setattr(settings_ops, "per_city_visible_codes", _visible)
    monkeypatch.setattr(ss, "admin_selected_city", _header)
    shown, _ = _run(ss.find(MANAGER, "приветствие"))
    assert "start_text" not in [c.key for c in shown]  # start_text — городская настройка
    assert cities.is_per_city("start_text")

    async def _own_city(_admin):
        return cities.city_codes()[0]

    monkeypatch.setattr(ss, "admin_selected_city", _own_city)
    shown, _ = _run(ss.find(MANAGER, "приветствие"))
    assert "start_text" in [c.key for c in shown]


# ── хендлеры и вход ───────────────────────────────────────────────────────────────────────

def _state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))


class _Msg:
    def __init__(self, text):
        self.text = text
        self.from_user = User(id=ADMIN, is_bot=False, first_name="Админ")
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs.get("reply_markup")))


def test_query_shows_results_and_clears_state(tmp_path):
    _ready(tmp_path)
    state = _state()
    _run(state.set_state(SettingsSearch.waiting_query))
    msg = _Msg("приветствие")
    _run(ss.settings_search_query(msg, state))
    text, kb = msg.answers[0]
    assert "нашлось" in text
    assert any(row[0].callback_data == "settings_edit:start_text" for row in kb.inline_keyboard)
    assert _run(state.get_state()) is None


def test_query_not_found_keeps_waiting(tmp_path):
    _ready(tmp_path)
    state = _state()
    _run(state.set_state(SettingsSearch.waiting_query))
    msg = _Msg("зюзябра")
    _run(ss.settings_search_query(msg, state))
    assert msg.answers[0][0] == ("Не нашёл. Попробуйте другое слово, например «оплата» или «приветствие».")
    assert _run(state.get_state()) == SettingsSearch.waiting_query.state


def test_start_sets_state_and_asks_for_word():
    state = _state()
    edited = {}

    class _CB:
        data = "settings_search"
        from_user = User(id=ADMIN, is_bot=False, first_name="Админ")

        class message:  # noqa: N801 — заглушка сообщения с edit_text
            @staticmethod
            async def edit_text(text, **kwargs):
                edited["text"] = text

        async def answer(self, *a, **k):
            return None

    _run(ss.settings_search_start(_CB(), state))
    assert "оплата" in edited["text"] and "приветствие" in edited["text"]
    assert _run(state.get_state()) == SettingsSearch.waiting_query.state


def test_caps_for_search_callbacks_and_state():
    assert required_capability(callback_data="settings_search") == "settings"
    assert required_capability(callback_data="settings_search_cancel") == "settings"
    assert required_capability(raw_state=SettingsSearch.waiting_query.state) == "settings"


def test_manage_section_has_search_first_only_for_settings_holders(tmp_path):
    from handlers.admin_sections import build_section_keyboard, section_of

    _ready(tmp_path)
    assert section_of("settings_search") == "manage"
    kb = _run(build_section_keyboard("manage", ADMIN))
    first = kb.inline_keyboard[0][0]
    assert (first.text, first.callback_data) == ("🔎 Найти настройку", "settings_search")

    _run(db.add_staff(MANAGER, "reg_manager", ADMIN))  # заявки и чеки, без настроек
    kb = _run(build_section_keyboard("manage", MANAGER))
    assert all(b.callback_data != "settings_search" for row in kb.inline_keyboard for b in row)


def test_cancel_clears_state_and_returns_to_manage_section(tmp_path):
    _ready(tmp_path)
    state = _state()
    _run(state.set_state(SettingsSearch.waiting_query))
    edited = {}

    class _CB:
        data = "settings_search_cancel"
        from_user = User(id=ADMIN, is_bot=False, first_name="Админ")

        class message:  # noqa: N801
            @staticmethod
            async def edit_text(text, **kwargs):
                edited["text"], edited["kb"] = text, kwargs.get("reply_markup")

        async def answer(self, *a, **k):
            return None

    _run(ss.settings_search_cancel(_CB(), state))
    assert _run(state.get_state()) is None
    assert "Управление" in edited["text"]
    cbs = [b.callback_data for row in edited["kb"].inline_keyboard for b in row]
    assert "settings_search" in cbs


# ── ревью: длина запроса, выход командой/кнопкой меню, синонимы только бота ─────────────────

def test_long_query_is_refused_and_keeps_waiting(tmp_path):
    _ready(tmp_path)
    state = _state()
    _run(state.set_state(SettingsSearch.waiting_query))
    msg = _Msg("оплата " * 50)
    _run(ss.settings_search_query(msg, state))
    assert msg.answers[0][0].startswith("Слишком длинно, напишите одно-два слова")
    assert _run(state.get_state()) == SettingsSearch.waiting_query.state


def test_echo_of_query_is_trimmed(tmp_path):
    _ready(tmp_path)
    query = "приветствие " + "а" * 80  # длиннее эха, но короче предела запроса
    assert len(query) <= ss.QUERY_MAX
    cands = [Candidate(key="start_text", label="Приветствие " + "а" * 80, extra={"cb": "settings_edit:start_text", "section": "x"})]
    import handlers.admin_settings_search as mod

    orig = mod.candidates
    mod.candidates = lambda: cands
    try:
        text, _kb = _run(ss.results_screen(ADMIN, query))
    finally:
        mod.candidates = orig
    echo = text.split("«", 1)[1].split("»", 1)[0]
    assert len(echo) == ss.ECHO_MAX and echo.endswith("…")


def test_command_or_menu_tap_leaves_search_and_passes_through(tmp_path):
    import pytest
    from aiogram.dispatcher.event.bases import SkipHandler
    from keyboards.builders import all_menu_button_texts

    _ready(tmp_path)
    for text in ("/admin", next(iter(all_menu_button_texts()))):
        state = _state()
        _run(state.set_state(SettingsSearch.waiting_query))
        msg = _Msg(text)
        with pytest.raises(SkipHandler):
            _run(ss.settings_search_query(msg, state))
        assert msg.answers == []
        assert _run(state.get_state()) is None


def test_bot_only_synonyms_not_in_web_search():
    from domain.settings.synonyms import BOT_ONLY_SYNONYMS

    key = next(iter(BOT_ONLY_SYNONYMS))
    assert search_terms(key) == []  # веб-поиск (роутер Mini App зовёт без bot=True)
    assert search_terms(key, bot=True) == BOT_ONLY_SYNONYMS[key]


# ── экраны-кнопки разделов (UAT 09.10: «аватар» → «Не нашёл») ─────────────────────────────

def test_screen_rows_of_sections_are_found(tmp_path):
    """«🖼 Аватар бота» — не ключ реестра, а экран раздела «🎪 Событие». Поиск обязан его
    находить: кнопка ведёт тем же callback, что из раздела, подпись — «экран · раздел»."""
    _ready(tmp_path)
    text, kb = _run(ss.results_screen(ADMIN, "аватар"))
    buttons = [row[0] for row in kb.inline_keyboard]
    avatar = [b for b in buttons if b.callback_data == "admin_bot_avatar"]
    assert avatar, [b.text for b in buttons]
    assert avatar[0].text == "🖼 Аватар бота · 🎪 Событие"
    for word, cb in (("роли", "admin_roles"), ("сезон", "admin_season_reset"), ("таблица", "admin_sheet_target")):
        shown, _ = _run(ss.find(ADMIN, word))
        assert cb in [c.extra["cb"] for c in shown], word


def test_screen_candidates_respect_section_rights(tmp_path):
    """Права — как у раздела: экран без права не показывается, «только суперадмину» — только ему,
    сам поиск в выдаче не встречается."""
    from handlers.admin_sections import SECTIONS, row_callback

    _ready(tmp_path)
    all_screens = {row_callback(r) for _t, _l, rows in SECTIONS for r in rows if r[0] in ("screen", "screen_admin")}
    shown = {c.extra["cb"] for c in _run(ss.screen_candidates(ADMIN))}
    assert "settings_search" not in shown
    assert "admin_season_reset" in shown and "admin_bot_avatar" in shown
    assert shown <= all_screens

    _run(db.add_staff(MANAGER, "reg_manager", ADMIN))  # заявки и чеки, без настроек
    mgr = {c.extra["cb"] for c in _run(ss.screen_candidates(MANAGER))}
    assert "admin_season_reset" not in mgr and "admin_sheet_target" not in mgr
    assert "admin_bot_avatar" not in mgr  # капа settings
    from handlers.admin_caps import _holds

    for cb in mgr:
        assert _holds({"moderate_reg", "moderate_receipts"}, required_capability(callback_data=cb)), cb
