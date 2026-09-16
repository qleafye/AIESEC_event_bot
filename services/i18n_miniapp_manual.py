"""Задача «делегатский интерфейс Mini App на английском» (после Phase 27) — ручной английский
для делегатских текстов, которые физически лежат ВНЕ корпуса анкеты (`services/i18n_sources.py`
::DELEGATE_GROUPS = reg_prompts/reg/party, LANG-08): группы `miniapp`/`game`/`event`, статусные
литералы `miniapp/routers/tasks.py`. `tr()` (`services/i18n.py`) ищет перевод по хешу
СОДЕРЖИМОГО (`src_hash`), а не по группе реестра — запись в `translations` с `manual=1` здесь
работает для ЛЮБОГО текста, независимо от того, видит ли его корпус/машинный воркер (план 27-03).

Почему не `i18n_ui_en.py` (ярус A): тот словарь — для строк, участвующих в жёстких сравнениях
aiogram-фильтров (докстринг `i18n_ui_en.py`), не для контента интерфейса. Здесь — ярус B,
записанный вручную, а не выучен машиной (`manual=1` — `services/i18n_worker.py::drain()` эти
строки не трогает, LANG-05).

Владелец (17.09, приёмка): «машинные переводы плохие» — часть строк из ГРУППЫ `reg_prompts`
(корпус анкеты) тоже уже прогнана машиной с посредственным качеством. `MANUAL_EN` НИЖЕ поэтому
включает и несколько ключевых строк анкеты, явно названных в приёмке (карточка «Образование»,
вилки города/трека, «Продолжить в чате», обзор перед отправкой) — `seed()` перезаписывает их
машинный перевод РУЧНЫМ (тот же `upsert_translation(manual=1)`, что и остальной ярус B), не
трогая при этом РЕАЛЬНУЮ правку менеджера (см. докстринг `seed()`).

Формат: RU-текст БАЙТ-В-БАЙТ (после `.strip()`, как везде — `services.i18n.src_hash`) -> EN.
Плейсхолдеры (`{count}`, `{дата}`, `{имя}`, `{город}`, `{entity}`, `{n}`, `{max}`, `{limit}`
и т.п.) переносятся ДОСЛОВНО — вызывающий код делает `.format()`/`.replace()` ПОСЛЕ `tr()`
(см. `miniapp/routers/hub.py`/`tasks.py`), значит английский текст обязан нести те же самые
токены, что и русский, иначе подстановка не сработает.
"""
from __future__ import annotations

import logging

from database.db import get_translation, upsert_translation
from services.i18n import src_hash

logger = logging.getLogger(__name__)

# Маркер происхождения — отличает НАШ сид от настоящей ручной правки менеджера (план 27-06,
# экран правки перевода): `seed()` не перезаписывает строку, если она уже `manual=1` с ДРУГИМ
# `origin_key` (менеджер её редактировал сам через экран правки).
ORIGIN = "miniapp_manual_seed"


# ── Хаб, статус заявки, оплата (miniapp_hub_*, reg_status_*, group=miniapp/reg_prompts) ────
_HUB_STATUS = {
    "Твой баланс": "Your balance",
    "монет": "coins",
    "{done} из {total} заданий сдано": "{done} of {total} tasks done",
    "{days} дней до форума": "{days} days to the forum",
    "Следующее": "Up next",
    "Разделы": "Sections",
    "⏳ Анкета на проверке": "⏳ Application under review",
    "Заявку получили. Ответ придёт сюда же, в чат с ботом, обычно за {days} дн.": (
        "We got your application. The answer will come right here, in the chat with the bot — "
        "usually within {days} days."
    ),
    "❌ Заявка отклонена": "❌ Application rejected",
    "Решение организаторов пришло в чат с ботом. Анкету можно поправить и подать заново.": (
        "The organizers' decision is in the chat with the bot. You can fix the form and submit "
        "it again."
    ),
    "Подать заново": "Submit again",
    "Причина: {reason}": "Reason: {reason}",
    "Активные задания": "Active tasks",
    "Твоё место": "Your place",
    "из {total}": "of {total}",
    # ── reg_status_* (обзор экрана #/status, группа reg_prompts — тоже перезаписываем ──────
    "Заявка на проверке": "Application under review",
    "Мы получили твою заявку": "We got your application",
    "Обычно отвечаем за день. Напишем сюда же, в чат, — следить за экраном не нужно.": (
        "We usually reply within a day. We'll write right here, in the chat — no need to watch "
        "the screen."
    ),
    "Заявка отклонена": "Application rejected",
    "В этот раз не получилось": "It didn't work out this time",
    "Это не навсегда: можно поправить анкету и подать её заново.": (
        "It's not forever — you can fix the form and submit it again."
    ),
    "Заявка одобрена": "Application approved",
    "Ты в деле, {имя}!": "You're in, {имя}!",
    "Осталось оплатить участие — и увидимся {дата} в {город}.": (
        "All that's left is to pay for participation — see you {дата} in {город}."
    ),
    "Что дальше": "What's next",
    "Изменить анкету": "Edit application",
    "Оплатить участие": "Pay for participation",
    "Причина": "Reason",
    "Что поправить": "What to fix",
    "Остальные ответы сохранены": "The rest of your answers are saved",
    "Поправить и подать заново": "Fix and submit again",
    "Отклонена · поправь и подай снова": "Rejected · fix and submit again",
    "Одобрена · оплати до {дата}": "Approved · pay by {дата}",
    "Оплата до {дата}": "Payment due {дата}",
    "Напомним за 3 дня и за день до срока.": "We'll remind you 3 days and 1 day before the deadline.",
}

