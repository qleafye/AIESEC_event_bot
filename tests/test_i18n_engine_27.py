"""Phase 27 (27-03, LANG-04/LANG-10) — сторож `EmbeddedArgosDriver._boot()`: наличие
`.argosmodel`-файла в `ARGOS_PACKAGES_DIR` само по себе не делает модель рабочей —
`argos-translate-lt` читает только УСТАНОВЛЕННЫЕ пакеты (`get_installed_packages()`), архив
рядом с ними нужно распаковать через `install_from_path()`. Без этой находки (стенд,
06.09.2026) `translate()` падал `'NoneType' object has no attribute 'get_translation'` на
каждой строке очереди — ровно тот сбой, который здесь и покрывается тестом.

`argostranslate` в этом окружении не установлен (модель 156 МБ, Pitfall 9 замера 27-01) —
фейковый модуль инжектится в `sys.modules` через `monkeypatch.setitem` (реального пакета,
сети, диска с моделью не требуется, автооткат после каждого теста).
"""
from __future__ import annotations

import os
import sys
import types

import pytest

from services.i18n_engine import EmbeddedArgosDriver


class _FakePackage:
    def __init__(self, from_code: str, to_code: str) -> None:
        self.from_code = from_code
        self.to_code = to_code


def _install_fake_argostranslate(monkeypatch, initial_installed):
    """Кладёт фейковые `argostranslate`/`argostranslate.package`/`argostranslate.translate`
    в `sys.modules` — ровно то, что импортируют `import argostranslate.package`/
    `import argostranslate.translate` внутри `_boot()`. Возвращает списки вызовов для
    проверок (`installed`, `install_calls`)."""
    installed: list[_FakePackage] = list(initial_installed)
    install_calls: list[str] = []

    def get_installed_packages():
        return list(installed)

    def install_from_path(path: str) -> None:
        install_calls.append(path)
        installed.append(_FakePackage("ru", "en"))

    def translate(text: str, from_code: str, to_code: str) -> str:
        return f"EN:{text}"

    fake_root = types.ModuleType("argostranslate")
    fake_package = types.ModuleType("argostranslate.package")
    fake_translate = types.ModuleType("argostranslate.translate")
    fake_package.get_installed_packages = get_installed_packages
    fake_package.install_from_path = install_from_path
    fake_translate.translate = translate
    fake_root.package = fake_package
    fake_root.translate = fake_translate

    monkeypatch.setitem(sys.modules, "argostranslate", fake_root)
    monkeypatch.setitem(sys.modules, "argostranslate.package", fake_package)
    monkeypatch.setitem(sys.modules, "argostranslate.translate", fake_translate)

    return installed, install_calls


def _packages_dir(monkeypatch, tmp_path):
    packages_dir = str(tmp_path / "argos")
    os.makedirs(packages_dir, exist_ok=True)
    monkeypatch.delenv("ARGOS_PACKAGES_DIR", raising=False)
    monkeypatch.setenv("ARGOS_PACKAGES_DIR", packages_dir)
    return packages_dir


# ── (a) .argosmodel лежит рядом, но не установлен ──────────────────────────────────────────

def test_boot_installs_argosmodel_file_when_present_but_not_installed(monkeypatch, tmp_path):
    packages_dir = _packages_dir(monkeypatch, tmp_path)
    model_path = os.path.join(packages_dir, "translate-ru_en-1_9.argosmodel")
    with open(model_path, "wb") as fh:
        fh.write(b"fake-argosmodel-bytes")

    installed, install_calls = _install_fake_argostranslate(monkeypatch, initial_installed=[])

    driver = EmbeddedArgosDriver()
    driver._boot()

    assert install_calls == [model_path]
    assert len(installed) == 1
    assert driver._ready is True
    assert driver._unavailable is False


# ── (b) ничего нет — ни установленного пакета, ни .argosmodel-файла ────────────────────────

def test_boot_stays_unavailable_with_single_error_when_nothing_present(
    monkeypatch, tmp_path, caplog,
):
    _packages_dir(monkeypatch, tmp_path)  # каталог существует, но пуст

    _installed, install_calls = _install_fake_argostranslate(monkeypatch, initial_installed=[])

    driver = EmbeddedArgosDriver()
    with caplog.at_level("ERROR"):
        driver._boot()

    assert install_calls == []
    assert driver._unavailable is True
    assert driver._ready is False
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    assert "translate-ru_en-1_9.argosmodel" in error_records[0].getMessage()

    # translate_batch тихо отдаёт пустые строки той же длины, без повторной попытки boot.
    assert driver.translate_batch(["текст 1", "текст 2"]) == ["", ""]


# ── (c) пакет ru->en уже установлен ─────────────────────────────────────────────────────────

def test_boot_skips_install_when_ru_en_package_already_installed(monkeypatch, tmp_path):
    packages_dir = _packages_dir(monkeypatch, tmp_path)
    # Файл может физически лежать рядом (например, после предыдущего запуска) — не должен
    # провоцировать повторную установку, раз пакет уже числится установленным.
    with open(os.path.join(packages_dir, "translate-ru_en-1_9.argosmodel"), "wb") as fh:
        fh.write(b"fake-argosmodel-bytes")

    installed, install_calls = _install_fake_argostranslate(
        monkeypatch, initial_installed=[_FakePackage("ru", "en")],
    )

    driver = EmbeddedArgosDriver()
    driver._boot()

    assert install_calls == []
    assert len(installed) == 1
    assert driver._ready is True
    assert driver._unavailable is False
    assert driver.translate_batch(["текст"]) == ["EN:текст"]
