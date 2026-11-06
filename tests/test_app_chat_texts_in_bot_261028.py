"""Тексты, которые человек видит в чате, правятся из экранов бота — и на событии с выключенным
приложением.

Подписи копий файлов из приложения, итог подачи/правки анкеты, текст мёртвой ссылки волонтёра
и личные сообщения менеджеру о сверке после привязки чата раньше правились только в приложении
или нигде. Здесь закреплено, куда они выведены (domain/settings/chat_fields.py), что их группы
видны при выключенном приложении и что сторож tests/test_chat_keys_reachable_in_bot.py видит
ключи, переданные через константу или переменную.

Без сети: временная БД и статический обход исходников.
"""
import asyncio

from config import config
from database import db
from domain.settings.chat_fields import CHAT_TEXT_GROUPS
from domain.settings.schema import SETTINGS_SCHEMA
from handlers.settings import admin_sections, admin_settings
from tests._dbtpl import fast_init_db
from tests.test_chat_keys_reachable_in_bot import _scan

ADMIN_ID = 900046

GROUP_OF = {key: token for _label, token, keys in CHAT_TEXT_GROUPS for key in keys}
KEYS_OF = {token: keys for _label, token, keys in CHAT_TEXT_GROUPS}

MINIAPP_CHAT_KEYS = [
    "miniapp_open_text", "miniapp_open_button", "miniapp_disabled_text",
    "miniapp_hub_referral_label_text",
    "miniapp_form_ambassador_offer_heading_text", "miniapp_form_ambassador_offer_body_text",
    "miniapp_form_ambassador_cta_text", "miniapp_form_ambassador_later_text",
    "miniapp_form_ambassador_link_note_text",
    "miniapp_upload_caption_resume", "miniapp_upload_caption_delegate",
    "miniapp_upload_caption_staff", "miniapp_upload_caption_settings",
]

REG_FORM_TAIL = [
    "reg_form_complete_heading_text", "reg_form_complete_body_text",
    "reg_form_edited_heading_text", "reg_form_edited_pending_heading_text",
    "reg_form_resubmit_heading_text",
]

# Найденные ключи -> группа бота (11) и группа реестра, которая не меняется.
PLACED = {
    "volunteer_invite_link_expired_text": ("forum_chat", "reg"),
    **{key: ("reg_chat", "reg") for key in REG_FORM_TAIL},
    "miniapp_upload_caption_delegate": ("miniapp_chat", "miniapp"),
    "miniapp_upload_caption_staff": ("miniapp_chat", "miniapp"),
    "miniapp_upload_caption_settings": ("miniapp_chat", "miniapp"),
    "chat_bind_reconcile_start_text": ("chat_bind_texts", "system"),
    "chat_bind_reconcile_done_text": ("chat_bind_texts", "system"),
}
# Ещё 4 найденных ключа — id/название привязанного чата: в EXCEPTIONS сторожа, не в экранах.
BINDING_VALUES = ["delegate_chat_id", "delegate_chat_title", "sos_chat_id", "sos_chat_title"]


def _admin_ready(tmp_path):
    config.DB_PATH = str(tmp_path / "test_app_chat_texts_in_bot_261028.db")
    fast_init_db()
    config.ADMIN_IDS = [ADMIN_ID]


def _flat(kb):
    return {btn.callback_data for row in kb.inline_keyboard for btn in row}


def test_miniapp_chat_texts_live_in_a_bot_group_with_a_section():
    for key in MINIAPP_CHAT_KEYS:
        assert key in GROUP_OF, key
        assert admin_sections.section_of(f"settings_group:{GROUP_OF[key]}"), key


def test_found_keys_are_placed_as_decided_and_keep_registry_group():
    for key, (token, registry_group) in PLACED.items():
        assert GROUP_OF.get(key) == token, key
        assert SETTINGS_SCHEMA[key]["group"] == registry_group, key
    forum = KEYS_OF["forum_chat"]
    assert forum.index("volunteer_invite_link_expired_text") == forum.index("volunteer_invite_already_has_access_text") + 1
    reg_chat = KEYS_OF["reg_chat"]
    start = reg_chat.index("reg_resume_review_label") + 1
    assert reg_chat[start:start + len(REG_FORM_TAIL)] == REG_FORM_TAIL
    miniapp = KEYS_OF["miniapp_chat"]
    start = miniapp.index("miniapp_upload_caption_resume") + 1
    assert miniapp[start:start + 3] == [
        "miniapp_upload_caption_delegate", "miniapp_upload_caption_staff", "miniapp_upload_caption_settings",
    ]
    assert CHAT_TEXT_GROUPS[-1] == (
        "🔗 Привязка чата: сообщения вам", "chat_bind_texts",
        ["chat_bind_reconcile_start_text", "chat_bind_reconcile_done_text"],
    )
    for key in BINDING_VALUES:
        assert key not in GROUP_OF, key


def test_bind_group_sits_in_manage_right_after_chat():
    rows = admin_sections._declared_rows("manage")
    i = rows.index(("group", "chat"))
    assert rows[i + 1] == ("group", "chat_bind_texts")
    assert rows[i + 2] == ("group", "system")


def test_group_keyboards_offer_editing_of_the_new_texts(tmp_path):
    _admin_ready(tmp_path)
    miniapp = _flat(asyncio.run(admin_settings.build_settings_group_keyboard("miniapp_chat")))
    for key in ("miniapp_upload_caption_delegate", "miniapp_upload_caption_staff", "miniapp_upload_caption_settings"):
        assert f"settings_edit:{key}" in miniapp, key
    bind = _flat(asyncio.run(admin_settings.build_settings_group_keyboard("chat_bind_texts")))
    for key in ("chat_bind_reconcile_start_text", "chat_bind_reconcile_done_text"):
        assert f"settings_edit:{key}" in bind, key


def test_groups_visible_with_miniapp_disabled(tmp_path):
    _admin_ready(tmp_path)
    asyncio.run(db.set_setting("miniapp_enabled", "off"))
    rows = admin_sections.section_rows("manage")
    assert ("group", "miniapp_chat") in rows
    assert ("group", "chat_bind_texts") in rows
    kb = _flat(asyncio.run(admin_settings.build_settings_group_keyboard("miniapp_chat")))
    assert "settings_edit:miniapp_disabled_text" in kb


def test_reg_form_prompts_say_the_text_also_goes_to_chat():
    for key in REG_FORM_TAIL:
        prompt = SETTINGS_SCHEMA[key]["prompt"]
        assert "в чат" in prompt, key
        assert prompt.endswith("Этот же текст бот присылает делегату в чат."), key


def test_guard_sees_keys_passed_by_name():
    reads, writes, _buttons, _prefixes = _scan()
    # handlers/registration.py: _dead_text = "..."; services/chat_tracking.py: константа модуля,
    # handlers/chat/group_chat.py читает её через импорт; services/forum/program.py: PROGRAM_VIEW_KEY.
    for key in ("volunteer_invite_link_expired_text", "chat_bind_reconcile_start_text", "program_miniapp_view"):
        assert key in reads, key
    assert any("через" in where for where in reads["volunteer_invite_link_expired_text"])
    # Запись через имя модуля: amb_status.TOGGLE_KEY, _key(crp.KEY_ENABLED, code).
    for key in ("amb_team_selection_enabled", "chat_rating_post_enabled"):
        assert key in writes, key
