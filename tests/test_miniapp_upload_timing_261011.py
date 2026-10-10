"""Приёмка 10.10: фото к заданию в Mini App грузилось очень долго, а по логам нельзя было
понять, где теряется время. Теперь `POST /app/api/uploads` пишет ОДНУ строку INFO в конце
обработки: размер файла, приём тела (`read_ms`), отправка в Telegram (`telegram_ms`), всего
(`total_ms`) и итоговый код ответа — и на успехе, и на отказе.

Сжатие фото — на клиенте (`miniapp/static/js/photo_shrink.js`); сервер принимает уже
ужатый JPEG тем же маршрутом, что и оригинал.
"""
from __future__ import annotations

import logging
import re

from tests.test_miniapp_routes import ADMIN_ID
from tests.test_miniapp_submissions import (  # noqa: F401 — фикстура client
    DELEGATE_ID,
    _upload,
    client,
)
from tests.test_miniapp_upload_photo_fallback_260927 import _reject, api  # noqa: F401 — фикстура api

LOGGER = "miniapp.routers.submissions"
LINE_RE = re.compile(
    r"^uploads: target=(?P<target>\S+) content_type=(?P<ctype>\S+) ext=(?P<ext>\S+) size=(?P<size>\S+) "
    r"kind=(?P<kind>\S+) status=(?P<status>\d+) read_ms=(?P<read>\S+) telegram_ms=(?P<tg>\S+) total_ms=(?P<total>\d+)$"
)


def _upload_lines(caplog) -> list[re.Match]:
    lines = [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]
    return [m for m in (LINE_RE.match(line) for line in lines) if m]


def test_compressed_cover_is_accepted_as_photo_and_logged_once_with_timings(client, api, caplog):
    """Клиент прислал уже ужатый JPEG (имя с .jpg, image/jpeg) — обложка уходит фото, в логе
    ровно одна строка с размером и миллисекундами по этапам."""
    shrunk = b"\xff\xd8\xff\xe0" + b"j" * 250_000
    with caplog.at_level(logging.INFO, logger=LOGGER):
        resp = _upload(client, ADMIN_ID, shrunk, name="IMG_0001.jpg", ctype="image/jpeg", target="task_cover")
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "photo"
    assert api.calls == ["sendPhoto"]

    hits = _upload_lines(caplog)
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]
    m = hits[0]
    assert m["target"] == "task_cover"
    assert m["ctype"] == "image/jpeg" and m["ext"] == ".jpg"
    assert m["size"] == str(len(shrunk))
    assert m["kind"] == "photo" and m["status"] == "200"
    assert m["read"].isdigit() and m["tg"].isdigit()
    assert int(m["total"]) >= int(m["read"])


def test_submission_part_logs_kind_and_status(client, api, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        resp = _upload(client, DELEGATE_ID, b"%PDF-1.4", name="doc.pdf", ctype="application/pdf")
    assert resp.status_code == 200, resp.text
    (m,) = _upload_lines(caplog)
    assert m["target"] == "task" and m["kind"] == "document" and m["status"] == "200"


def test_failed_telegram_upload_is_still_logged_with_error_status(client, api, caplog):
    api.responses["sendPhoto"] = _reject()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        resp = _upload(client, ADMIN_ID, b"img", name="cover.jpg", ctype="image/jpeg", target="task_cover")
    assert resp.status_code == 502, resp.text
    (m,) = _upload_lines(caplog)
    assert m["status"] == "502" and m["size"] == "3"
    assert m["tg"].isdigit()


def test_empty_file_logged_without_telegram_time(client, api, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        resp = _upload(client, DELEGATE_ID, b"", name="empty.jpg", ctype="image/jpeg")
    assert resp.status_code == 400, resp.text
    (m,) = _upload_lines(caplog)
    assert m["status"] == "400" and m["tg"] == "—"
    assert api.calls == []