# ── Задания (game_*, miniapp_task_*) ────────────────────────────────────────────────────────
_TASKS = {
    "Пришли скриншот/фото:": "Send a screenshot/photo:",
    "Пришли файл (PDF):": "Send a file (PDF):",
    "Напиши текстом:": "Write it as text:",
    "Пришли ссылку:": "Send a link:",
    "Пришли подтверждение:": "Send proof:",
    "Когда всё прислал — нажми «✅ Готово».": "Once you've sent everything, tap «✅ Done».",
    "✅ Готово": "✅ Done",
    "Сначала пришли хотя бы одну часть — фото, файл, текст или ссылку.": (
        "First send at least one part — a photo, file, text, or link."
    ),
    "Принято! Менеджер проверит и начислит монеты.": "Accepted! A manager will review it and award coins.",
    "Частей: {count} · {breakdown}": "Parts: {count} · {breakdown}",
    "🗑 Убрать последнее": "🗑 Remove last",
    "Лёгкое": "Easy",
    "Среднее": "Medium",
    "Сложное": "Hard",
    "Реферальное": "Referral",
    "Особое": "Special",
    "Активных заданий сейчас нет. Загляни попозже!": "No active tasks right now. Check back later!",
    "стр. {page}/{total}": "page {page}/{total}",
    "Статус: {status}": "Status: {status}",
    "📷 Скриншот/фото": "📷 Screenshot/photo",
    "📄 PDF": "📄 PDF",
    "✍️ Текст": "✍️ Text",
    "🔗 Ссылка": "🔗 Link",
    "не важно": "any type",
    "⏰ Срок вышел — отправить можно, монеты решит менеджер": (
        "⏰ Deadline passed — you can still submit, a manager will decide on the coins"
    ),
    "Что сделать": "What to do",
    "Нужно прислать": "What to send",
    "На присланном должно быть видно, что задание выполнено.": (
        "Whatever you send should clearly show the task is done."
    ),
    "осталось {days} дн.": "{days} days left",
    "менеджер, обычно за день": "a manager, usually within a day",
    "на проверке": "under review",
    "принято (+{n}🪙)": "accepted (+{n}🪙)",
    "новое": "new",
    "новое · попытка {n} из {limit}": "new · attempt {n} of {limit}",
}

# ── Монеты и рейтинг ─────────────────────────────────────────────────────────────────────
_COINS = {
    "вручную": "manually",
    "задание": "task",
    "Пока не было ни одной операции.": "No transactions yet.",
    "🏆 Рейтинг по монетам": "🏆 Coin leaderboard",
    "Пока ни у кого нет монет.": "Nobody has coins yet.",
    "Твоё место: {rank} · баланс: {balance}": "Your place: {rank} · balance: {balance}",
    "📜 История монет": "📜 Coin history",
}

