"""Квик 260911-6i9 (W3, пункт 2, T-6i9-01): строка «Резюме» в `GET /app/api/profile` — имя
файла или текст резюме, НИКОГДА ссылка Nextcloud (хост хранилища + токен ОБЩЕЙ расшаренной
папки). Харнесс — `tests/test_miniapp_routes.py` (`_client`/`_hdr`/`_standard_seed`) +
`tests/test_miniapp_delegate.py::_fill_profile` (прямой UPDATE колонок `users`).

pytest-asyncio в этом окружении не установлен — весь HTTP идёт через `TestClient`
(синхронный), собственных `asyncio.run()` этому файлу не требуется.
"""
from __future__ import annotations

import json
from urllib.parse import quote

import miniapp.routers.profile as profile_module
import reg_engine

from tests.test_miniapp_delegate import _fill_profile
from tests.test_miniapp_routes import DELEGATE_ID, _cfg, _client, _hdr, _set, _standard_seed, _use_tmp_db

_HOST = "https://203.0.113.5:8443"
_TOKEN = "SUPERSECRETFOLDERTOKEN"


def _nextcloud_link(name: str) -> str:
    return f"{_HOST}/s/{_TOKEN}/download?path=%2F&files={quote(name)}"


def _ready(tmp_path, name="miniapp_profile_resume_privacy_260911.db"):
    db_path = _use_tmp_db(tmp_path, name)
    _standard_seed()
    _set("reg_q_resume", "on")
    return db_path


def _client_for(db_path):
    return _client(_cfg(db_path))


# ── файловое резюме: имя файла, не ссылка ────────────────────────────────────────────────

def test_profile_resume_field_shows_filename_not_link(tmp_path):
    db_path = _ready(tmp_path)
    link = _nextcloud_link("Фамилия_Имя_YL26.pdf")
    _fill_profile(DELEGATE_ID, resume_file_id="AgACfile", resume_url=link, resume_text="")
    client = _client_for(db_path)
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_key = {f["key"]: f for f in body["fields"]}
    resume_key = reg_engine.label_key_for("resume")
    assert by_key[resume_key]["value"] == "Фамилия_Имя_YL26.pdf"

    dumped = json.dumps(body)
    assert "http" not in dumped
    assert "/s/" not in dumped
    assert _TOKEN not in dumped
    assert link not in dumped


# ── текстовое резюме: текст, ссылка (на .txt) тоже не уходит ────────────────────────────

def test_profile_resume_field_shows_text_when_resume_is_text(tmp_path):
    db_path = _ready(tmp_path)
    link = _nextcloud_link("resume.txt")
    _fill_profile(DELEGATE_ID, resume_file_id="", resume_url=link, resume_text="Мой опыт в продажах")
    client = _client_for(db_path)
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_key = {f["key"]: f for f in body["fields"]}
    resume_key = reg_engine.label_key_for("resume")
    assert by_key[resume_key]["value"] == "Мой опыт в продажах"
    assert " / " not in by_key[resume_key]["value"]

    dumped = json.dumps(body)
    assert "http" not in dumped
    assert "/s/" not in dumped
    assert _TOKEN not in dumped
    assert link not in dumped


# ── загрузка в Nextcloud не удалась: пустой resume_url — строки нет, ответ рабочий ───────

def test_profile_resume_field_absent_when_upload_failed(tmp_path):
    db_path = _ready(tmp_path)
    _fill_profile(DELEGATE_ID, resume_file_id="AgACfile", resume_url="", resume_text="",
                  phone="+7 999")
    _set("reg_q_phone", "on")
    client = _client_for(db_path)
    resp = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    resume_key = reg_engine.label_key_for("resume")
    by_key = {f["key"]: f for f in body["fields"]}
    assert resume_key not in by_key
    # остальные вопросы (контакты) на месте — отказ одного поля не ломает ответ целиком
    contacts_by_key = {c["key"]: c for c in body["contacts"]}
    assert contacts_by_key["reg_q_phone"]["value"] == "+7 999"


# ── form_filled/form_percent считаются от фактического списка ───────────────────────────

def test_profile_form_counts_reflect_resume_field_presence(tmp_path):
    db_path = _ready(tmp_path)
    client = _client_for(db_path)

    # Без резюме — строки нет, form_filled не учитывает её.
    _fill_profile(DELEGATE_ID, resume_file_id="", resume_url="", resume_text="")
    body_without = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    resume_key = reg_engine.label_key_for("resume")
    by_key_without = {f["key"]: f for f in body_without["fields"]}
    assert resume_key not in by_key_without
    filled_without = body_without["form_filled"]

    # С резюме файлом — строка появляется, form_filled растёт ровно на неё.
    _fill_profile(DELEGATE_ID, resume_file_id="AgACfile",
                  resume_url=_nextcloud_link("cv.pdf"), resume_text="")
    body_with = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    by_key_with = {f["key"]: f for f in body_with["fields"]}
    assert resume_key in by_key_with
    assert body_with["form_filled"] == filled_without + 1
    assert body_with["form_total"] == body_without["form_total"]


# ── адресность правки: другой составной вопрос по-прежнему через " / " ──────────────────

def test_other_composite_question_still_joined_with_slash(tmp_path):
    db_path = _ready(tmp_path)
    _fill_profile(DELEGATE_ID, expectations="Нетворкинг", expectations_ar="Networking AR")
    client = _client_for(db_path)
    body = client.get("/app/api/profile", headers=_hdr(DELEGATE_ID)).json()
    by_key = {f["key"]: f for f in body["fields"]}
    assert by_key["reg_q_expectations"]["value"] == "Нетворкинг / Networking AR"


# ── сторож формата: ключ вопроса берётся движком, не литералом ──────────────────────────

def test_profile_module_has_no_reg_q_resume_literal():
    import inspect
    source = inspect.getsource(profile_module)
    assert '"reg_q_resume"' not in source
    assert "'reg_q_resume'" not in source
    assert "file_name_from_link" in source
