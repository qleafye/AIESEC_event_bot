"""UAT 07.09 (T-d6t-03): `tg_media.media_type_for` — единственное правило «настоящий тип
файла Telegram». Чистая функция, ни БД, ни сети — параметризованная таблица кейсов.
"""
from __future__ import annotations

import pytest

import tg_media


@pytest.mark.parametrize(
    "content_type, file_path, expected",
    [
        ("application/octet-stream", "photos/file_42.jpg", "image/jpeg"),
        ("application/octet-stream", "photos/file_42.JPG", "image/jpeg"),
        ("application/octet-stream", "photos/file_42.png", "image/png"),
        ("", "photos/file_42.jpg", "image/jpeg"),
        (None, None, "application/octet-stream"),
        ("image/webp", "photos/file_42.jpg", "image/webp"),
        ("application/pdf", "photos/file_42.jpg", "application/pdf"),
        ("application/octet-stream", "documents/cv.pdf", "application/pdf"),
        ("application/octet-stream", "documents/file_noext", "application/octet-stream"),
        # Живой ответ getFile для фото из настроек (логотип Mini App, 10.09.2026): бакет
        # photos/ и НИКАКОГО расширения. Без этой ветки тип оставался октет-стримом, и с
        # `nosniff` браузер отказывался рисовать логотип в шапке приложения.
        ("application/octet-stream", "photos/file_2", "image/jpeg"),
        ("", "photos/file_2", "image/jpeg"),
        (None, "photos/file_2", "image/jpeg"),
        # Правило про бакет — только для фото и только при отсутствии расширения: чужой
        # бакет без расширения по-прежнему октет-стрим (строка выше), а осмысленный
        # заголовок Telegram важнее правила.
        ("image/webp", "photos/file_2", "image/webp"),
    ],
)
def test_media_type_for(content_type, file_path, expected):
    assert tg_media.media_type_for(content_type, file_path) == expected
