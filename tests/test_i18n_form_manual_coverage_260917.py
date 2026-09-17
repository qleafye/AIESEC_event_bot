"""Квик 260917-en (приёмка, английский делегат: «перевод интерфейса очень криво сделан») —
сторож покрытия корпуса анкеты ручным переводом (`services/i18n_form_manual.py`).

pytest-asyncio в проекте нет (см. соседние тесты Phase 27) — асинхронные вызовы идут через
`asyncio.run`, `config.DB_PATH` смотрит в `tmp_path`.

Пять пунктов инструкции (5а-5г, 5д проверяется отдельными golden-тестами
`test_reg_engine_parity.py`/`test_refac_snapshot_260816.py`, не здесь):

(а) каждый RU-текст корпуса по умолчанию (`services.i18n_sources.corpus()` на пустой БД) имеет
    ручной перевод — в `i18n_ui_en.UI_EN`, `services.i18n_miniapp_manual.MANUAL_EN` или
    `services.i18n_form_manual.FORM_DEFAULT_EN`. Новый ключ реестра без перевода роняет тест с
    понятным сообщением (список недостающих строк, не просто счётчик).
(б) `tr(ru, "en", tr_map)` после `seed()` возвращает ручной перевод — включая строки с ведущим
    эмодзи и HTML-разметкой.
(в) плейсхолдеры (`{min}`, `{n}`, `{count}`, ...) совпадают у RU и EN во всех словарях модуля.
(г) сид не перетирает `manual=1` перевод менеджера (другой `origin_key`).

`EVENT_TEXTS_260917` сторож (а) НЕ требует — это снимок реальных текстов конкретного события,
не часть кодовой базы (см. докстринг `services/i18n_form_manual.py`)."""
import asyncio
import re

from config import config
from database import db
from i18n_ui_en import UI_EN
from services import i18n
from services.i18n_miniapp_manual import MANUAL_EN
from services.i18n_form_manual import (
    EVENT_TEXTS_260917,
    FORM_DEFAULT_EN,
    ORIGIN_DEFAULT,
    ORIGIN_EVENT,
    seed,
)
from services.i18n_sources import corpus

_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")


def _db_ready(tmp_path, name="test_i18n_form_manual_coverage.db"):
    config.DB_PATH = str(tmp_path / name)
    asyncio.run(db.init_db())


# --- (а) покрытие корпуса по умолчанию -------------------------------------------------------

def test_default_corpus_fully_covered_by_manual_translations(tmp_path):
    """Пустая БД -> `corpus()` отдаёт ровно кодовые дефолты (без правок менеджера) — каждый из
    них обязан иметь ручной перевод хотя бы в одном из трёх словарей яруса A/B."""
    _db_ready(tmp_path)
    items = asyncio.run(corpus())
    covered = set(UI_EN) | set(MANUAL_EN) | set(FORM_DEFAULT_EN)
    missing = [(origin, text) for origin, text in items if text not in covered]
    assert missing == [], (
        f"{len(missing)} строк(и) корпуса анкеты без ручного перевода "
        f"(добавь в services/i18n_form_manual.py::FORM_DEFAULT_EN): {missing[:20]}"
    )


def test_default_corpus_nonempty_sanity(tmp_path):
    """Сторож сторожа: если `corpus()` вдруг схлопнется до пустого списка (баг в самой
    функции), тест (а) выше молча «пройдёт» на пустом множестве — явно проверяем масштаб."""
    _db_ready(tmp_path)
    items = asyncio.run(corpus())
    assert len(items) > 400, f"corpus() внезапно вернул мало строк ({len(items)}) — проверь i18n_sources.py"


# --- (в) плейсхолдеры RU/EN совпадают --------------------------------------------------------

def test_placeholders_match_ru_en_form_default():
    mismatched = []
    for ru, en in FORM_DEFAULT_EN.items():
        ru_tokens = sorted(_PLACEHOLDER_RE.findall(ru))
        en_tokens = sorted(_PLACEHOLDER_RE.findall(en))
        if ru_tokens != en_tokens:
            mismatched.append((ru[:60], ru_tokens, en_tokens))
    assert mismatched == [], f"плейсхолдеры RU/EN разошлись: {mismatched}"


def test_placeholders_match_ru_en_event_texts():
    mismatched = []
    for ru, en in EVENT_TEXTS_260917.items():
        ru_tokens = sorted(_PLACEHOLDER_RE.findall(ru))
        en_tokens = sorted(_PLACEHOLDER_RE.findall(en))
        if ru_tokens != en_tokens:
            mismatched.append((ru[:60], ru_tokens, en_tokens))
    assert mismatched == [], f"плейсхолдеры RU/EN разошлись: {mismatched}"


