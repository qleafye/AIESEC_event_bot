"""Опросы (native Telegram polls): доставка, закрытие, итоги, выгрузка в таблицу.

Без aiogram-хендлеров — сюда ходят и админка (handlers/admin_polls.py), и джоба планировщика
(services/scheduler.py::send_scheduled_poll), и тесты. Бот передаётся параметром (как
`_safe_send` в scheduler), поэтому модуль не держит своих глобалов.

Модель: каждому делегату уходит ОТДЕЛЬНЫЙ Telegram-опрос (send_poll в личку) — у каждого свой
`telegram_poll_id`, свои счётчики. `poll_messages` хранит их все: это и карта «ответ → наш
опрос», и цели для stop_poll, и чекпоинт доставки (дошлёт после рестарта пропускает чаты,
уже записанные — та же схема, что scheduled_broadcast_deliveries у рассылок).
"""
import asyncio
import html as html_module
import logging
from datetime import datetime

from database.db import (
    get_poll,
    claim_poll_sending,
    set_poll_status,
    record_poll_message,
    list_poll_sent_chat_ids,
    list_poll_messages,
    list_poll_answers,
    list_polls,
    get_poll_results,
    get_all_users_ids,
    count_and_list_filtered,
    _csv_safe,
)
from settings_schema import get_setting_typed, SETTINGS_SCHEMA

logger = logging.getLogger(__name__)

# Лимиты Bot API для send_poll — проверяются в мастере ДО отправки, чтобы менеджер получил
# понятную подсказку, а не «Bad Request: POLL_QUESTION_TOO_LONG» уже на рассылке.
POLL_QUESTION_MAX = 300
POLL_OPTION_MAX = 100
POLL_OPTIONS_MIN = 2
POLL_OPTIONS_MAX = 10

POLLS_SHEET_HEADERS = ["Дата", "Опрос", "Telegram ID", "Делегат", "Username", "Город", "Ответ"]


# ── аудитория ────────────────────────────────────────────────────────────────────────────────

async def resolve_poll_audience(poll: dict) -> list[int]:
    """telegram_id получателей по audience (filter_spec рассылок). [] в spec = все
    пользователи. Город в spec пере-резолвится по живому реестру (как у рассылок, WR-02):
    неизвестный код → пустая аудитория, а не молчаливый редирект в город по умолчанию."""
    spec = poll.get("audience") or []
    if not spec:
        return await get_all_users_ids()
    from cities import refresh_city_filter_spec  # cities импортирует database.db — цикла нет
    spec = refresh_city_filter_spec(spec)
    if spec is None:
        logger.error("poll %s targets an unknown event_city — refusing to send", poll.get("id"))
        return []
    return await count_and_list_filtered(spec)


def audience_label(spec: list[dict] | None) -> str:
    """Человеческая подпись аудитории для карточки/списка."""
    if not spec:
        return "все делегаты"
    parts = []
    for f in spec:
        field, value = f.get("field"), f.get("value")
        if field == "event_city":
            parts.append(f"город: {f.get('label') or value}")
        elif field == "status":
            parts.append({"approved": "только одобренные"}.get(value, f"статус: {value}"))
        elif field == "participant_type":
            parts.append(f"трек: {f.get('label') or value}")
        else:
            parts.append(f"{field} = {value}")
    return ", ".join(parts)


# ── доставка ─────────────────────────────────────────────────────────────────────────────────

async def _send_one(bot, poll: dict, chat_id: int, intro: str | None):
    """Одна доставка: вступление (если задано) + сам опрос. Возвращает Message опроса."""
    if intro:
        try:
            await bot.send_message(chat_id, intro)
        except Exception as e:
            # Вступление — украшение; сам опрос важнее. Логируем и шлём опрос.
            logger.warning("poll %s intro to %s failed: %s", poll["id"], chat_id, e)
    return await bot.send_poll(
        chat_id,
        question=poll["question"],
        options=list(poll["options"]),
        is_anonymous=bool(poll["is_anonymous"]),
        allows_multiple_answers=bool(poll["allows_multiple"]),
    )


async def deliver_poll(bot, poll_id: int) -> dict | None:
    """Разослать опрос его аудитории. Идемпотентно: клейм 'scheduled' → 'sending' (второй
    вызов выходит сразу), каждый чат чекпоинтится в poll_messages, повторный прогон после
    краха/рестарта шлёт только хвост. В конце — 'open'. Возвращает счётчики или None, если
    клейм не удался."""
    poll = await get_poll(poll_id)
    if poll is None:
        return None
    if not await claim_poll_sending(poll_id):
        return None
    try:
        targets = await resolve_poll_audience(poll)
        already = await list_poll_sent_chat_ids(poll_id)
        intro = (await get_setting_typed("poll_intro_text") or "").strip() or None
        sent = failed = skipped = 0
        for chat_id in targets:
            if chat_id in already:
                skipped += 1
                continue
            # Ловим только ошибку ОТПРАВКИ (заблокировал бота / удалённый аккаунт / 400) — она
            # фиксируется как failed, чтобы при дошлёте не долбить чат снова (как
            # list_delivered_chat_ids у рассылок). Ошибка записи чекпоинта — это уже крах
            # процесса: она уходит наружу, строка остаётся 'sending', бут дошлёт хвост.
            try:
                msg = await _send_one(bot, poll, chat_id, intro)
            except Exception as e:
                logger.warning("poll %s to %s failed: %s", poll_id, chat_id, e)
                await record_poll_message(poll_id, chat_id, None, None, False)
                failed += 1
            else:
                tg_poll_id = getattr(getattr(msg, "poll", None), "id", None)
                await record_poll_message(poll_id, chat_id, tg_poll_id, getattr(msg, "message_id", None), True)
                sent += 1
            await asyncio.sleep(0.05)
        await set_poll_status(poll_id, "open")
        logger.info(
            "poll %s delivered: sent %s, skipped %s (already), failed %s of %s",
            poll_id, sent, skipped, failed, len(targets),
        )
        return {"sent": sent, "failed": failed, "skipped": skipped, "total": len(targets)}
    except Exception as e:
        # Строка остаётся 'sending' — реконсиляция на буте реклеймит её и дошлёт хвост.
        logger.error("deliver_poll(%s) failed mid-way: %s", poll_id, e)
        return None


