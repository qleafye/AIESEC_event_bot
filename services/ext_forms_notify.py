"""Уведомления менеджерам о новых ответах внешних форм и алерт повторного входа в Яндекс.

Новые ответы — одним сообщением на форму за тик (пачкой), только у форм с включённым
тумблером, не в тихие часы: ответы копятся и уходят одним сообщением после окна. Получатели —
обладатели moderate_reg; про повторный вход в Яндекс — обладатели settings. В тексте только
число и название формы, без персональных данных.
"""
import html
import logging

from database import ext_forms_db as ef
from handlers.admin_caps import notify_by_capability
from services.quiet_hours import is_quiet, window_for_city
from services.timeutil import msk_now

logger = logging.getLogger(__name__)

_FMT = "%Y-%m-%d %H:%M:%S"


def _plural_answers(n: int) -> str:
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return "ответов"
    tail = n_abs % 10
    if tail == 1:
        return "ответ"
    if 2 <= tail <= 4:
        return "ответа"
    return "ответов"


async def _in_quiet_hours(now) -> bool:
    window = await window_for_city(None)
    if window is None:
        return False
    return is_quiet(now, window[0], window[1])


async def notify_new_answers(bot) -> int:
    """Сколько сообщений отправлено."""
    forms = [f for f in await ef.list_forms() if f.get("notify")]
    if not forms:
        return 0
    now = msk_now()
    if await _in_quiet_hours(now):
        return 0
    sent = 0
    for form in forms:
        try:
            n = await ef.count_new_since(form["id"], form.get("notified_at"))
            if n <= 0:
                continue
            title = html.escape(form.get("title") or "без названия")
            text = f"📝 +{n} {_plural_answers(n)} в «{title}»"
            await notify_by_capability(bot, "moderate_reg", text, parse_mode="HTML")
            await ef.set_form_notified(form["id"], now.strftime(_FMT))
            sent += 1
        except Exception as e:
            logger.error(f"ext_forms notify form {form.get('id')} failed: {e}")
    return sent


async def alert_reauth(bot) -> int:
    """Предупреждение «войдите заново в Яндекс» — один раз на подключение."""
    sent = 0
    for conn in await ef.list_connections_to_alert():
        try:
            text = (
                "⚠️ Доступ к Яндекс Формам истёк, новые ответы перестали приходить. "
                "Войдите заново: «📝 Внешние формы → 🔑 Войти через Яндекс»."
            )
            await notify_by_capability(bot, "settings", text)
            await ef.set_connection_status(
                conn["id"], "needs_reauth", alerted_at=msk_now().strftime(_FMT))
            sent += 1
        except Exception as e:
            logger.error(f"ext_forms reauth alert {conn.get('id')} failed: {e}")
    return sent
