"""Квик 260919-u7e (P5, находка 02-miniapp «Mini App слеп: ошибок не видно», пункт 2):
`miniapp.main._json_errors`/`_log_api_error` — одна строка лога на каждый 4xx/5xx ответ API,
с `telegram_id` (из проверенной initData, иначе "anon") и причиной, БЕЗ значения ответа
делегата (T-21-08/T-30-06: в лог — только ключи полей/причина).

Живая находка: 44 ответа `PATCH /app/api/reg/draft 400` за 45,5 часов прода без единого
сигнала в логе — причину и человека установить было невозможно.

Харнесс PATCH `/app/api/reg/draft` — фикстуры `tests/test_miniapp_form.py`, файлового
маршрута — `tests/test_miniapp_files.py`, харнесс TestClient — `tests/test_miniapp_routes.py`.
"""
from __future__ import annotations

import logging

from miniapp.main import _error_log_extra

from tests.test_miniapp_files import FILE_ID as OWNED_FILE_ID
from tests.test_miniapp_form import _seed_draft
from tests.test_miniapp_routes import (
    DELEGATE_ID,
    _cfg,
    _client,
    _hdr,
    _standard_seed,
    _use_tmp_db,
)


# ── _error_log_extra: только ключи полей/коды, никогда значение ответа делегата ────────────

def test_error_log_extra_reads_field_keys_not_values():
    body = {
        "reason": "invalid",
        "errors": {"age": "Введите число от 16 до 99", "full_name": "Слишком короткое имя"},
    }
    extra = _error_log_extra(body)
    assert "fields=" in extra
    assert "age" in extra and "full_name" in extra
    # Тексты ошибок (человеческие подсказки) и тем более значения делегата -- не в extra.
    assert "Введите число" not in extra
    assert "Слишком короткое имя" not in extra


def test_error_log_extra_reads_single_field_code():
    assert _error_log_extra({"reason": "bad_field", "field": "not_a_real_column"}) == " field=not_a_real_column"
    assert _error_log_extra({"reason": "no_cap", "cap": "moderate_game"}) == " cap=moderate_game"
    assert _error_log_extra({"reason": "section_off", "section": "coins"}) == " section=coins"
    assert _error_log_extra({"reason": "delegate_gate", "kind": "pending"}) == " kind=pending"


def test_error_log_extra_empty_for_bodies_without_recognised_keys():
    assert _error_log_extra({"reason": "no_auth"}) == ""
    assert _error_log_extra({"reason": "server_error"}) == ""


# ── интеграция: PATCH /app/api/reg/draft 400 -> одна строка с telegram_id, без значения ────

def test_patch_invalid_answer_logs_telegram_id_reason_and_field_without_value(tmp_path, caplog):
    """Тот же случай, что живая находка: делегат шлёт `age=abc` (формат не проходит
    валидацию) -- лог обязан назвать причину и поле, но НЕ ЗНАЧЕНИЕ ответа делегата ("abc" в
    строку лога попасть не должно, T-21-08)."""
    db_path = _use_tmp_db(tmp_path, "miniapp_logging_errors_260919.db")
    _standard_seed()
    client = _client(_cfg(db_path))
    with caplog.at_level(logging.INFO, logger="miniapp.main"):
        resp = client.patch(
            "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
            json={"version": 0, "answers": {"age": "abc"}},
        )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "invalid"

    records = [r for r in caplog.records if r.name == "miniapp.main"]
    assert records, "central _json_errors не записал ни одной строки на 400"
    line = records[-1].getMessage()
    assert f"telegram_id={DELEGATE_ID}" in line
    assert "reason=invalid" in line
    assert "PATCH" in line and "/app/api/reg/draft" in line and "400" in line
    assert "fields=" in line and "age" in line
    # Значение делегата (ответ на вопрос) не должно попасть в лог ни в каком виде.
    assert "abc" not in line