# ── FAQ, профиль, финальный экран анкеты, ошибки ────────────────────────────────────────────
_MISC = {
    "Пока здесь пусто. Напиши свой вопрос — ответим и добавим сюда.": (
        "Nothing here yet. Ask your question — we'll answer and add it here."
    ),
    "Контакты": "Contacts",
    "Анкета": "Application",
    "{filled} из {total} вопросов": "{filled} of {total} questions",
    "Отправлена {date}": "Submitted {date}",
    "изменена {date}": "edited {date}",
    "одобрена {date}": "approved {date}",
    "Анкету видит только менеджер АЙСЕК. Город и трек после одобрения не меняются.": (
        "Only an AIESEC manager sees your application. City and track can't be changed after "
        "approval."
    ),
    "Привет!": "Hi!",
    "Приводи друзей в АЙСЕК": "Bring friends to AIESEC",
    "Каждый, кто зарегистрируется по твоей ссылке, будет засчитан тебе как приглашённый.": (
        "Everyone who signs up with your link will be counted as invited by you."
    ),
    "Хочу свою ссылку": "I want my own link",
    "Позже": "Later",
    "Твоя ссылка": "Your link",
    "Скопировать": "Copy",
    "Скопировано": "Copied",
    "На главную": "Home",
    "Файл больше 20 МБ — пришлите его через бота.": "File is larger than 20 MB — send it via the bot.",
    "Не удалось загрузить данные. Проверьте связь и нажмите «Повторить».": (
        "Couldn't load the data. Check your connection and tap «Retry»."
    ),
    "Повторить": "Retry",
    "Не дошло до сервера. Проверьте связь и попробуйте ещё раз.": (
        "Didn't reach the server. Check your connection and try again."
    ),
    "Задание уже отправлено и ждёт проверки — решение придёт в бот.": (
        "The task has already been submitted and is awaiting review — the decision will come to "
        "the bot."
    ),
    "Это задание уже принято.": "This task has already been accepted.",
    "Попытки по этому заданию закончились. Напишите менеджеру, если это ошибка.": (
        "You're out of attempts for this task. Message a manager if this looks wrong."
    ),
    # Онбординг главного экрана
    "Делаешь задания — получаешь монеты.": "Complete tasks — earn coins.",
    "Погнали": "Let's go",
    "Как это работает": "How it works",
    "Заполни анкету — 14 вопросов, минут на пять. Черновик сохраняется сам.; "
    "Делай задания — сторис, фото, знакомства с делегатами из других городов.; "
    "Копи монеты — на форуме обменяешь их в CC-shop.": (
        "Fill out the application — 14 questions, about five minutes. Your draft saves itself.; "
        "Complete tasks — stories, photos, meeting delegates from other cities.; "
        "Collect coins — trade them in at the CC-shop during the forum."
    ),
}

# ── Первые экраны анкеты (согласия/город/трек/образование), обзор перед отправкой ──────────
_FORM_INTRO = {
    "Выбери город мероприятия:": "Choose the event city:",
    "Выбери формат участия:": "Choose your participation format:",
    "Продолжить в чате": "Continue in chat",
    "💬 Анкета сейчас открыта в чате с ботом. Можно продолжить там или забрать её сюда.": (
        "💬 The application is currently open in the chat with the bot. You can continue there "
        "or bring it back here."
    ),
    "📱 Забрать сюда": "📱 Bring it here",
    "Дальше": "Next",
    "Назад": "Back",
    "Где и на чём учишься": "Where and what you're studying",
    "Сейчас учусь здесь": "Currently studying here",
    "Уже не учусь здесь": "No longer studying here",
    "Выключи, если уже закончил(а)": "Turn off if you've already graduated",
    "Включи, если ещё студент(ка)": "Turn on if you're still a student",
    "Хорошо — про учёбу больше не спросим.": "Got it — we won't ask about studies anymore.",
    "Начни вводить — подскажем. Например: «спб», «вшэ», «политех».": (
        "Start typing — we'll suggest options. For example: «spb», «hse», «polytech»."
    ),
    "Впиши {entity} сам — менеджер увидит его как есть.": (
        "Type your own {entity} — the manager will see it exactly as you wrote it."
    ),
    "Другой {entity}": "Other {entity}",
}


MANUAL_EN: dict[str, str] = {
    **_HUB_STATUS, **_TASKS, **_COINS, **_MISC, **_FORM_INTRO,
}


async def seed(lang: str = "en") -> dict:
    """Пишет `MANUAL_EN` в `translations` с `manual=1` — идемпотентно, безопасно звать на
    каждом старте бота (план подсказывает именно так, следом за `seed_cities_if_empty()` в
    `main.py`). Пропускает строку, если в базе на неё уже есть `manual=1` перевод НЕ с нашим
    `origin_key` (значит менеджер отредактировал её сам через будущий экран правки, план 27-06
    — эта функция не имеет права затирать чужую ручную работу, LANG-05 действует и для сида).

    Возвращает `{"applied": N, "skipped_manager_edit": M}` — для лога старта."""
    applied = 0
    skipped = 0
    for ru_text, en_text in MANUAL_EN.items():
        text_hash = src_hash(ru_text)
        existing = await get_translation(lang, text_hash)
        if existing and existing.get("manual") and existing.get("origin_key") != ORIGIN:
            skipped += 1
            continue
        await upsert_translation(lang, text_hash, ru_text, en_text, manual=1, origin_key=ORIGIN)
        applied += 1
    if skipped:
        logger.info(
            "i18n_miniapp_manual.seed: пропущено %d строк — уже отредактированы менеджером", skipped,
        )
    return {"applied": applied, "skipped_manager_edit": skipped}
