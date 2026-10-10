"""Группа реестра `amb` «🤝 Амбассадоры»: состав, веб-раздел, корпус перевода, доступность."""
from __future__ import annotations

import asyncio
import re

from config import config
from database import db
from handlers import admin_sections as sec
from handlers import admin_settings as st
from services import i18n_sources
import domain.settings.ops as settings_ops
from domain.settings.schema import SETTINGS_SCHEMA
from tests._dbtpl import fast_init_db

ADMIN_ID = 1
_AMB_PREFIX = re.compile(r"^(amb_|ambassador_|wave_)")
# Число делегатских текстов корпуса перевода до переноса (группа game ∪ amb) — не должно меняться.
DELEGATE_KEYS_BEFORE = 471 + 4 + 1 + 3 + 44 + 1 + 17 + 1 + 1 + 1 + 1 + 1 + 8 + 19  # +19 (10.10) — подписи экранов информации/контактов и кнопок «🪙 Баланс»/«🏅 Рейтинг волны»; +8 (10.10) — подписи кнопок фоновых сообщений («Не пришёл», перенос, «🔕»/«🔔»); +1 (10.10, приёмка) — кнопка «▶️ К проверке ответов» на дочитанной анкете (reg_resume_review_label); +1 (10.10, приёмка) — ответ на непонятное сообщение одобренного «Меню обновилось» (menu_refreshed_text); +1 (10.10, приёмка приложения) — резюме без чата с ботом (reg_form_resume_no_chat_text); +1 (10.10, приёмка) — приветствие /start отклонённому в этом сезоне (start_text_rejected); +1 (09.10, приёмка) — заголовок «Нашли — выбери:» над результатами поиска справочника в чате; +17 (09.10) — подписи кнопок главного меню делегата (menu_*_label); +1 (09.10) — причина «За приглашённых до вступления» в истории монет; +3 — тексты делегатам вузов (приветствие новому и уже подавшему, гейма выключена); +4 делегатских текста «Моя ссылка» (статус, баллы, место в волне); +1 — утренний текст повтора QR; +44 — делегатские тексты записи на сессии (session_enroll_*) и теста по компетенциям (quiz_*)


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, *, selection):
    config.DB_PATH = str(tmp_path / "test_amb_settings_group_34.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]
    _run(db.set_setting("amb_team_selection_enabled", "on" if selection else "off"))


def _callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_every_ambassador_key_is_in_amb_group():
    for key, spec in SETTINGS_SCHEMA.items():
        if _AMB_PREFIX.match(key):
            assert spec["group"] == "amb", key
    assert not [k for k, s in SETTINGS_SCHEMA.items() if s["group"] == "game" and _AMB_PREFIX.match(k)]


def test_game_group_keeps_tasks_check_and_coins():
    assert "game_late_penalty_percent" in st._GAME_FIELD_ORDER
    assert not set(st._GAME_FIELD_ORDER) & set(st._AMB_FIELD_ORDER)
    for key in st._AMB_FIELD_ORDER:
        assert SETTINGS_SCHEMA[key]["group"] == "amb", key


def test_bot_group_and_web_section_follow_game():
    tokens = [t for _, t, _ in st.SETTINGS_GROUPS]
    assert tokens.index("amb") == tokens.index("game") + 1
    sections = [s[0] for s in settings_ops.SECTION_GROUPS]
    assert sections.index("amb") == sections.index("game") + 1
    assert settings_ops.GROUP_LABELS["amb"] == "🤝 Амбассадоры"
    assert "amb" in settings_ops.SETTINGS_MAIN_SECTIONS


def test_every_amb_key_reachable_in_web_exactly_once():
    reach = [k for _, _, groups in settings_ops.SECTION_GROUPS if "amb" in groups
             for k in settings_ops.editable_keys() if SETTINGS_SCHEMA.get(k, {}).get("group") == "amb"]
    assert len(reach) == len(set(reach))


def test_translation_corpus_keeps_delegate_texts_and_skips_admin_ones():
    assert "amb" in i18n_sources.DELEGATE_GROUPS
    keys = i18n_sources.delegate_registry_keys()
    assert len(keys) == DELEGATE_KEYS_BEFORE
    assert "amb_progress_text" in keys and "wave_start_message_text" in keys
    for admin_only in ("wave_end_manager_text", "amb_count_deadline", "amb_join_mode", "amb_slots_limit"):
        assert admin_only not in keys


def test_amb_section_has_settings_row():
    assert ("group", "amb") in sec.section_rows("amb")
    assert sec.section_rows("amb")[-1] == ("group", "amb")


def test_amb_group_reachable_with_module_on_and_off(tmp_path):
    _ready(tmp_path, selection=True)
    kb = _run(sec.build_section_keyboard("amb", ADMIN_ID))
    assert "settings_group:amb" in _callbacks(kb)
    _run(db.set_setting("amb_team_selection_enabled", "off"))
    kb = _run(sec.build_section_keyboard("game", ADMIN_ID))
    assert "settings_group:amb" in _callbacks(kb)
    group_kb = _run(st.build_settings_group_keyboard("amb", ADMIN_ID))
    cbs = _callbacks(group_kb)
    assert "toggle_wave_rating_show_names" in cbs
    assert cbs[-1] == "admin_sec:game"  # раздела нет — «Назад» в «🎮 Геймификацию»
    game_cbs = _callbacks(_run(st.build_settings_group_keyboard("game", ADMIN_ID)))
    assert "toggle_amb_team_selection" in game_cbs  # модуль включается оттуда же


def test_guide_entries_for_amb_keys_point_to_ambassador_section():
    from handlers.admin_roles import SETTINGS_GUIDE_SECTIONS
    seen = 0
    for _title, _hint, entries in SETTINGS_GUIDE_SECTIONS:
        for e in entries:
            spec = SETTINGS_SCHEMA.get(e["key"])
            if spec and spec["group"] == "amb":
                seen += 1
                assert e["where"].startswith("🤝 Амбассадоры"), e["key"]
    assert seen


def test_guide_describes_new_phase_settings():
    from handlers.admin_roles import SETTINGS_GUIDE_KEYS
    for key in ("amb_join_mode", "amb_slots_limit", "amb_tiers_count", "amb_tiers_require_approved"):
        assert key in SETTINGS_GUIDE_KEYS, key
