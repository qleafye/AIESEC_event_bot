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
}

_OCTET_STREAM = "application/octet-stream"


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

    return _OCTET_STREAM


__all__ = ["media_type_for"]