# --- (б) tr() реально находит перевод после seed(), включая эмодзи/HTML ----------------------

_TR_SAMPLES = (
    # Обычная строка вопроса анкеты.
    "Из какого ты города?",
    # Ведущий эмодзи — REG_LABELS-подпись. `tr()` (в отличие от `reg_i18n.tr_text`) ищет по
    # хешу ВСЕГО текста, эмодзи в начале остаётся частью ключа.
    "🪪 ФИО",
    # HTML-разметка внутри значения — тег обязан пережить перевод дословно.
    "📜 <b>История монет</b>",
    # Плейсхолдер внутри шаблона.
    "Шаг {step} из {total}",
)


def test_seeded_form_default_translations_resolve_through_tr(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(seed())
    tr_map = asyncio.run(i18n.load_map("en"))
    for ru_text in _TR_SAMPLES:
        assert ru_text in FORM_DEFAULT_EN, f"фикстура теста устарела: {ru_text!r} пропал из FORM_DEFAULT_EN"
        assert i18n.tr(ru_text, "en", tr_map) == FORM_DEFAULT_EN[ru_text]


def test_seeded_event_translations_resolve_through_tr(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(seed())
    tr_map = asyncio.run(i18n.load_map("en"))
    ru_text, en_text = next(iter(EVENT_TEXTS_260917.items()))
    assert i18n.tr(ru_text, "en", tr_map) == en_text


def test_seed_applies_every_entry(tmp_path):
    _db_ready(tmp_path)
    result = asyncio.run(seed())
    assert result["applied"] == len(FORM_DEFAULT_EN) + len(EVENT_TEXTS_260917)
    assert result["skipped_manager_edit"] == 0


def test_seed_is_idempotent(tmp_path):
    _db_ready(tmp_path)
    asyncio.run(seed())
    result = asyncio.run(seed())
    assert result["applied"] == len(FORM_DEFAULT_EN) + len(EVENT_TEXTS_260917)
    assert result["skipped_manager_edit"] == 0


# --- (г) сид не перетирает ручную правку менеджера -------------------------------------------

def test_seed_does_not_overwrite_manager_manual_edit_default(tmp_path):
    _db_ready(tmp_path)
    ru_text = next(iter(FORM_DEFAULT_EN))
    manager_text = "A manager's own hand-written translation"
    asyncio.run(db.upsert_translation(
        "en", i18n.src_hash(ru_text), ru_text, manager_text, manual=1, origin_key="admin_edit",
    ))
    result = asyncio.run(seed())
    assert result["skipped_manager_edit"] == 1
    row = asyncio.run(db.get_translation("en", i18n.src_hash(ru_text)))
    assert row["text"] == manager_text
    assert row["origin_key"] == "admin_edit"


def test_seed_does_not_overwrite_manager_manual_edit_event(tmp_path):
    _db_ready(tmp_path)
    ru_text = next(iter(EVENT_TEXTS_260917))
    manager_text = "A manager's own hand-written translation"
    asyncio.run(db.upsert_translation(
        "en", i18n.src_hash(ru_text), ru_text, manager_text, manual=1, origin_key="admin_edit",
    ))
    result = asyncio.run(seed())
    assert result["skipped_manager_edit"] == 1
    row = asyncio.run(db.get_translation("en", i18n.src_hash(ru_text)))
    assert row["text"] == manager_text
    assert row["origin_key"] == "admin_edit"


def test_reseeding_after_upgrade_updates_our_own_previous_seed(tmp_path):
    """Наша же прошлая версия сида (тот же `origin_key`) — не «менеджер редактировал», её можно
    и нужно перезаписать новой редакцией строки без вмешательства человека."""
    _db_ready(tmp_path)
    ru_text = next(iter(FORM_DEFAULT_EN))
    asyncio.run(db.upsert_translation(
        "en", i18n.src_hash(ru_text), ru_text, "stale old seed text", manual=1, origin_key=ORIGIN_DEFAULT,
    ))
    result = asyncio.run(seed())
    assert result["skipped_manager_edit"] == 0
    row = asyncio.run(db.get_translation("en", i18n.src_hash(ru_text)))
    assert row["text"] == FORM_DEFAULT_EN[ru_text]


def test_event_origin_key_is_distinct_from_default():
    """Отдельные `origin_key` (докстринг модуля) — событийный снимок и дефолты кодовой базы
    развиваются независимо, перезапись одного не должна путаться с другим."""
    assert ORIGIN_DEFAULT != ORIGIN_EVENT
