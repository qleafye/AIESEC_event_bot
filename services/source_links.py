"""Ссылка с меткой (`https://t.me/<бот>?start=src_<метка>`): проверка метки и тексты ответа.

Одна логика на два входа — команду `/create_link <метка>` (handlers/admin.py) и кнопку
«➕ Новая ссылка» на экране «🔗 Ссылки с метками» (handlers/applications/admin_source_links.py). Модуль
без aiogram: только строки, тестируется без бота.
"""
import html
import re

# Метка едет в deep-link как `?start=src_<метка>`, а Telegram разрешает в этом параметре
# только латиницу, цифры, «_» и «-» (всего 64 символа, из них 4 занимает префикс `src_`).
# Всё остальное молча ломает ссылку, поэтому проверяем до отправки, а не после.
SOURCE_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")


def clean_tag(raw: str) -> str:
    """Реальный случай 14.08: менеджер скопировал формат из справки ВМЕСТЕ с угловыми скобками
    («/create_link <инфо ВК>»). Скобки попадали в HTML-ответ неэкранированными, Telegram видел
    «<инфо» как открывающий тег и отклонял ВСЁ сообщение — человек не получал ни ссылки, ни
    ошибки. Снимаем скобки молча (намерение очевидно), а остальное объясняем словами."""
    raw = raw.strip()
    return raw[1:-1].strip() if raw.startswith("<") and raw.endswith(">") else raw


def is_valid_tag(tag: str) -> bool:
    return bool(SOURCE_TAG_RE.match(tag))


def bad_tag_text(raw: str, example: str) -> str:
    """Объяснение, что не так с меткой. `example` — готовый пример ввода для этого входа
    (`/create_link vk_poster` у команды, `vk_poster` у кнопки)."""
    return (
        "⚠️ Метка подставляется прямо в ссылку, поэтому в ней можно использовать только "
        "латинские буквы, цифры, «_» и «-» — без пробелов, русских букв и знаков.\n\n"
        f"Вы прислали: <code>{html.escape(raw.strip())}</code>\n"
        f"Например, для афиши во ВКонтакте: <code>{html.escape(example)}</code>"
    )


def build_link(bot_username: str, tag: str) -> str:
    return f"https://t.me/{bot_username}?start=src_{tag}"


def link_reply_text(tag: str, link: str, where: str = "«📈 Источники»") -> str:
    return (
        f"🔗 Ссылка с меткой <b>{html.escape(tag)}</b>:\n\n"
        f"<code>{html.escape(link)}</code>\n\n"
        f"Регистрации по этой ссылке появятся в разделе {where}."
    )