async def close_poll(bot, poll_id: int) -> tuple[int, int]:
    """stop_poll на каждом доставленном сообщении, fail-soft по каждому (делегат мог удалить
    чат — остальным это не мешает). Статус → 'closed' в любом случае: ответы на уже
    остановленных опросах Telegram не принимает, а на недоступных — некому. (ok, failed)."""
    ok = failed = 0
    for m in await list_poll_messages(poll_id):
        try:
            await bot.stop_poll(m["chat_id"], m["message_id"])
            ok += 1
        except Exception as e:
            logger.warning("stop_poll poll=%s chat=%s: %s", poll_id, m["chat_id"], e)
            failed += 1
        await asyncio.sleep(0.05)
    await set_poll_status(poll_id, "closed")
    return ok, failed


# ── отображение ──────────────────────────────────────────────────────────────────────────────

def _bar(count: int, total: int, width: int = 10) -> str:
    filled = round(width * count / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def render_results_text(results: dict) -> str:
    """HTML-текст итогов для карточки опроса: полоски по вариантам + сводка."""
    poll = results["poll"]
    counts = results["counts"]
    total_votes = sum(counts)
    lines = []
    for text, n in zip(poll["options"], counts):
        pct = round(100 * n / total_votes) if total_votes else 0
        lines.append(f"{_bar(n, total_votes)} {n} ({pct}%) — {html_module.escape(str(text))}")
    who = "итоги Telegram (анонимный — без имён)" if poll["is_anonymous"] else "по людям"
    summary = (
        f"Ответили: <b>{results['respondents']}</b> из {results['delivered']} получивших "
        f"({who})"
    )
    if results.get("failed"):
        summary += f", не доставлено: {results['failed']}"
    return "\n".join(lines) + ("\n\n" if lines else "") + summary


# ── выгрузка в таблицу ───────────────────────────────────────────────────────────────────────

def _fmt_date(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(raw)


async def build_polls_sheet_rows(poll_ids: list[int] | None = None) -> list[list]:
    """Строки вкладки «Опросы». Неанонимный опрос — строка на ответ (делегат + выбранные
    варианты через «; »). Анонимный — строка на вариант с числом голосов (людей Telegram не
    отдаёт). `poll_ids=None` — все опросы, вкладка перезаписывается целиком (идемпотентно)."""
    polls = await list_polls()
    if poll_ids is not None:
        polls = [p for p in polls if p["id"] in set(poll_ids)]
    from cities import city_label  # cities импортирует database.db, обратной зависимости нет
    rows: list[list] = []
    for poll in sorted(polls, key=lambda p: p["id"]):
        q = poll["question"]
        if poll["is_anonymous"]:
            results = await get_poll_results(poll["id"])
            stamp = _fmt_date(poll.get("closed_at") or poll.get("created_at"))
            for text, n in zip(poll["options"], results["counts"]):
                rows.append([stamp, q, "", "— анонимный опрос —", "", "", f"{text}: {n}"])
            continue
        for a in await list_poll_answers(poll["id"]):
            chosen = "; ".join(
                str(poll["options"][i]) for i in a["option_ids"] if 0 <= i < len(poll["options"])
            )
            city = await city_label(a["event_city"]) if a.get("event_city") else ""
            rows.append([
                _fmt_date(a.get("answered_at")), q, a["user_id"],
                a.get("full_name") or "", a.get("username") or "", city, chosen,
            ])
    return [[_csv_safe(v) for v in r] for r in rows]


async def export_polls_to_sheet() -> int:
    """Полная перезапись вкладки (название — настройка polls_sheet_tab). Возвращает число
    строк или -1, если таблица недоступна/не настроена — вызывающий показывает менеджеру
    «таблица недоступна, результаты в боте»."""
    from services.sheets import sync_named_worksheet  # sheets тянет gspread — держим лениво
    tab = (await get_setting_typed("polls_sheet_tab") or "").strip() or SETTINGS_SCHEMA["polls_sheet_tab"]["default"]
    try:
        rows = await build_polls_sheet_rows()
        return await sync_named_worksheet(tab, POLLS_SHEET_HEADERS, rows)
    except Exception as e:
        logger.error("export_polls_to_sheet failed: %s", e)
        return -1
