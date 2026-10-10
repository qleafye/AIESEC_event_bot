"""Приёмка 09.10 (Mini App): служебная подпись «💬 Ожидания (общие)» видна делегату.

«(общие)» в админке отличает вопрос от варианта для отдельного трека; делегату в анкете
приложения (над полем, в списке вопросов, в сводке) это ничего не говорит. Анкета берёт
подпись из `reg_labels.DELEGATE_LABELS`, админка — по-прежнему из `REG_LABELS`.
"""
from __future__ import annotations

import asyncio

import domain.regform.engine as reg_engine
from domain.regform.labels import DELEGATE_LABELS, REG_LABELS
from services import i18n_form_manual


def test_admin_label_unchanged():
    assert reg_engine.label_for("expectations") == REG_LABELS["reg_q_expectations"]


def test_delegate_label_has_no_service_suffix():
    label = reg_engine.delegate_label_for("expectations")
    assert "(общие)" not in label
    assert "Ожидания" in label


def test_other_steps_keep_admin_label():
    assert reg_engine.delegate_label_for("phone") == reg_engine.label_for("phone")


def test_step_spec_uses_delegate_label(tmp_path, monkeypatch):
    from tests.test_miniapp_routes import _use_tmp_db
    _use_tmp_db(tmp_path, "labels.db")
    spec = asyncio.run(reg_engine.step_spec("expectations", "full", None))
    assert spec["label"] == DELEGATE_LABELS["reg_q_expectations"]


def test_delegate_labels_have_english():
    for label in DELEGATE_LABELS.values():
        assert label in i18n_form_manual._REG_LABELS_EN
    assert set(DELEGATE_LABELS) <= set(REG_LABELS)
