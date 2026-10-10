"""Приёмка 09.10: `/admin` от человека без прав — молчание. Deny-by-default
`CapabilityMiddleware` пропускает апдейт дальше (UNHANDLED), и его не ловил никто — человек
думал, что бот завис. Роутер `handlers/access/admin_no_access.py` стоит сразу после `admin.router` и
отвечает понятным текстом. Остальные админ-команды по-прежнему молчат — поверхность админки
не раскрываем."""
from tests._paths import REPO_ROOT
import asyncio
from pathlib import Path

from config import config
from handlers.access import admin_no_access
from services import i18n_sources
from tests._dbtpl import fast_init_db

ROOT = REPO_ROOT
UID = 912300400


class _User:
    def __init__(self, uid):
        self.id = uid
        self.username = None


class _Chat:
    def __init__(self, cid, kind="private"):
        self.id = cid
        self.type = kind


class _Msg:
    def __init__(self, uid, text="/admin", kind="private"):
        self.from_user = _User(uid)
        self.chat = _Chat(uid, kind)
        self.text = text
        self.sent = []

    async def answer(self, text=None, *a, **k):
        self.sent.append(text)


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "admin_no_access.db")
    fast_init_db()


def test_stranger_admin_command_gets_explanation(tmp_path):
    _ready(tmp_path)
    msg = _Msg(UID)
    asyncio.run(admin_no_access.admin_no_access(msg))
    assert msg.sent == [admin_no_access.ADMIN_NO_ACCESS_TEXT]
    assert "организатор" in admin_no_access.ADMIN_NO_ACCESS_TEXT


def test_staff_not_answered_by_fallback(tmp_path):
    _ready(tmp_path)
    saved = list(config.ADMIN_IDS)
    config.ADMIN_IDS = [UID]
    try:
        msg = _Msg(UID)
        asyncio.run(admin_no_access.admin_no_access(msg))
    finally:
        config.ADMIN_IDS = saved
    assert msg.sent == []


def test_router_included_right_after_admin_router():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i_admin = src.index("dp.include_router(admin.router)")
    i_no_access = src.index("dp.include_router(admin_no_access.router)")
    i_payment = src.index("dp.include_router(payment.router)")
    assert i_admin < i_no_access < i_payment


def test_text_in_translation_corpus():
    texts = {t for _k, t in i18n_sources.code_literals()}
    assert admin_no_access.ADMIN_NO_ACCESS_TEXT in texts