def test_patch_bad_field_logs_field_without_submitted_value(tmp_path, caplog):
    db_path = _use_tmp_db(tmp_path, "miniapp_logging_errors_260919_bf.db")
    _standard_seed()
    client = _client(_cfg(db_path))
    with caplog.at_level(logging.INFO, logger="miniapp.main"):
        resp = client.patch(
            "/app/api/reg/draft", headers=_hdr(DELEGATE_ID),
            json={"version": 0, "answers": {"not_a_real_column": "секретный ответ делегата"}},
        )
    assert resp.status_code == 400
    records = [r for r in caplog.records if r.name == "miniapp.main"]
    line = records[-1].getMessage()
    assert f"telegram_id={DELEGATE_ID}" in line
    assert "reason=bad_field" in line
    assert "field=not_a_real_column" in line
    assert "секретный ответ делегата" not in line


def test_unauthenticated_401_logs_anon_not_a_crash(tmp_path, caplog):
    """До проверки initData личность неизвестна -- "anon", а не исключение/пустая строка."""
    db_path = _use_tmp_db(tmp_path, "miniapp_logging_errors_260919_anon.db")
    _standard_seed()
    client = _client(_cfg(db_path))
    with caplog.at_level(logging.INFO, logger="miniapp.main"):
        resp = client.get("/app/api/me")
    assert resp.status_code == 401
    records = [r for r in caplog.records if r.name == "miniapp.main"]
    line = records[-1].getMessage()
    assert "telegram_id=anon" in line
    assert "reason=no_auth" in line


def test_file_route_401_logs_at_debug_not_warning(tmp_path, caplog):
    """Тег `<img>` не шлёт initData -- 401 на приватной картинке штатен на каждый показ
    карточки без токена; WARNING здесь топит лог шумом вместо сигнала (находка, пункт 2)."""
    db_path = _use_tmp_db(tmp_path, "miniapp_logging_errors_260919_file.db")
    _standard_seed()
    client = _client(_cfg(db_path))
    with caplog.at_level(logging.DEBUG, logger="miniapp.main"):
        resp = client.get(f"/app/api/file/{OWNED_FILE_ID}")
    assert resp.status_code == 401
    records = [r for r in caplog.records if r.name == "miniapp.main"]
    assert records, "файловый 401 обязан всё равно попасть в caplog на DEBUG"
    assert records[-1].levelno == logging.DEBUG
    assert not any(r.levelno >= logging.WARNING for r in records)


def test_server_error_logs_at_error_level(tmp_path, caplog, monkeypatch):
    """5xx (`reg draft submit: finalize_data failed`) обязан уйти уровнем ERROR, не WARNING --
    менеджер, грепающий `docker logs | grep ERROR`, не должен пропустить настоящий сбой.

    Патчим `miniapp.routers.form.finalize_data`, не `services.reg_finalize.finalize_data` --
    `form.py` делает `from services.reg_finalize import finalize_data` (имя скопировано в его
    собственное пространство имён при импорте), патч исходного модуля роутер не увидит."""
    db_path = _use_tmp_db(tmp_path, "miniapp_logging_errors_260919_500.db")
    _standard_seed()
    client = _client(_cfg(db_path))

    import miniapp.routers.form as form_router

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(form_router, "finalize_data", _boom)
    # Тот же сид, что рабочий сценарий `test_submit_edit_success_queues_one_event_and_replies`
    # (test_miniapp_form.py) -- одобренный делегат с full_name из _standard_seed, без согласий.
    _seed_draft(DELEGATE_ID, kind="edit", patch={"phone": "+79997776655"})

    with caplog.at_level(logging.INFO, logger="miniapp.main"):
        resp = client.post("/app/api/reg/draft/submit", headers=_hdr(DELEGATE_ID))
    assert resp.status_code == 500
    records = [r for r in caplog.records if r.name == "miniapp.main"]
    assert records, "central _json_errors не записал 500"
    assert records[-1].levelno == logging.ERROR
    assert f"telegram_id={DELEGATE_ID}" in records[-1].getMessage()
    assert "reason=server_error" in records[-1].getMessage()
