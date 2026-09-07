"""Phase 28 Plan 08 (SU-08, 28-UI-SPEC §8) — правила балла кнопками.

Задача 1: бот-экран `handlers/admin_reg_scoring.py` — чекбокс-пикеры ЧЕТЫРЁХ скоринговых
множеств строятся из ТЕКУЩЕГО списка вариантов вопроса анкеты (`reg_engine.options(step_key)`),
а не замороженного словаря (в этом разница с `type: "multi"`, см. `admin_modcard.py`). Харнесс
— прямой вызов хендлеров с фейковым `CallbackQuery` (без реального aiogram-диспетчера), тот же
приём, что `tests/test_admin_percity_menu.py`.

pytest-asyncio в проекте не используется — асинхронщина через `asyncio.run()`, БД — временная
(`config.DB_PATH = tmp_path / "..."` + `database.db.init_db()`), как в соседних тестах фазы.
"""
from __future__ import annotations

import asyncio

import handlers.admin_reg_scoring as admin_reg_scoring
from config import config
from database import db as bot_db
from handlers.admin_caps import required_capability
from moderation_card import EMPTY_SENTINEL
from settings_schema import get_setting_typed


def _run(coro):
    return asyncio.run(coro)


def _admin_ready(tmp_path, name="skillup_scoring_ui_28.db"):
    config.DB_PATH = str(tmp_path / name)
    _run(bot_db.init_db())


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.edit_calls = 0

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup
        self.edit_calls += 1


class FakeCallback:
    def __init__(self, data, user_id=1):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _kb_callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: экран «🧮 Правила балла» в боте
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_screen_lists_current_options(tmp_path):
    _admin_ready(tmp_path)
    _run(bot_db.set_setting("study_field_options", "Backend\nFrontend\nDesign"))
    cb = FakeCallback("admin_reg_scoring")
    _run(admin_reg_scoring.admin_reg_scoring(cb))
    assert "Backend" in cb.message.text
    assert "Frontend" in cb.message.text
    assert "Design" in cb.message.text
    callbacks = _kb_callbacks(cb.message.markup)
    assert "scoring_toggle:score_it_fields:0" in callbacks
    assert "scoring_toggle:score_it_fields:1" in callbacks
    assert "scoring_toggle:score_it_fields:2" in callbacks

    # Менеджер поменял список вариантов вопроса — пикер сразу видит новый набор.
    _run(bot_db.set_setting("study_field_options", "OnlyOne"))
    cb2 = FakeCallback("admin_reg_scoring")
    _run(admin_reg_scoring.admin_reg_scoring(cb2))
    assert "OnlyOne" in cb2.message.text
    assert "Backend" not in cb2.message.text
    callbacks2 = _kb_callbacks(cb2.message.markup)
    assert "scoring_toggle:score_it_fields:0" in callbacks2
    assert "scoring_toggle:score_it_fields:1" not in callbacks2


def test_toggle_persists_and_orders(tmp_path):
    _admin_ready(tmp_path)
    _run(bot_db.set_setting("study_field_options", "Backend\nFrontend\nDesign"))
    # Отмечаем в порядке 2, потом 0 — хранение остаётся в порядке ВАРИАНТОВ, не нажатий.
    _run(admin_reg_scoring.scoring_toggle(FakeCallback("scoring_toggle:score_it_fields:2")))
    _run(admin_reg_scoring.scoring_toggle(FakeCallback("scoring_toggle:score_it_fields:0")))
    assert _run(get_setting_typed("score_it_fields")) == ["Backend", "Design"]


def test_empty_set_uses_sentinel_not_default(tmp_path):
    _admin_ready(tmp_path)
    _run(bot_db.set_setting("study_field_options", "Backend\nFrontend"))
    _run(admin_reg_scoring.scoring_toggle(FakeCallback("scoring_toggle:score_it_fields:0")))
    # Снимаем единственную галочку — пустой набор пишется сентинелом, не удалением ключа
    # (реестровый default этого ключа — None, но «менеджер явно снял все галочки» и
    # «никогда не трогал» — разные состояния, тот же приём, что modcard_fields).
    _run(admin_reg_scoring.scoring_toggle(FakeCallback("scoring_toggle:score_it_fields:0")))
    assert _run(bot_db.get_setting("score_it_fields")) == EMPTY_SENTINEL


def test_stale_label_shown_separately(tmp_path):
    _admin_ready(tmp_path)
    _run(bot_db.set_setting("study_field_options", "Backend\nFrontend"))
    _run(bot_db.set_setting("score_it_fields", "OldField\nBackend"))
    cb = FakeCallback("admin_reg_scoring")
    _run(admin_reg_scoring.admin_reg_scoring(cb))
    assert "⚠️ OldField — варианта больше нет" in cb.message.text
    assert "✅ Backend" in cb.message.text
    assert "scoring_drop:score_it_fields:0" in _kb_callbacks(cb.message.markup)

    # Убираем пропавший вариант кнопкой — Backend остаётся отмеченным, значение не теряется.
    cb2 = FakeCallback("scoring_drop:score_it_fields:0")
    _run(admin_reg_scoring.scoring_drop(cb2))
    assert _run(get_setting_typed("score_it_fields")) == ["Backend"]
    assert cb2.answers[0][0] == "OldField: убрано"
    assert "⚠️" not in cb2.message.text


def test_screen_requires_capability():
    assert required_capability(callback_data="admin_reg_scoring") == "settings"
    assert required_capability(callback_data="scoring_toggle:score_it_fields:0") == "settings"
    assert required_capability(callback_data="scoring_limit:score_course_from:3") == "settings"
    assert required_capability(callback_data="scoring_drop:score_it_fields:0") == "settings"
    assert required_capability(callback_data="scoring_noop") == "settings"


def test_no_raw_keys_in_texts(tmp_path):
    _admin_ready(tmp_path)
    _run(bot_db.set_setting("score_it_fields", "OldField"))
    text = _run(admin_reg_scoring.render_scoring_text())
    for code in (
        "score_it_fields", "score_senior_statuses", "score_readiness_counts",
        "score_experience_counts", "score_course_from", "score_stack_from",
        "study_field", "education_status", "readiness", "experience",
    ):
        assert code not in text, f"код ключа/шага «{code}» просочился в текст экрана"


def test_limit_threshold_changes_via_preset(tmp_path):
    _admin_ready(tmp_path)
    cb = FakeCallback("scoring_limit:score_course_from:5")
    _run(admin_reg_scoring.scoring_limit(cb))
    assert _run(get_setting_typed("score_course_from")) == 5
    assert "5" in cb.answers[0][0]
