"""Phase 27 (27-05, LANG-02) — перевод делегатской анкеты чата бота НА ОТПРАВКЕ.

Один шов на весь чат: `handlers/registration.py::_safe_answer` (единственная воронка отправки
вопросов, 48 вызовов, `tests/test_registration_send_guard_260816.py`) зовёт `tr_text`/`tr_kb`
из этого модуля НАПРЯМУЮ (импорт вверху `registration.py` — цикла нет, этот модуль НЕ
импортирует `handlers.registration` на уровне модуля, только лениво внутри `say()`). Пять
остальных швов анкеты (`reg_steps`, `reg_flow`, `reg_consent`, `reg_resume`, `reg_handoff`)
зовут `say()` вместо прямого `message.answer(...)` там, где отправка идёт делегату (план
27-05, Задача 2).

`reg_engine` о языках не знает (A-03, 27-CONTEXT.md) — перевод целиком живёт здесь и в
`services/i18n.py`, ядро анкеты не тронуто.

Идентичность объекта при `lang == "ru"` — ЖЁСТКОЕ требование (как и у `services.i18n.tr`):
`tr_text`/`tr_kb` отдают ТЕ ЖЕ объекты текста/разметки, а не пересобранные копии — иначе
`test_registration_send_guard_260816.py` (сверяет объекты `is`) и golden-снимки текстов бота
ловят фазу там, где её быть не должно.

Идентичность делегата берётся из `message.chat.id` (не `from_user.id`) — тот же приём, что уже
использует `_stamp_reg_step` в `registration.py` везде по проекту: в приватном чате `chat.id`
делегата совпадает и когда сообщение прислал сам делегат, И когда это `callback.message`,
автором которого formально является бот (`callback.message.from_user` — бот, не делегат).
Это snимает необходимость в трюке подмены `from_user` (`model_copy`) ради одного только
перевода — он остаётся только там, где нужен по другим причинам (mark_reg_started и т.п.).
Для `CallbackQuery` (нет своего `.chat`) идентичность берётся из `callback.from_user.id` —
тот же tapper, что везде в проекте (`record_user_consent(callback.from_user.id, ...)`).
"""
import logging
import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from services import i18n as i18n_service
# REG_LABELS (подписи сводки, "🎖 Позиция в АЙСЕК") и подписи multi-клавиатуры (_multi_kb:
# "✅ "/"▫️ " + вариант) — оба ведущий-символьный префикс поверх слова, который точечный
# словарь UI_EN/tr_map никогда не матчит целиком. Та же функция, что уже решает эту задачу для
# машинного перевода контента (services/i18n_worker.py) — переиспользуем её и здесь, а не
# заводим вторую копию регулярки.
from services.i18n_glossary import split_leading_symbols
# Задача 3 (LANG-06): канонизация английской подписи варианта в русский канон ДО
# validate_answer — reg_engine сам о языках не знает (A-03), эти две функции существуют
# РЯДОМ с ним как узкий вход (план 27-04). reg_engine НЕ импортирует handlers ни при каких
# условиях — обратного цикла нет.
from reg_engine import option_pairs, canonical_option

logger = logging.getLogger(__name__)

# `_ask_step` шлёт f"{p}{prompt}", где `p` — префикс прогресса "(3/9) " (реестр
# reg_show_progress, по умолчанию пуст). Целиком такая строка в карте переводов не найдётся —
# отделяем префикс регуляркой, переводим остаток, склеиваем обратно.
_PROGRESS_RE = re.compile(r"^(\(\d+/\d+\)\s)")


