"""Quick 260914-k74 (Задача 3): модератор, заблокировавший бота, — один алерт в сутки.

До этой правки `notify_by_capability` ловил `TelegramForbiddenError` тем же общим
`except Exception`, что и любую другую ошибку доставки: два ERROR в лог НА КАЖДУЮ заявку,
и ни один живой админ не узнавал, что у модератора отвалились уведомления. Эти тесты
покрывают новое поведение: Forbidden не считается в `sent`, первый Forbidden для uid даёт
один WARNING + один алерт остальным `config.ADMIN_IDS` (не самому заблокировавшему), повтор
внутри 24 ч молчит (`logger.debug`), а через 24 ч алерт повторяется. Прочие исключения ведут
себя как раньше.

Стиль — как у tests/test_roles_phase8.py: asyncio.run() (pytest-asyncio в проекте нет),
БД в tmp_path, config.ADMIN_IDS подменяется списком тестовых id. Различаем «обычную»
отправку и «алертную» по тексту сообщения (алерт всегда содержит «заблокировал бота») —
FakeBot ловит именно этим, а не порядком вызовов.
"""
import asyncio

from aiogram.exceptions import TelegramForbiddenError

from config import config
from database import db
from handlers import admin_caps

ADMIN_A = 910001  # обычный админ, всегда доступен
ADMIN_BLOCKED = 910002  # заблокировал бота
ADMIN_C = 910003  # ещё один обычный админ


def _ready(tmp_path):
    config.DB_PATH = str(tmp_path / "notify_blocked.db")
    asyncio.run(db.init_db())
    config.ADMIN_IDS = [ADMIN_A, ADMIN_BLOCKED, ADMIN_C]
    admin_caps._blocked_notified_at.clear()


def _is_alert(text: str) -> bool:
    return "заблокировал бота" in text


class _NotifyBot:
    """chat_id из `forbidden_ids` роняет ОСНОВНУЮ отправку Forbidden-ом; chat_id из
    `alert_fail_ids` роняет отправку АЛЕРТА (отличаем по тексту, не по порядку вызова) —
    так проверяется, что провал самого алерта не трогает `sent`."""

    def __init__(self, forbidden_ids=None, alert_fail_ids=None, other_error_ids=None):
        self.sent = []
        self.forbidden_ids = set(forbidden_ids or [])
        self.alert_fail_ids = set(alert_fail_ids or [])
        self.other_error_ids = set(other_error_ids or [])

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if _is_alert(text):
            if chat_id in self.alert_fail_ids:
                raise RuntimeError("alert delivery boom")
            self.sent.append((chat_id, text))
            return
        if chat_id in self.forbidden_ids:
            raise TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")
        if chat_id in self.other_error_ids:
            raise RuntimeError("unrelated delivery boom")
        self.sent.append((chat_id, text))


def test_forbidden_not_counted_in_sent_and_recipient_dropped(tmp_path):
    _ready(tmp_path)
    bot = _NotifyBot(forbidden_ids=[ADMIN_BLOCKED])

    sent = asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))

    main_sends = [(cid, t) for cid, t in bot.sent if not _is_alert(t)]
    assert sent == 2  # ADMIN_A + ADMIN_C, заблокированный не считается
    assert (ADMIN_BLOCKED, "hi") not in main_sends
    assert {cid for cid, _t in main_sends} == {ADMIN_A, ADMIN_C}


def test_first_forbidden_alerts_other_admins_not_the_blocked_one(tmp_path):
    _ready(tmp_path)
    bot = _NotifyBot(forbidden_ids=[ADMIN_BLOCKED])

    asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))

    alerts = [(cid, t) for cid, t in bot.sent if _is_alert(t)]
    alert_recipients = {cid for cid, _t in alerts}
    assert alert_recipients == {ADMIN_A, ADMIN_C}
    assert ADMIN_BLOCKED not in alert_recipients
    assert str(ADMIN_BLOCKED) in alerts[0][1]  # текст называет заблокированного по id


def test_two_calls_in_a_row_alert_exactly_once(tmp_path):
    _ready(tmp_path)
    bot = _NotifyBot(forbidden_ids=[ADMIN_BLOCKED])

    asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))
    asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi again"))

    alerts = [(cid, t) for cid, t in bot.sent if _is_alert(t)]
    assert len(alerts) == 2  # один вызов -> алерт ADMIN_A + ADMIN_C, второй вызов молчит


def test_alert_repeats_after_24h_cooldown(tmp_path):
    _ready(tmp_path)
    bot = _NotifyBot(forbidden_ids=[ADMIN_BLOCKED])

    asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))
    alerts_after_first = len([1 for cid, t in bot.sent if _is_alert(t)])
    assert alerts_after_first == 2

    import time
    admin_caps._blocked_notified_at[ADMIN_BLOCKED] = time.time() - 25 * 3600

    asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))
    alerts_after_second = len([1 for cid, t in bot.sent if _is_alert(t)])
    assert alerts_after_second == 4  # ещё два алерта -- кулдаун истёк


def test_other_exception_still_errors_without_alert(tmp_path, caplog):
    _ready(tmp_path)
    bot = _NotifyBot(other_error_ids=[ADMIN_BLOCKED])

    import logging
    with caplog.at_level(logging.ERROR, logger=admin_caps.logger.name):
        sent = asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))

    assert sent == 2
    assert not any(_is_alert(t) for _cid, t in bot.sent)
    assert admin_caps._blocked_notified_at == {}
    assert any("failed to notify" in r.message for r in caplog.records)


def test_alert_send_failure_does_not_change_returned_sent(tmp_path):
    _ready(tmp_path)
    bot = _NotifyBot(forbidden_ids=[ADMIN_BLOCKED], alert_fail_ids=[ADMIN_A, ADMIN_C])

    sent = asyncio.run(admin_caps.notify_by_capability(bot, "moderate_reg", "hi"))

    assert sent == 2  # алерты не долетели ни одному админу, но это не sent-получателей
    assert not any(_is_alert(t) for _cid, t in bot.sent)
