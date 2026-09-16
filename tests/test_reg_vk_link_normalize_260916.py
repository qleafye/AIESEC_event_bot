"""ВК: `vk.com/...`/`@username`/голый ник — одно правило нормализации на обе поверхности
(чат и Mini App), т.к. обе идут через `reg_engine.validate_answer` (T-21-05, единый судья
ввода). Баг на стенде: Mini App показывала «Профиль узнали — ссылка рабочая» (клиент НЕ
валидирует `vk`-поле — оно `type == "text"`, не `"url"`, см. form_types.js::linkCard —
«у ВК-подобного юзернейма нет клиентского регэкспа», комментарий там же), а сервер отвечал
400 на `vk.com/robot`. Фикс — сервер принимает то же, что подсказывает клиентский вид
«ok»: ссылку, `@username` и голый ник, нормализуя всё в `@username`.

БД не нужна — `validate_answer` синхронная чистая функция для шага `vk` (не ходит в реестр).
"""
from __future__ import annotations

import reg_engine


def test_vk_accepts_plain_domain_link():
    value, error = reg_engine.validate_answer("vk", "vk.com/robot")
    assert error is None
    assert value == "@robot"


def test_vk_accepts_https_link():
    value, error = reg_engine.validate_answer("vk", "https://vk.com/robot")
    assert error is None
    assert value == "@robot"


def test_vk_accepts_mobile_subdomain_link():
    value, error = reg_engine.validate_answer("vk", "http://m.vk.com/robot")
    assert error is None
    assert value == "@robot"


def test_vk_accepts_id_style_link():
    value, error = reg_engine.validate_answer("vk", "vk.com/id123")
    assert error is None
    assert value == "@id123"


def test_vk_accepts_bare_nick_without_at():
    value, error = reg_engine.validate_answer("vk", "ivanova_maria")
    assert error is None
    assert value == "@ivanova_maria"


def test_vk_accepts_at_prefixed_nick_unchanged():
    value, error = reg_engine.validate_answer("vk", "@ivan")
    assert error is None
    assert value == "@ivan"


def test_vk_rejects_space_inside():
    value, error = reg_engine.validate_answer("vk", "ivan petrov")
    assert value is None
    assert error is not None


def test_vk_rejects_foreign_domain():
    value, error = reg_engine.validate_answer("vk", "facebook.com/robot")
    assert value is None
    assert error is not None


def test_vk_rejects_lookalike_subdomain_host():
    """`vk.com` как ПОДСТРОКА чужого хоста — не признак ссылки ВК (сверка идёт по netloc
    через urllib.parse, не по substring, тот же приём, что `validate_resume_link`)."""
    value, error = reg_engine.validate_answer("vk", "evilvk.com.attacker.com/robot")
    assert value is None
    assert error is not None


def test_vk_error_explains_accepted_formats():
    """Rule «ошибка объясняет, что сделать» — текст называет оба принимаемых формата."""
    _, error = reg_engine.validate_answer("vk", "bad input")
    assert "@username" in error
    assert "vk.com" in error


def test_vk_link_trailing_slash_and_query_ignored():
    value, error = reg_engine.validate_answer("vk", "vk.com/robot/?ref=qr")
    assert error is None
    assert value == "@robot"


def test_vk_help_example_matches_validator():
    """Подсказка `STEP_HELP['vk']` называет пример, который сам проходит валидатор шага
    (правило `test_examples_pass_their_own_validator`, tests/test_reg_step_help_260904.py)."""
    example = reg_engine.STEP_HELP_EXAMPLES["vk"]
    value, error = reg_engine.validate_answer("vk", example)
    assert error is None, error