async def ctx_for(message_or_callback) -> tuple[str, dict]:
    """`(lang, tr_map)` для делегата за сообщением/колбэком. Fail-soft (D-04): любая ошибка
    (нет chat/from_user, сбой БД) -> `("ru", {})` — делегат никогда не видит ошибку из-за
    перевода."""
    try:
        chat = getattr(message_or_callback, "chat", None)
        if chat is not None:
            telegram_id = getattr(chat, "id", None)
        else:
            from_user = getattr(message_or_callback, "from_user", None)
            telegram_id = getattr(from_user, "id", None)
        if telegram_id is None:
            return "ru", {}
        return await i18n_service.context(telegram_id)
    except Exception:  # noqa: BLE001 — намеренно широкий fail-soft (D-04)
        logger.error("reg_i18n.ctx_for: сбой контекста перевода", exc_info=True)
        return "ru", {}


def tr_text(text, lang: str, tr_map: dict[str, str]):
    """Перевод текста воронки отправки. `lang == "ru"` -> ТОТ ЖЕ объект `text` (см. докстринг
    модуля). Иначе:
    1. снять префикс прогресса (если есть, `"(3/9) "`);
    2. на остатке снять ведущий эмодзи/символьный префикс (если есть, `split_leading_symbols`
       — REG_LABELS/кнопки multi-шага несут его: «🎖 Позиция в АЙСЕК», «✅ Пропустить»; точечный
       словарь `UI_EN`/`tr_map` целиком такую строку не матчит, см. докстринг модуля
       `services/i18n_glossary.py`);
    3. перевести ядро через `services.i18n.tr` (ярус A побеждает всегда, потом `tr_map`, потом
       fail-soft — русский как есть);
    4. склеить оба префикса обратно, без перевода."""
    if lang == "ru" or not isinstance(text, str) or not text:
        return text
    progress_prefix = ""
    rest = text
    match = _PROGRESS_RE.match(text)
    if match:
        progress_prefix = match.group(1)
        rest = text[len(progress_prefix):]
    symbol_prefix, core = split_leading_symbols(rest)
    translated = i18n_service.tr(core, lang, tr_map)
    if not progress_prefix and not symbol_prefix:
        return translated
    return f"{progress_prefix}{symbol_prefix}{translated}"


def _tr_button(button, lang: str, tr_map: dict[str, str]):
    """Переводит ТОЛЬКО `.text` кнопки — `callback_data`/`web_app`/`request_contact` и порядок
    не трогаются ни при каких условиях (переводится надпись, не поведение). Переиспользует
    `tr_text` (та же обработка ведущего эмодзи-префикса, что и у подписей REG_LABELS)."""
    if not isinstance(button, (KeyboardButton, InlineKeyboardButton)):
        return button
    new_text = tr_text(button.text, lang, tr_map)
    if new_text is button.text:
        return button
    return button.model_copy(update={"text": new_text})


def tr_kb(markup, lang: str, tr_map: dict[str, str]):
    """Перевод подписей клавиатуры. `lang == "ru"` или `markup is None` -> ТОТ ЖЕ объект
    (никаких пересборок). Иначе пересобрать `ReplyKeyboardMarkup`/`InlineKeyboardMarkup`,
    переведя только `text` кнопок; всё остальное (`ReplyKeyboardRemove`/`ForceReply`/т.п.) не
    трогается вовсе — эти типы не несут подписей вариантов."""
    if lang == "ru" or markup is None:
        return markup
    if isinstance(markup, ReplyKeyboardMarkup):
        new_rows = [[_tr_button(btn, lang, tr_map) for btn in row] for row in markup.keyboard]
        return markup.model_copy(update={"keyboard": new_rows})
    if isinstance(markup, InlineKeyboardMarkup):
        new_rows = [[_tr_button(btn, lang, tr_map) for btn in row] for row in markup.inline_keyboard]
        return markup.model_copy(update={"inline_keyboard": new_rows})
    return markup


async def tr_for(message_or_callback, text: str) -> str:
    """Перевод одиночной строки для мест, где `_safe_answer` не участвует (`callback.answer(...)`
    всплывающие алерты и подтверждения) — тот же контекст, что `say()`, без похода в
    `registration._safe_answer` (алерты не отправляются через `message.answer`)."""
    lang, tr_map = await ctx_for(message_or_callback)
    return tr_text(text, lang, tr_map)


