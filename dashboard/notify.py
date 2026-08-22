"""Phase 15 Plan 04 (STAT-02, D-11): уведомление суперадминам о запросе доступа к дашборду.

Дашборд НЕ пишет в БД бота (D-04) — состояние антиспама живёт в памяти процесса дашборда, а
не в новой таблице: рестарт контейнера как максимум разрешит одно лишнее уведомление, что
дешевле новой таблицы и нарушения read-only-контракта пакета.

Ключ антиспама — `telegram_id` ЧЕЛОВЕКА, который просит доступ, а НЕ адрес клиента: за
Cloudflare Tunnel заголовок, которым прокси сообщает происхождение запроса (см.
`dashboard.auth.client_ip`), общий для всех, кто пришёл через один edge-узел, и управляем
извне — ключ по такому адресу запирал бы чужих людей и обходился бы тривиально подделкой
заголовка.

Канал — прямой вызов Bot API (`sendMessage`) через `httpx`, а не запись в БД бота: получатели
— `cfg.admin_ids` (тот же bootstrap-список, что даёт полный набор прав в боте, второй системы
прав не заводим). Любая сетевая ошибка — WARNING в лог и `False`, никогда исключение наружу:
страница «нет доступа» обязана отрисоваться, даже если Telegram недоступен.
"""
from __future__ import annotations

import logging
from datetime import date

import httpx

logger = logging.getLogger(__name__)

# telegram_id -> дата последнего уведомления суперадминов об этом человеке (D-11: не чаще
# одного раза в сутки). Состояние процесса, не БД — см. модульный докстринг.
_last_notified: dict[int, date] = {}

_SEND_MESSAGE_TIMEOUT_SECONDS = 10


def _seen_today(telegram_id: int, today: date) -> bool:
    """Чистая функция без сети — тестируется отдельно. `True`, если сегодня об этом человеке
    уже уведомляли (и тогда состояние НЕ трогается повторно); иначе помечает сегодняшний день
    для него и возвращает `False`."""
    if _last_notified.get(telegram_id) == today:
        return True
    _last_notified[telegram_id] = today
    return False


def _who_text(*, telegram_id: int, username: str | None, first_name: str | None) -> str:
    if first_name and username:
        return f"{first_name} (@{username})"
    if username:
        return f"@{username}"
    if first_name:
        return first_name
    return str(telegram_id)


def _access_request_text(*, telegram_id: int, username: str | None, first_name: str | None) -> str:
    who = _who_text(telegram_id=telegram_id, username=username, first_name=first_name)
    return (
        f"🔔 {who} просит доступ к дашборду статистики.\n\n"
        "Если это свой человек — выдайте право «📊 Статистика» в разделе «👥 Роли и доступы»."
    )


def notify_access_request(
    cfg,
    *,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    today: date | None = None,
) -> bool:
    """`True`, если сообщение дошло хотя бы до одного суперадмина; `False` без исключения —
    при повторном запросе того же человека в тот же день (D-11 антиспам), при пустом
    `cfg.admin_ids`, или при любой сетевой ошибке до Bot API. `today` — только для тестов,
    чтобы не подделывать системные часы."""
    day = today if today is not None else date.today()
    if _seen_today(telegram_id, day):
        return False
    if not cfg.admin_ids:
        return False

    text = _access_request_text(telegram_id=telegram_id, username=username, first_name=first_name)
    reply_markup = {
        "inline_keyboard": [[{"text": "👥 Роли и доступы", "callback_data": "admin_roles"}]]
    }
    url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"

    try:
        with httpx.Client(proxy=cfg.proxy_url, timeout=_SEND_MESSAGE_TIMEOUT_SECONDS) as client:
            sent = False
            for admin_id in cfg.admin_ids:
                try:
                    response = client.post(
                        url,
                        json={
                            "chat_id": admin_id,
                            "text": text,
                            "reply_markup": reply_markup,
                        },
                    )
                    if response.status_code == 200:
                        sent = True
                    else:
                        logger.warning(
                            "notify_access_request: sendMessage to %s returned %s",
                            admin_id, response.status_code,
                        )
                except Exception as e:
                    # Fail-soft per recipient (D-11 shape mirrors notify_by_capability):
                    # один недоступный суперадмин не должен блокировать остальных.
                    logger.warning(
                        "notify_access_request: failed to notify %s: %s", admin_id, e
                    )
            return sent
    except Exception as e:
        logger.warning("notify_access_request: could not reach Bot API: %s", e)
        return False
