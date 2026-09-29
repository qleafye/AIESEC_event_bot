"""Инлайн-кнопки делегатских рассылок форума — на языке получателя.

Приёмка 25.09: EN-делегат получал «✅ Сохранил, открывается» под QR, «🚶 Уже еду / 😔 Не смогу
прийти / 📍 Я на месте» под шаблоном «Не пришёл» и «✍️ Написать» после оценки сессии — перевод
в словаре был (или не был), но клавиатуры строились без языка получателя. Плюс догонялка
брошенной анкеты: текст и обе кнопки уходили по-русски.

(а) построители клавиатур для EN-получателя дают английские подписи, для RU — русские;
(б) статический сторож: в модулях, которые шлют делегату из джоб/рассылок (там нет шва
    `_safe_answer`, переводящего клавиатуру при отправке), `InlineKeyboardButton(text=<литерал
    с кириллицей>)` без обёртки перевода запрещён. Админские модули не проверяются."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from services import i18n
from services.i18n_form_manual import FORM_DEFAULT_EN

ROOT = Path(__file__).resolve().parent.parent

_TR_MAP = {i18n.src_hash(ru): en for ru, en in FORM_DEFAULT_EN.items()}


def _texts(markup) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


# --- (а) построители клавиатур ---------------------------------------------------------------

def test_qr_confirm_button_english():
    from services import checkin_broadcast as cb
    assert _texts(cb._confirm_kb("en", _TR_MAP)) == ["✅ Saved, it opens"]
    assert _texts(cb._confirm_kb()) == ["✅ Сохранил, открывается"]
    assert cb._confirm_kb("en", _TR_MAP).inline_keyboard[0][0].callback_data == cb.CONFIRM_CALLBACK


def test_not_arrived_buttons_english():
    from services import checkin_not_arrived as cna
    kb = cna._response_kb("2026-10-03", "en", _TR_MAP)
    assert _texts(kb) == ["🚶 On my way", "😔 Can't make it", "📍 I'm here"]
    assert _texts(cna._response_kb("2026-10-03")) == ["🚶 Уже еду", "😔 Не смогу прийти", "📍 Я на месте"]
    # callback_data не зависит от языка
    ru = cna._response_kb("2026-10-03")
    assert [b.callback_data for r in kb.inline_keyboard for b in r] == \
        [b.callback_data for r in ru.inline_keyboard for b in r]


def test_session_feedback_comment_button_english():
    from services import session_feedback as sf
    assert _texts(sf.comment_offer_keyboard(7, "en", _TR_MAP)) == ["✍️ Write a comment"]
    assert _texts(sf.comment_offer_keyboard(7)) == ["✍️ Написать"]


def test_nudge_keyboard_english():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from services import scheduler
    from core.settings_schema import SETTINGS_SCHEMA

    chat = SETTINGS_SCHEMA["reg_nudge_chat_button_text"]["default"]
    app = SETTINGS_SCHEMA["reg_nudge_app_button_text"]["default"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=chat, url="https://t.me/x?start=continue")],
        [InlineKeyboardButton(text=app, url="https://example.org/app")],
    ])
    out = scheduler._tr_markup(kb, "en", _TR_MAP)
    assert _texts(out) == [FORM_DEFAULT_EN[chat], FORM_DEFAULT_EN[app]]
    assert scheduler._tr_markup(kb, "ru", _TR_MAP) is kb
    nudge = SETTINGS_SCHEMA["nudge_text"]["default"]
    assert i18n.tr(nudge, "en", _TR_MAP) == FORM_DEFAULT_EN[nudge]


def test_context_cached_loads_map_once_per_language(monkeypatch):
    langs = {1: "en", 2: "en", 3: "ru", 4: "ask"}
    loads: list[str] = []

    async def fake_lang(tid, language_code=None):
        return langs[tid]

    async def fake_load(lang):
        loads.append(lang)
        return {} if lang == "ru" else {"h": "x"}

    monkeypatch.setattr(i18n, "delegate_lang", fake_lang)
    monkeypatch.setattr(i18n, "load_map", fake_load)

    async def go():
        maps: dict = {}
        return [await i18n.context_cached(tid, maps) for tid in (1, 2, 3, 4)]

    out = asyncio.run(go())
    assert [lang for lang, _ in out] == ["en", "en", "ru", "ask"]
    assert sorted(loads) == ["en", "ru"]


# --- (б) статический сторож ------------------------------------------------------------------

# Модули, которые шлют делегату клавиатуры из джоб/массовых рассылок — без шва
# `registration._safe_answer`/`reg_i18n.say`, переводящего разметку при отправке.
DELEGATE_KB_FILES = [
    ROOT / "services" / "checkin_broadcast.py",
    ROOT / "services" / "checkin_not_arrived.py",
    ROOT / "services" / "session_feedback.py",
    ROOT / "services" / "forum_noshow_poll.py",
]


def _has_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in text)


def _raw_cyrillic_buttons(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        if name != "InlineKeyboardButton":
            continue
        for kw in node.keywords:
            if kw.arg != "text":
                continue
            v = kw.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str) and _has_cyrillic(v.value):
                found.append((node.lineno, v.value))
            if isinstance(v, ast.JoinedStr) and any(
                isinstance(p, ast.Constant) and _has_cyrillic(str(p.value)) for p in v.values
            ):
                found.append((node.lineno, ast.unparse(v)))
    return found


@pytest.mark.parametrize("path", DELEGATE_KB_FILES, ids=lambda p: p.name)
def test_no_untranslated_cyrillic_button_literals(path):
    bad = _raw_cyrillic_buttons(path)
    assert bad == [], (
        f"{path.name}: подпись кнопки делегату русским литералом без перевода — оберните в "
        f"reg_i18n.tr_text(..., lang, tr_map): {bad}"
    )


def test_guard_catches_raw_literal(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        'from aiogram.types import InlineKeyboardButton as B\n'
        'import aiogram.types as t\n'
        'x = t.InlineKeyboardButton(text="Привет", callback_data="a")\n'
        'y = t.InlineKeyboardButton(text=tr("Привет", "en", {}), callback_data="a")\n',
        encoding="utf-8",
    )
    assert [line for line, _ in _raw_cyrillic_buttons(sample)] == [3]
