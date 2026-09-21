"""Резолвер настоящего типа файла Telegram — корневой aiogram-free модуль (прецедент
`web_theme.py`, `settings_ops.py`, `reg_labels.py`). Импорт только `mimetypes` и
`PurePosixPath`, никаких зависимостей проекта.

Зачем: файловый сервер Telegram отдаёт фотографии с `content-type: application/octet-stream`,
а оба файловых прокси (`miniapp/routers/files.py`, `dashboard/files.py`) ставят
`X-Content-Type-Options: nosniff` — из-за этой комбинации браузер отказывается рисовать
`<img>` с честным заголовком октет-стрима. Наружу по-прежнему уходит только ТИП: сам
`file_path`, из которого он угадан, не возвращается клиенту и не логируется — тот же приём,
что `miniapp/routers/files.py::_download_name`.
"""
from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

# Явная карта расширений картинок — не украшательство: `mimetypes.guess_type` на Windows
# подтягивает типы из реестра HKCR, и на части машин для `.jpg` отдаёт не `image/jpeg`,
# а `image/pjpeg` — тест стал бы плавающим по машинам. Проверяем эту карту СНАЧАЛА и только
# потом падаем на `mimetypes.guess_type`.
_IMAGE_EXT_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    # Квик 260921: фавикон дашборда — ICO та же история, что и остальные записи карты —
    # `mimetypes.guess_type` угадывает и без неё на большинстве платформ, но не гарантированно
    # везде одинаково, фиксируем явно.
    ".ico": "image/x-icon",
}

_OCTET_STREAM = "application/octet-stream"

# Бакет фотографий на файловом сервере Telegram. Живой ответ getFile для фото из настроек
# (логотип Mini App, 10.09.2026): `file_path = "photos/file_2"` — РАСШИРЕНИЯ НЕТ ВООБЩЕ,
# поэтому ни карта выше, ни `mimetypes.guess_type` типа не дают, и с `nosniff` браузер
# отказывается рисовать `<img>`. Всё, что Telegram кладёт в этот бакет, — фотографии, а
# фотографии он хранит в JPEG (анимации и стикеры лежат в других бакетах со своими
# расширениями и сюда не попадают).
_PHOTOS_BUCKET = "photos/"


def media_type_for(content_type: str | None, file_path: str | None) -> str:
    """Заголовок непустой и НЕ `application/octet-stream` — вернуть его как есть (регистр
    и параметры не трогаем: осмысленный тип Telegram, например `image/webp` или
    `application/pdf`, важнее любой догадки). Иначе — угадать по расширению `file_path`
    (сначала своя карта картинок, потом `mimetypes.guess_type`); не угадалось —
    `application/octet-stream`."""
    if content_type and content_type != _OCTET_STREAM:
        return content_type

    ext = PurePosixPath(file_path or "").suffix.lower()
    if ext in _IMAGE_EXT_MAP:
        return _IMAGE_EXT_MAP[ext]

    guessed, _ = mimetypes.guess_type(file_path or "")
    if guessed:
        return guessed

    # Фото без расширения — последняя ветка, а не первая: реальное расширение (если Telegram
    # его всё же прислал) всегда важнее правила про бакет.
    if not ext and (file_path or "").startswith(_PHOTOS_BUCKET):
        return "image/jpeg"

    return _OCTET_STREAM


__all__ = ["media_type_for"]
