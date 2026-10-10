"""Пакет C, п.2 (24.09): хаб «🎪 Форум: функции» открывается под `moderate_reg`
(ADMIN_CAPS["admin_forum_functions"]), но кнопки внутри ведут на РОДНЫЕ экраны своих
функций под ДРУГИМИ капами (`settings` для «📱 Приложение»/«🔘 Кнопки меню»/«⭐ Отзывы»/
«🎟 Включить/выключить QR», `checkin` для «✅ Отметки на форуме»). Держатель одной
`moderate_reg` тапал кнопку и получал «Недостаточно прав» — `_render_hub` теперь рисует
только кнопки, на которые есть право (`required_capability`/`resolve_capabilities`/`_holds`,
тот же приём, что `handlers.settings.admin_sections.visible_rows`), остальные скрыты. Статусные
строки (✅/❌) остаются видны всем, кто вообще открыл хаб — это факт, а не действие.

pytest-asyncio недоступна — async через `asyncio.run()` (конвенция проекта). БД —
`tmp_path`, шаблон `fast_init_db` (`tests/_dbtpl.py`)."""
from __future__ import annotations

import asyncio

from config import config
from database import db
from handlers.forum import admin_forum_functions as aff
from handlers.access.admin_caps import role_caps_key
from tests._dbtpl import fast_init_db

SUPERADMIN_ID = 900924301
MANAGER_ID = 900924302


def _run(coro):
    return asyncio.run(coro)


def _ready(tmp_path, name="test_admin_forum_functions_caps.db"):
    config.DB_PATH = str(tmp_path / name)
    fast_init_db()
    config.ADMIN_IDS = [SUPERADMIN_ID]


def _only_caps(role: str, caps: str):
    _run(db.set_setting(role_caps_key(role), caps))