def tr_fmt(text, lang: str, tr_map: dict[str, str], **subs) -> str:
    """UAT-фикс 27-05 (LANG-02): перевод ШАБЛОНА (с `{step}`/`{total}`/`{count}`-плейсхолдерами)
    СНАЧАЛА, подстановка значений ПОСЛЕ — тот же порядок, что уже был применён точечно для
    `{season}` в `registration.py::cmd_start` (Quick 260906). Нарушение порядка — реальный
    класс бага, найденный стендовым UAT: код подставлял `{step}`/`{total}` в русский шаблон
    ДО перевода (`.replace` прямо в `handlers/reg_resume.py`), из-за чего `src_hash`
    подставленной строки переставал совпадать с хешем исходного шаблона в `tr_map`, и делегат
    с `lang="en"` видел русскую кнопку/текст, хотя перевод шаблона в БД был.

    Подстановка — `.replace("{key}", str(value))` цепочкой, НЕ `.format()` (T-073-03-05: текст
    менеджера может содержать посторонние `{}`, `.format()` на них упал бы). Плейсхолдеры
    переживают машинный перевод сентинелами глоссария (`services/i18n_glossary.py`), поэтому
    после `tr_text` они остаются в переведённом тексте нетронутыми и годными для `.replace`."""
    translated = tr_text(text, lang, tr_map)
    if not isinstance(translated, str):
        return translated
    for key, value in subs.items():
        translated = translated.replace("{" + key + "}", str(value))
    return translated


async def canonicalize(message, step_key: str, text):
    """Английская (или любая другая) подпись варианта -> русский канон, ДО `validate_answer`
    (LANG-06, план 27-05 Задача 3). Общая точка для пяти мест приёма ответа в чате
    (`reg_steps._thin_step`/`_store_choice`/`process_education_status`/`process_work_status`,
    `reg_flow.process_ambassador`) — вместо инлайна `option_pairs`/`canonical_option` в каждом
    (тот же контур, вынесенный в шов, а не продублированный пять раз).

    `canonical_option` вернувший `None` — сигнал «свободный ввод» (шаги с `other_allowed`
    разрешают делегату написать свой вариант): `text` возвращается КАК ЕСТЬ, не пустой строкой.
    Не-`str`/пустой `text` идёт напрямую в `canonical_option` (там же — `None` сразу для
    не-`str`), поведение не меняется, если так и было раньше."""
    lang, tr_map = await ctx_for(message)
    pairs = await option_pairs(step_key, lang, tr_map)
    canon = canonical_option(pairs, text)
    return canon if canon is not None else text


async def say(message, text, **kwargs):
    """Обёртка для швов анкеты (`reg_steps`/`reg_flow`/`reg_consent`/`reg_resume`/
    `reg_handoff`) — замена прямому `message.answer(...)`/`callback.message.answer(...)` там,
    где отправка идёт делегату. Получает контекст по `message` (см. `ctx_for` — работает и для
    `callback.message`, идентичность через `chat.id`), переводит текст и `reply_markup`, зовёт
    `registration._safe_answer` (тот же HTML-фолбэк + обрезка + «никогда не поднимает
    исключение», что и у прямых вопросов анкеты).

    Ленивый импорт `registration` ВНУТРИ функции — `registration.py` импортирует этот модуль
    на уровне модуля (нужен `_safe_answer`/`_build_summary`), статический импорт в обратную
    сторону дал бы цикл."""
    lang, tr_map = await ctx_for(message)
    text = tr_text(text, lang, tr_map)
    if "reply_markup" in kwargs and kwargs["reply_markup"] is not None:
        kwargs["reply_markup"] = tr_kb(kwargs["reply_markup"], lang, tr_map)
    from handlers import registration

    return await registration._safe_answer(message, text, **kwargs)
