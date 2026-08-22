"""Quick 260822 — версионирование согласий: чистые помощники без хендлеров.

Зачем. Текст согласия (consent_list) и PDF правятся из админки; без метки редакции нечем
доказать, ЧТО именно подписал делегат. Настройка `consent_version` (реестр) пишется в
аудит-таблицу `user_consents` при каждой подписи (database.db.record_user_consent); здесь —
чтение этой истории для карточки заявки и для гейта пересогласия.

Живёт в `services/`, а не в `handlers/`: карточку рендерит `handlers/admin_moderation.py`,
пересбор — `handlers/reg_consent.py`, и ни один из них не должен тянуть за собой другой
(handlers.* друг на друга — только через швы владельца роутера).
"""
import html
import logging

from database.db import current_consent_version, get_user_consent_versions
from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

# Текст напоминания менеджеру (часть B бэклога): целями обработки управляет менеджер, а не
# разработчик — поэтому объясняем, ЧТО изменилось и ЧТО проверить, без ключей настроек.
PURPOSE_REMINDER_TEXT = (
    "ℹ️ Цели обработки данных изменились{what} — перечитайте текст согласия и PDF в "
    "«📋 Согласия» и при необходимости поднимите версию."
)
PURPOSE_REMINDER_BUTTON = "📋 Открыть согласия"
# Кнопка ведёт на существующий экран группы настроек — новых callback'ов не нужно.
PURPOSE_REMINDER_CALLBACK = "settings_group:consent"
OLD_VERSION_MARKER = "⚠️ подписано старой редакцией"


def purpose_reminder_text(what: str | None = None) -> str:
    suffix = f" ({html.escape(what)})" if what else ""
    return PURPOSE_REMINDER_TEXT.format(what=suffix)


async def consent_card_line(user_id: int) -> str | None:
    """Одна строка для карточки заявки: «📝 Согласие: v<версия>», плюс маркер, если
    последняя подпись делегата — не текущая редакция. None — подписей нет вовсе (модуль
    согласий выключен или пользователь из эпохи до модуля): строку не показываем."""
    try:
        rows = await get_user_consent_versions(user_id)
    except Exception as e:  # fail-soft: карточка важнее строки про согласие
        logger.error(f"consent_card_line: история согласий {user_id} не прочиталась: {e}")
        return None
    if not rows:
        return None
    latest = rows[-1][1]
    current = await current_consent_version()
    if latest is None:
        line = "📝 Согласие: до версионирования"
    else:
        line = f"📝 Согласие: v{html.escape(str(latest))}"
    if latest != current:
        line += f" {OLD_VERSION_MARKER}"
    return line


async def recollect_gate_on() -> bool:
    """Пересогласие показываем только если включён И модуль согласий, И сам гейт
    (`consent_recollect_enabled`, дефолт off — поведение /start без него не меняется)."""
    return (
        await get_setting_typed("consent_enabled") == "on"
        and await get_setting_typed("consent_recollect_enabled") == "on"
    )


async def outstanding_consents(
    user_id: int, entries: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Согласия из `entries` ([(label, key)]), у которых нет подписи ТЕКУЩЕЙ редакции.
    Строка до версионирования (NULL) или старой версии — считается неподписанной."""
    current = await current_consent_version()
    signed = {key for key, ver in await get_user_consent_versions(user_id) if ver == current}
    return [(label, key) for label, key in entries if key not in signed]
