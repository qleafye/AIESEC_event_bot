"""Квик 260912 (W5, Задачи 2/3) — вход бота понимает и русскую, и английскую подпись плитки
главного меню; меню переводится в ОДНОЙ точке (`keyboards.builders.get_main_menu_kb`).

Задача 2: `i18n_ui_en.MENU_EN` (отдельно от `UI_EN` — см. докстринг модуля, «Payment → Оплата»
испортила бы канонизацию свободных ответов анкеты) + `keyboards.builders.MENU_TEXTS`
(множества «русская+английская подпись», выведенные вычислением) + 13 фильтров
(`F.text.in_(MENU_TEXTS[...])` вместо `F.text == "..."`) в `handlers/user_actions.py` (12) и
`handlers/reg_lang.py` (1, `menu_lang`).

Задача 3: сама фабрика `get_main_menu_kb` переводит подписи при `lang == "en"` — единственная
точка перевода, поэтому «восьмая непереведённая точка» физически невозможна; тест этого файла
это доказывает структурным сканом исходников, а не перечислением мест вызова.

Роутинг проверяется через `HandlerObject.check(event, **kwargs)` на РЕАЛЬНЫХ зарегистрированных
хендлерах (тот же метод, которым aiogram резолвит first-match) — тот же приём, что
`tests/test_i18n_service_words_27.py`. pytest-asyncio в этом окружении нет, только
`asyncio.run()`.
"""
import asyncio
import re
from pathlib import Path

import pytest

from aiogram.types import ReplyKeyboardMarkup

from config import config
from database import db
from handlers import registration as reg  # noqa: F401 -- тянет reg_lang в хвосте модуля
from handlers import reg_lang  # noqa: F401 -- регистрирует menu_lang_open на registration.router
from handlers import user_actions as ua_mod
from i18n_ui_en import MENU_EN
from keyboards.builders import MENU_BUTTONS, MENU_TEXTS, get_main_menu_kb

REPO_ROOT = Path(__file__).resolve().parent.parent
UID = 813001


def _use_tmp_db(tmp_path, name="test_menu_i18n_260912.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


async def _enable_lang_module():
    async with db._connect() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("delegate_lang_enabled", "on"),
        )
        await conn.commit()


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    def __init__(self, text, chat_id=UID):
        self.text = text
        self.chat = _FakeChat(chat_id)
        self.from_user = _FakeUser(chat_id)
        self.calls = []

    async def answer(self, text=None, **kwargs):
        self.calls.append((text, kwargs))
        return "sent"


class _FakeBot:
    """Некоторые более ранние хендлеры роутера используют Command(...)-фильтры, чей check()
    требует параметр bot по имени -- пустышки достаточно (см. test_i18n_service_words_27.py)."""


async def _first_match(observer, event, **kwargs) -> str | None:
    kwargs.setdefault("bot", _FakeBot())
    for h in observer.handlers:
        ok, _ = await h.check(event, **kwargs)
        if ok:
            return h.callback.__name__
    return None


# ── Задача 2: покрытие MENU_TEXTS ────────────────────────────────────────────────────────────

def test_menu_texts_covers_all_thirteen_keys():
    expected_keys = {key for key, _ in MENU_BUTTONS} | {"menu_payment"}
    assert set(MENU_TEXTS.keys()) == expected_keys
    assert len(MENU_TEXTS) == 13


def test_menu_texts_each_set_has_ru_and_en_variant():
    ru_by_key = dict(MENU_BUTTONS)
    ru_by_key["menu_payment"] = "💳 Оплата"
    for key, texts in MENU_TEXTS.items():
        ru = ru_by_key[key]
        assert ru in texts
        if key != "menu_lang":  # menu_lang уже двуязычна одной строкой -- множество из 1 элемента
            assert MENU_EN[ru] in texts
            assert len(texts) == 2
        else:
            assert len(texts) == 1


# ── Задача 2: 12 точек user_actions.router матчат обе подписи ───────────────────────────────

_USER_ACTIONS_POINTS = [
    ("menu_coins", "show_my_coins"),
    ("menu_game_tasks", "show_game_tasks"),
    ("menu_payment", "upload_receipt_entry"),
    ("menu_info", "show_info_menu"),
    ("menu_program", "show_program"),
    ("menu_speakers", "show_speakers"),
    ("menu_contacts", "show_contacts"),
    ("menu_referral", "my_referral_link"),
    ("menu_invites", "my_referrals"),
    ("menu_faq", "show_faq"),
    ("menu_question", "ask_organizer_start"),
    ("menu_miniapp", "open_miniapp_button"),
]


@pytest.mark.parametrize("menu_key,handler_name", _USER_ACTIONS_POINTS)
def test_user_actions_filter_matches_both_labels(menu_key, handler_name):
    async def go():
        results = {}
        for text in MENU_TEXTS[menu_key]:
            results[text] = await _first_match(ua_mod.router.message, _FakeMessage(text))
        return results

    results = asyncio.run(go())
    for text, matched in results.items():
        assert matched == handler_name, f"{text!r} -> {matched!r} (ожидали {handler_name!r})"


def test_reg_lang_menu_filter_matches_both_labels():
    async def go():
        results = {}
        for text in MENU_TEXTS["menu_lang"]:
            results[text] = await _first_match(reg.router.message, _FakeMessage(text))
        return results

    results = asyncio.run(go())
    for text, matched in results.items():
        assert matched == "menu_lang_open", f"{text!r} -> {matched!r}"


