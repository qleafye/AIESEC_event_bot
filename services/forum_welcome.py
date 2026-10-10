"""Идея №3 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): приветствие делегату
сразу после ПЕРВОЙ отметки входа — слушатель `services.checkin.register_first_entry_listener`
(точка расширения из коммита d08e95e, `services/checkin.py` этим модулем не тронут).

Регистрация — `register()` ниже, зовётся ОДИН раз из `main.py` при старте бота: слушатели живут
только в процессе бота (Mini App без `Bot` переносит событие в `miniapp_outbox`, бот разбирает
очередь и зовёт слушателей сам, см. докстринг `register_first_entry_listener`).

Правила отправки (координация владельца 24.09, форум-ночь):
- мастер-тумблер `forum_welcome_enabled` (per_city, дефолт ВЫКЛ, D-36) — молчим, пока менеджер
  не включил явно для города делегата.
- `source == "csv"` — выгрузку офлайн-сканера грузят ПОСТФАКТУМ, часто на следующий день или
  через несколько часов: шлём, ТОЛЬКО если скан сегодняшний (по Москве) и не старше часа.
  `approx=True` («время скана не нашлось в файле, подставлено время загрузки», D-10) — трактуем
  как «время неизвестно» и тоже молчим, даже если формально «сегодня»: подставлять делегату
  неверное время в приветствие хуже, чем промолчать.
- остальные источники (`miniapp`/`manual`/`auto_session`) — живой скан, шлём всегда (в пределах
  тумблера выше).

Приветствие — служебное сообщение о только что случившемся факте (делегат физически стоит на
стойке), не рассылка: `services.quiet_hours` НЕ участвует (тот же довод, что у служебного QR
накануне форума, D-35, — здесь тишина ещё неуместнее: отложенное на весь вечер «ты отмечен»
не несёт смысла).

Сбой отправки (нет сети/делегат заблокировал бота/сбой перевода) — fail-soft, логируется и не
пробрасывается: `services.checkin.fire_first_entry` и так ловит исключение любого слушателя, но
это НЕ повод не логировать конкретику здесь же (тот же приём, что `services/session_feedback.py::
deliver_feedback_prompts`)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from domain.cities import get_setting_typed_for_city
from services.timeutil import city_offset_hours

logger = logging.getLogger(__name__)

_MAX_CSV_AGE_MINUTES = 60


def _is_fresh_csv_scan(scanned_at: str | None, approx: bool) -> bool:
    """CSV-скан «свежий» -> можно слать: время скана известно (`approx` не выставлен),
    парсится, тот же календарный день (МСК) и не старше `_MAX_CSV_AGE_MINUTES`. Любая кривизна
    (пусто/не парсится/будущее/вчерашнее/старше часа) -> `False` — тот же fail-soft баланс, что
    у остальных парсеров дат чек-ина (`services.checkin._parse_cell_datetime` и соседи)."""
    if approx or not scanned_at:
        return False
    from services.timeutil import msk_now

    try:
        scanned = datetime.strptime(scanned_at[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    now = msk_now().replace(tzinfo=None)
    if scanned.date() != now.date():
        return False
    age_minutes = (now - scanned).total_seconds() / 60
    return 0 <= age_minutes <= _MAX_CSV_AGE_MINUTES


def _should_send(source: str, scanned_at: str | None, approx: bool) -> bool:
    if source == "csv":
        return _is_fresh_csv_scan(scanned_at, approx)
    return True  # miniapp/manual/auto_session — живой скан, время всегда «сейчас»/точное


def _format_time(scanned_at: str | None, offset_hours: int = 0) -> str:
    """«YYYY-MM-DD HH:MM:SS» -> «ЧЧ:ММ»; пустое/кривое значение (не должно случаться на живом
    источнике, но парсер CSV может отдать что угодно до фильтра `_is_fresh_csv_scan` выше) ->
    «сейчас» — плейсхолдер `{time}` подставляется всегда, текст не ломается ни при какой
    кривизне."""
    if scanned_at and len(scanned_at) >= 16:
        if offset_hours:
            # В базе время МСК, делегату показываем часы его города.
            try:
                local = datetime.strptime(scanned_at[:16], "%Y-%m-%d %H:%M") + timedelta(hours=offset_hours)
                return local.strftime("%H:%M")
            except ValueError:
                pass
        return scanned_at[11:16]
    return "сейчас"


async def _on_first_entry(bot, user_id: int, city: str | None, day: str, **kwargs) -> None:
    try:
        if await get_setting_typed_for_city("forum_welcome_enabled", city) != "on":
            return
        # Вход каждый день (24.09): хук зовётся на первый вход КАЖДОГО дня, `first_of_forum`
        # отличает настоящий первый приход (True) от повторного зова на второй день
        # двухдневного форума (False) — иначе делегат Москвы получил бы приветствие дважды.
        # Отсутствие kwarg — обратная совместимость со старым событием из outbox
        # (`services/miniapp_outbox.py`, событие могло попасть в очередь ДО того, как
        # `services.checkin` начал класть `first_of_forum` в событие) — трактуем как True,
        # чтобы не потерять приветствие для уже поставленных в очередь событий.
        if not kwargs.get("first_of_forum", True):
            return
        source = kwargs.get("source") or ""
        scanned_at = kwargs.get("scanned_at")
        approx = bool(kwargs.get("approx"))
        if not _should_send(source, scanned_at, approx):
            return

        template = await get_setting_typed_for_city("forum_welcome_text", city)
        if not (template or "").strip():
            return  # пустой текст -- молчим, тот же приём, что у соседних форумных рассылок

        # Ленивые импорты — модуль aiogram-free на уровне импорта (тот же приём, что
        # `services/session_feedback.py::deliver_feedback_prompts`), эта функция выполняется
        # ТОЛЬКО ботом.
        from services import i18n as i18n_service
        from handlers.i18n import reg_i18n

        lang, tr_map = await i18n_service.context(user_id)
        from services.text_fill import event_name, fill_event

        text = reg_i18n.tr_fmt(
            template, lang, tr_map, time=_format_time(scanned_at, await city_offset_hours(city)),
        )
        text = fill_event(text, await event_name(), lang, escape=True)
        # С приветствием приходит и главное меню: reply-клавиатуру никто не перерисовывает, а
        # кнопка «🆘 SOS» появляется только в дни форума — без этого её не было до /start.
        from keyboards.builders import get_main_menu_kb
        kb = reg_i18n.tr_kb(await get_main_menu_kb(user_id), lang, tr_map)
        await bot.send_message(user_id, text, reply_markup=kb)
    except Exception as e:
        logger.error(
            "forum_welcome: не удалось отправить приветствие %s: %s: %s",
            user_id, type(e).__name__, e,
        )


def register() -> None:
    """Подписывает слушателя приветствия на `services.checkin.register_first_entry_listener` —
    идемпотентно (сама функция-регистратор уже проверяет `if fn not in _first_entry_listeners`),
    повторный вызов (рестарт, тест) не даёт дубля отправки."""
    from services.checkin import register_first_entry_listener

    register_first_entry_listener(_on_first_entry)
