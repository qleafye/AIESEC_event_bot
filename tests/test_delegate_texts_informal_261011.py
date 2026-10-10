"""Приёмка 10.10: продукт обращается к делегату на «ты». Догонялка о незавершённой анкете и
тексты о возврате заявки (из автоотказа, в ожидание, повторная подача, правка) были на «вы».
"""
import re

import pytest

from domain.settings.schema import SETTINGS_SCHEMA
from handlers.applications import admin_reject_journal
from services import scheduler
from services.i18n.i18n_form_manual import FORM_DEFAULT_EN

_FORMAL = re.compile(r"\b(Вы|Вас|Вам|Ваш\w*|вы|вас|вам|ваш\w*|Отправьте|отправьте|нажмите|Нажмите)\b")

_KEYS = (
    "nudge_text",
    "reject_rules_return_text",
    "revert_pending_notify_text",
    "resubmit_granted_notify_text",
    "edit_granted_notify_text",
)


@pytest.mark.parametrize("key", _KEYS)
def test_delegate_default_is_informal_and_translated(key):
    default = SETTINGS_SCHEMA[key]["default"]
    assert default and not _FORMAL.search(default), default
    assert default in FORM_DEFAULT_EN, f"нет английского перевода: {default}"


def test_code_fallbacks_match_registry():
    assert scheduler.DEFAULT_NUDGE_TEXT == SETTINGS_SCHEMA["nudge_text"]["default"]
    assert admin_reject_journal.DEFAULT_RETURN_TEXT == SETTINGS_SCHEMA["reject_rules_return_text"]["default"]