# ── Задача 2: русский вход не меняется -- ровно тот же хендлер срабатывает на русский текст ──

@pytest.mark.parametrize("menu_key,handler_name", _USER_ACTIONS_POINTS)
def test_russian_label_still_routes_unchanged(menu_key, handler_name):
    ru_by_key = dict(MENU_BUTTONS)
    ru_by_key["menu_payment"] = "💳 Оплата"

    async def go():
        return await _first_match(ua_mod.router.message, _FakeMessage(ru_by_key[menu_key]))

    assert asyncio.run(go()) == handler_name


# ── Задача 3, утверждение 1: покрытие MENU_EN ровно соответствует подписям меню ─────────────

def test_menu_en_keys_match_menu_buttons_minus_lang_plus_payment():
    expected = {text for key, text in MENU_BUTTONS if key != "menu_lang"} | {"💳 Оплата"}
    assert set(MENU_EN.keys()) == expected


# ── Задача 3, утверждение 2: ни один handlers/*.py не сравнивает подпись меню через F.text == ─

_MENU_LABELS = [text for _key, text in MENU_BUTTONS if _key != "menu_lang"] + ["💳 Оплата"]


def test_no_handler_file_matches_menu_label_by_exact_equality():
    offenders = []
    for path in sorted((REPO_ROOT / "handlers").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "F.text" not in line:
                continue
            for label in _MENU_LABELS:
                if label in line and f'F.text == "{label}"' in line:
                    offenders.append((path.name, lineno, label))
    assert offenders == [], (
        "Найден фильтр точного равенства на подпись меню -- используйте "
        f"F.text.in_(MENU_TEXTS[...]) вместо F.text == '...': {offenders!r}"
    )


# ── Задача 3, утверждение 3: реальная клавиатура lang=en -- каждая подпись узнаваема ─────────
#
# menu_miniapp (пустой DASHBOARD_PUBLIC_URL по умолчанию) и menu_faq (нет ни одного пункта FAQ
# в пустой тестовой БД) гейтятся ДО перевода -- их не будет ни на одной из клавиатур этого
# файла независимо от языка, это тот же паритет, что и до Задачи 3. menu_lang по умолчанию
# "off" (единственное исключение из конвенции menu_* default "on") -- тоже не будет без
# отдельного явного `db.set_setting("menu_lang", "on")`, которого ни один тест этого файла не
# делает.
_GATED_KEYS = ("menu_miniapp", "menu_faq", "menu_lang")
_BASELINE_RU_LABELS = {text for key, text in MENU_BUTTONS if key not in _GATED_KEYS}


def test_english_keyboard_labels_are_all_recognizable(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_lang_module()
        # `add_user` не знает колонки `lang` (INSERT со своим явным списком колонок) -- писать
        # язык нужно через `set_user_lang`, тот же приём, что и во всех остальных тестах фазы 27.
        await db.add_user({"telegram_id": UID, "full_name": "Delegate", "registration_date": None})
        await db.set_user_lang(UID, "en")
        kb = await get_main_menu_kb(UID)
        labels = [btn.text for row in kb.keyboard for btn in row]
        assert labels, "английская клавиатура пуста"
        for label in labels:
            recognized = any(label in texts for texts in MENU_TEXTS.values())
            assert recognized, f"подпись {label!r} не лежит ни в одном MENU_TEXTS"
        # английские подписи реально отличаются от русских -- перевод действительно применился,
        # и это ровно ожидаемый переведённый набор (без гейтнутых кнопок).
        expected_en = {MENU_EN[text] for text in _BASELINE_RU_LABELS}
        assert set(labels) == expected_en
        assert not (set(labels) & _BASELINE_RU_LABELS)

    asyncio.run(go())


# ── Задача 3, утверждение 4: паритет ru / выключенный модуль -- байт-в-байт как раньше ───────

def test_russian_keyboard_matches_baseline_regardless_of_lang_module(tmp_path):
    _use_tmp_db(tmp_path)

    async def go():
        await _enable_lang_module()
        await db.add_user({"telegram_id": UID, "full_name": "Delegate", "registration_date": None})
        await db.set_user_lang(UID, "ru")
        kb_ru = await get_main_menu_kb(UID)
        labels_ru = {btn.text for row in kb_ru.keyboard for btn in row}

        await db.set_user_lang(UID, None)
        # Модуль включён, но lang не выбран -- resolve_lang("ask") трактуется как "ru".
        kb_ask = await get_main_menu_kb(UID)
        labels_ask = {btn.text for row in kb_ask.keyboard for btn in row}

        assert labels_ru == _BASELINE_RU_LABELS
        assert labels_ask == _BASELINE_RU_LABELS

    asyncio.run(go())


def test_module_off_keyboard_matches_baseline_even_with_stored_en(tmp_path):
    _use_tmp_db(tmp_path, name="test_menu_i18n_260912_off.db")

    async def go():
        # delegate_lang_enabled НЕ включаем -- модуль выключен.
        await db.add_user({"telegram_id": UID, "full_name": "Delegate", "registration_date": None})
        await db.set_user_lang(UID, "en")
        kb = await get_main_menu_kb(UID)
        labels = {btn.text for row in kb.keyboard for btn in row}
        # Модуль выключен -- lang="en" в БД не имеет значения (resolve_lang это гарантирует).
        assert labels == _BASELINE_RU_LABELS

    asyncio.run(go())
