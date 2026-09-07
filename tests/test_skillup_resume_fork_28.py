"""Phase 28 (28-04, SU-04, СкиллАп 5): сторожа серверной половины развилки резюме — третье
значение режима `fork`, редактируемый вайтлист доменов, чистая проверка ссылки
(`reg_engine.validate_resume_link`), спека развилки для обеих поверхностей (`step_spec`),
3-way тумблер режима резюме (`handlers/admin_reg_percity.py`, задача 2) и запись
`resume_type`/`resume_link`/`link_verified` на финале + два столбца листа (задача 3).

pytest-asyncio недоступен в этом окружении — async через `asyncio.run()`, фикстура временной
БД — тот же приём, что `tests/test_skillup_core_28.py::_ready(tmp_path)`.
"""
import asyncio

from config import config
from database import db
import reg_engine

UID = 900800400


def _ready(tmp_path, name="test_skillup_resume_fork_28.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


# ══════════════════════════════════════════════════════════════════════════════════════════
# Задача 1: режим fork, вайтлист доменов и проверка ссылки
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_mode_fork_resolves(tmp_path):
    """`reg_resume_mode = fork` резолвится как есть — третье допустимое значение."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        return await reg_engine.resume_mode()

    assert asyncio.run(go()) == "fork"


def test_unknown_mode_falls_back(tmp_path):
    """Любое прочее значение (в т.ч. мусор/будущее ещё не заведённое) по-прежнему падает в
    дефолт `file_or_text` — сторож D-06 (третье значение ничего не сломало в fail-soft)."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_resume_mode", "garbage")
        return await reg_engine.resume_mode()

    assert asyncio.run(go()) == "file_or_text"


def test_link_from_whitelist_verified(tmp_path):
    """Ссылка на hh.ru (домен из дефолтного вайтлиста) — принята и помечена проверенной."""
    _ready(tmp_path)
    url, verified, err = reg_engine.validate_resume_link(
        "https://hh.ru/resume/123", reg_engine.RESUME_LINK_WHITELIST_DEFAULT,
    )
    assert url == "https://hh.ru/resume/123"
    assert verified is True
    assert err is None


def test_link_outside_whitelist_accepted_unverified(tmp_path):
    """Личный сайт вне списка — НЕ ошибка (ТЗ §3.3, D-04): ссылка принимается, флаг ложный,
    ошибки нет вовсе (никакого укора делегату)."""
    _ready(tmp_path)
    url, verified, err = reg_engine.validate_resume_link(
        "https://my-portfolio.example", reg_engine.RESUME_LINK_WHITELIST_DEFAULT,
    )
    assert url == "https://my-portfolio.example"
    assert verified is False
    assert err is None


def test_link_without_scheme_rejected(tmp_path):
    """Ссылка без `http(s)://` — объясняющая ошибка, а не молчаливый отказ/краш."""
    _ready(tmp_path)
    url, verified, err = reg_engine.validate_resume_link(
        "hh.ru/resume/123", reg_engine.RESUME_LINK_WHITELIST_DEFAULT,
    )
    assert url is None
    assert verified is False
    assert err
    assert "http" in err


def test_lookalike_subdomain_not_verified(tmp_path):
    """Похожий домен-самозванец (`hh.ru.evil.com`) НЕ считается проверенным — сравнение по
    ПОЛНОМУ хосту, не `endswith`/подстрока (T-28-04-02)."""
    _ready(tmp_path)
    url, verified, err = reg_engine.validate_resume_link(
        "https://hh.ru.evil.com/resume", reg_engine.RESUME_LINK_WHITELIST_DEFAULT,
    )
    assert url == "https://hh.ru.evil.com/resume"
    assert verified is False
    assert err is None


def test_whitelist_editable_from_registry(tmp_path):
    """Менеджер правит список доменов из админки (D-01) — `resume_link_whitelist()` читает
    `reg_resume_link_whitelist`, пусто = пять сайтов ТЗ §3.3 байт-в-байт."""
    _ready(tmp_path)

    async def go():
        before = await reg_engine.resume_link_whitelist()
        await db.set_setting("reg_resume_link_whitelist", "example.org\ncareers.example.org")
        after = await reg_engine.resume_link_whitelist()
        return before, after

    before, after = asyncio.run(go())
    assert before == reg_engine.RESUME_LINK_WHITELIST_DEFAULT
    assert after == ["example.org", "careers.example.org"]


def test_step_spec_publishes_fork_options(tmp_path):
    """`step_spec("resume")` в режиме `fork` отдаёт `type: "resume-fork"` и три записи
    `fork_options` с человеческими подписями по умолчанию; `step_spec("resume_link")` отдаёт
    `type: "url"` и `link_whitelist` (дефолтный список, если менеджер ничего не настраивал)."""
    _ready(tmp_path)

    async def go():
        await db.set_setting("reg_resume_mode", "fork")
        resume_spec = await reg_engine.step_spec("resume")
        link_spec = await reg_engine.step_spec("resume_link")
        return resume_spec, link_spec

    resume_spec, link_spec = asyncio.run(go())

    assert resume_spec["type"] == "resume-fork"
    assert resume_spec["resume_mode"] == "fork"
    fork_options = resume_spec["fork_options"]
    assert [o["code"] for o in fork_options] == ["file", "link", "mini"]
    assert fork_options[0]["label"] == "📎 Загрузить файл"
    assert fork_options[1]["label"] == "🔗 Дать ссылку"
    assert fork_options[2]["label"] == "🙅 У меня нет резюме"
    assert all(o["icon"] for o in fork_options)

    assert link_spec["type"] == "url"
    assert link_spec["link_whitelist"] == reg_engine.RESUME_LINK_WHITELIST_DEFAULT
