"""Доска приёмки 12.10: блок «Форум Москвы» по функциям, вкладки без имён тестеров и раздача
файлов для шагов (`/files/<имя>`) из каталога рядом с app.py — по коду доступа, как /api/state.
"""
from __future__ import annotations
from tests._paths import REPO_ROOT

import importlib
import re
import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

BOARD = REPO_ROOT / "tools" / "uat_board" / "index.html"


def _board() -> str:
    return BOARD.read_text(encoding="utf-8")


def _js_list(html: str, start: str) -> str:
    i = html.index(start)
    return html[i:html.index("\n];", i)]


def test_moscow_block_is_first_and_split_by_function():
    html = _board()
    blocks = _js_list(html, "const BLOCKS = [")
    keys = re.findall(r'^\{ key:"(\w+)", pre:', blocks, flags=re.M)
    assert keys[0] == "msk", keys
    msk = blocks[blocks.index('{ key:"msk"'):blocks.index('{ key:"fix"')]
    titles = re.findall(r'title:"([^"]+)", from:\["ms\.', msk)
    for want in ("Подготовка", "Картинки дней", "Тест: настройка", "Расписание: запись делегата",
                 "Чек-ин на сессиях", "Рассылки, выгрузки, статистика", "Закрытие записи и возврат стенда"):
        assert want in titles, want
    path = _js_list(html, "const PATH = [")
    first = re.findall(r'"([\w-]+)"', path)[:len(titles)]
    assert all(p.startswith("msk-") for p in first), first
    # старые разделы той же фичи из «Новое» убраны — без дублей
    for old in ("enroll0910", "enrscan0910", "quiz0910"):
        assert f'"bl.{old}"' not in blocks and f'"new-{old}"' not in path


def test_no_tester_names_on_board():
    html = _board()
    found = re.findall(r"Алин[аыеу]|Наст(?:я|и|е|ю|ей)\b|Ghostarss|nassst", html)
    assert not found, found
    tabs = html[html.index("const TABS = {"):html.index("const tabOf")]
    assert re.findall(r'^\s+(\w): \{ name: "([^"]+)"', tabs, flags=re.M) == [
        ("D", "Делегат"), ("M", "Менеджер"), ("V", "Волонтёр"), ("O", "Владелец")]
    assert "person:" not in tabs


def test_step_files_are_the_known_ones():
    names = set(re.findall(r'files:\["([^"]+)"\]', _board()))
    assert names == {"quiz-msk.csv", "agenda-30-10.jpg", "agenda-31-10.jpg"}


def test_page_without_code_warns_and_hides_downloads():
    """Без ?k= страница говорит, где взять ссылку, а файлы не скачиваются: download сохранял ответ 403
    под именем CSV. Сам код в странице не лежит — репозиторий публичный."""
    html = _board()
    assert 'id="nokey"' in html and "https://uat.alekseev.info/?k=…" in html
    assert "файл доступен по ссылке с кодом" in html
    assert "body.no-key .fl-ok{display:none}" in html
    assert re.search(r"\?k=[A-Za-z0-9]{3,}", html) is None


@pytest.fixture()
def board(tmp_path, monkeypatch):
    files = tmp_path / "files"
    files.mkdir()
    (files / "quiz-msk.csv").write_bytes("вопрос;вариант;компетенция;баллы\n".encode("utf-8-sig"))
    (files / "agenda-30-10.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (files / "notes.txt").write_text("служебное", encoding="utf-8")
    (tmp_path / "secret.csv").write_text("не отдавать", encoding="utf-8")
    monkeypatch.setenv("UAT_DATA", str(tmp_path / "data" / "state.json"))
    monkeypatch.setenv("UAT_FILES", str(files))
    monkeypatch.setenv("UAT_CODE", "kod")
    monkeypatch.syspath_prepend(str(REPO_ROOT / "tools" / "uat_board"))
    sys.modules.pop("app", None)
    mod = importlib.import_module("app")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    sys.modules.pop("app", None)


def _get(port: int, path: str, headers: dict | None = None):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()


def test_csv_is_served_as_attachment_by_code(board):
    status, head, body = _get(board, "/files/quiz-msk.csv?k=kod")
    assert status == 200
    assert head["content-type"] == "text/csv; charset=utf-8"
    assert head["content-disposition"].startswith("attachment;")
    assert body.decode("utf-8-sig").startswith("вопрос;")
    status, _, _ = _get(board, "/files/quiz-msk.csv", {"X-Code": "kod"})
    assert status == 200


def test_picture_is_inline(board):
    status, head, body = _get(board, "/files/agenda-30-10.jpg?k=kod")
    assert status == 200 and head["content-type"] == "image/jpeg"
    assert head["content-disposition"].startswith("inline;") and body.startswith(b"\xff\xd8")


def test_files_need_code(board):
    status, head, body = _get(board, "/files/quiz-msk.csv")
    assert status == 403 and "ссылке с доски" in body.decode("utf-8")
    status, _, _ = _get(board, "/files/quiz-msk.csv?k=wrong")
    assert status == 403


@pytest.mark.parametrize("name", [
    "..%2Fsecret.csv", "%2E%2E%2Fsecret.csv", "../secret.csv", "notes.txt", "net-takogo.csv", ".hidden.csv", "",
])
def test_only_listed_files_from_the_folder(board, name):
    status, _, _ = _get(board, "/files/" + name + "?k=kod")
    assert status == 404
