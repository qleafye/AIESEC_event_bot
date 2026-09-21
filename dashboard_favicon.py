"""Квик 260921: своя иконка вкладки браузера ДАШБОРДА статистики — корневой aiogram-free
модуль (тот же приём, что `tg_media.py`/`web_theme.py`): и `handlers/admin_settings.py`
(приём документа в чате), и `handlers/admin_miniapp_theme.py` (кнопка/подсказка на экране
«🎭 Пресеты и ручки») читают отсюда одни и те же тексты и правила, второй копии нет.

НЕ путать с `miniapp_logo` (лого в шапке Mini App) — это отдельный ключ реестра для отдельной
поверхности (favicon дашборда), см. `dashboard/main.py::_favicon_url`.

Правило T-19.1-25 у остальных ручек оформления («только фото, файл — отказ») здесь
ПЕРЕВЁРНУТО: Telegram пережимает фото в JPEG и теряет прозрачность/чёткость маленькой
иконки, поэтому сюда принимается ТОЛЬКО документ (`message.document`), а фото отклоняется с
объяснением, как прислать картинку файлом.
"""
from __future__ import annotations

SETTING_KEY = "dashboard_favicon"
LABEL = "🔖 Иконка вкладки дашборда"

# 256 КБ с большим запасом для иконки 32×32/64×64 (типичный PNG такого размера — единицы КБ).
MAX_BYTES = 256 * 1024

UPLOAD_PROMPT_HTML = (
    f"🔖 <b>{LABEL}</b>\n\n"
    "Пришлите маленькую квадратную картинку — например 32×32 или 64×64 — PNG (можно с "
    "прозрачным фоном), <b>файлом, а не фото</b>: Telegram сжимает фото в JPEG, и мелкая "
    "иконка теряет прозрачность и чёткость.\n\n"
    "На телефоне: скрепка 📎 → «Файл»/«Документ» → выберите картинку (НЕ через галерею "
    "«Фото»). Если телефон всё равно предлагает только «Фото» — сначала отправьте картинку "
    "себе в «Избранное» файлом с компьютера или через файловый менеджер."
)

PHOTO_REJECT_MESSAGE = (
    "Это пришло как фото — Telegram уже сжал его в JPEG, для маленькой иконки вкладки так "
    "не подойдёт. Пришлите тот же файл через скрепку 📎 → «Файл»/«Документ», не через "
    "«Фото» — тогда без сжатия."
)

MIME_REJECT_MESSAGE = (
    "Нужен именно PNG (или ICO) — пришлите его документом (скрепка 📎 → «Файл»)."
)

SIZE_REJECT_MESSAGE = (
    f"Файл слишком большой (максимум {MAX_BYTES // 1024} КБ для маленькой иконки) — "
    "сожмите и пришлите заново."
)

SUCCESS_MESSAGE = "✅ Иконка вкладки дашборда обновлена!"

_PNG_MIME = {"image/png"}
_ICO_MIME = {"image/x-icon", "image/vnd.microsoft.icon"}


def is_acceptable_document(mime_type: "str | None", file_name: "str | None") -> bool:
    """PNG или ICO — по mime-типу ИЛИ по расширению имени файла (телефоны нередко шлют
    `application/octet-stream` для .ico, реальный тип потом переопределяет `tg_media` на
    стороне раздачи — здесь достаточно не пустить явно постороннее)."""
    mime = (mime_type or "").lower()
    name = (file_name or "").lower()
    if mime in _PNG_MIME or name.endswith(".png"):
        return True
    if mime in _ICO_MIME or name.endswith(".ico"):
        return True
    return False


def is_too_large(file_size: "int | None") -> bool:
    return file_size is not None and file_size > MAX_BYTES