def _cbs(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


# ══════════════════════════════════════════════════════════════════════════════════════════
# Держатель только moderate_reg: видит статусы, но кнопки — только свои
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_moderate_reg_only_manager_sees_only_moderate_reg_buttons(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", SUPERADMIN_ID))
    _only_caps("reg_manager", "moderate_reg")

    text, kb = _run(aff._render_hub(MANAGER_ID, "msk"))
    cbs = _cbs(kb)

    # Свои капы (moderate_reg) — кнопка на месте.
    assert "checkinqr_cfg:msk" in cbs
    assert "checkinvol_cfg:msk" in cbs
    assert "asos_city:msk" in cbs
    assert "checkin_training_sheet" in cbs  # бэклог №7: лист учебных QR — checkin ИЛИ moderate_reg

    # Чужие капы (settings/checkin) — кнопки СКРЫТЫ, не просто недоступны.
    assert "forumfn_qr:msk" not in cbs
    assert "forumfn_open:chk:msk" not in cbs
    assert "forumfn_open:app:msk" not in cbs
    assert "forumfn_open:menu:msk" not in cbs
    assert "forumfn_open:fb:msk" not in cbs

    # Статусные строки текста — про ВСЕ функции, независимо от прав на кнопку.
    assert "🎟 QR для входа" in text
    assert "🎫 Сканер в приложении" in text
    assert "✅ Отметки на форуме" in text
    assert "🔘 Кнопки меню" not in text  # эта строка — заголовок КНОПКИ п.6, не статус


def test_settings_only_manager_sees_settings_and_checkin_denied(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", SUPERADMIN_ID))
    _only_caps("reg_manager", "settings")

    _text, kb = _run(aff._render_hub(MANAGER_ID, "msk"))
    cbs = _cbs(kb)

    assert "forumfn_qr:msk" in cbs
    assert "forumfn_open:app:msk" in cbs
    assert "forumfn_open:menu:msk" in cbs
    assert "forumfn_open:fb:msk" in cbs

    # moderate_reg-only и checkin-only кнопки скрыты.
    assert "checkinqr_cfg:msk" not in cbs
    assert "checkinvol_cfg:msk" not in cbs
    assert "asos_city:msk" not in cbs
    assert "forumfn_open:chk:msk" not in cbs
    assert "checkin_training_sheet" not in cbs


def test_checkin_only_manager_sees_only_admin_checkin(tmp_path):
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", SUPERADMIN_ID))
    _only_caps("reg_manager", "checkin")

    _text, kb = _run(aff._render_hub(MANAGER_ID, "msk"))
    cbs = _cbs(kb)

    assert "forumfn_open:chk:msk" in cbs
    assert "checkin_training_sheet" in cbs
    for cb in ("forumfn_qr:msk", "checkinqr_cfg:msk", "forumfn_open:app:msk",
               "checkinvol_cfg:msk", "forumfn_open:menu:msk", "forumfn_open:fb:msk", "asos_city:msk"):
        assert cb not in cbs


def test_superadmin_sees_every_button(tmp_path):
    """Держатель всех семи прав (config.ADMIN_IDS, D-12) видит хаб целиком — фильтр по
    правам не отнял ничего у того, у кого право реально есть."""
    _ready(tmp_path)
    text, kb = _run(aff._render_hub(SUPERADMIN_ID, "msk"))
    cbs = _cbs(kb)
    for cb in ("forumfn_qr:msk", "checkinqr_cfg:msk", "forumfn_open:chk:msk",
               "forumfn_open:app:msk", "checkinvol_cfg:msk", "forumfn_open:menu:msk",
               "forumfn_open:fb:msk", "asos_city:msk"):
        assert cb in cbs, f"{cb} пропал у держателя всех прав"
    # «← Назад» (`back_button("admin_checkin", ...)` резолвит РАЗДЕЛ-владелец "admin_checkin",
    # т.е. "apps" -> "admin_sec:apps", не сам литерал) остаётся последней строкой при любом
    # наборе прав — не путать с кнопкой «✅ Отметки на форуме» (callback "admin_checkin").
    assert cbs[-1] == "admin_sec:apps"
    assert _texts(kb)[-1] == "◀️ Назад"


def test_manager_with_unrelated_cap_sees_only_back_button(tmp_path):
    """`stats` не гейтит ни одну кнопку хаба — держатель видит статусы, но ни одной живой
    кнопки, кроме навигационного «← Назад» (не фильтруется правом — как и `admin_sec:*`,
    это точка входа, а не действие)."""
    _ready(tmp_path)
    _run(db.add_staff(MANAGER_ID, "reg_manager", SUPERADMIN_ID))
    _only_caps("reg_manager", "stats")

    _text, kb = _run(aff._render_hub(MANAGER_ID, "msk"))
    assert _cbs(kb) == ["admin_sec:apps"]  # только «← Назад»


# ══════════════════════════════════════════════════════════════════════════════════════════
# «◀️ Назад» хаба ведёт в раздел, где объявлен САМ хаб, а не «✅ Отметки на форуме»
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_hub_back_leads_to_own_section(tmp_path, monkeypatch):
    from handlers.settings import admin_sections

    _ready(tmp_path)
    own = f"admin_sec:{admin_sections.section_of('admin_forum_functions')}"
    assert own == "admin_sec:apps"

    _text, kb = _run(aff._render_hub(SUPERADMIN_ID, "msk"))
    assert _cbs(kb)[-1] == own
    _text, kb = _run(aff._render_city_picker())
    assert _cbs(kb)[-1] == own

    # «Назад» выводится из раздела самого хаба: переезд «✅ Отметки на форуме» в другой
    # раздел не должен уводить менеджера из хаба туда.
    real = admin_sections.section_of
    monkeypatch.setattr(
        admin_sections, "section_of",
        lambda cb: "other" if cb == "admin_checkin" else real(cb),
    )
    _text, kb = _run(aff._render_hub(SUPERADMIN_ID, "msk"))
    assert _cbs(kb)[-1] == own
