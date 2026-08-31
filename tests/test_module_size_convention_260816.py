"""Phase 13 (refac-split-god-files) Plan 07 — REFAC-03: module-size convention, CI-enforced.

The whole point of Phase 13 (13-01..13-06) was cutting `handlers/admin.py` (3067 lines) and
`handlers/registration.py` (2953 lines) down to a set of readable, single-concern seam files
sharing one `Router()` per aggregator. Splitting once is worthless if nothing stops the same
files from silently re-accreting handlers back to god-file size over the next dozen features.

This is that guard. See `CONVENTIONS.md` ("Размер модуля: ориентир ~800 строк") for the full
rationale and the human-readable version of the table below.

Design (per 13-07-PLAN.md's <interfaces> note):
- The ~800-line number is a REVIEW ORIENTEER, not the CI gate itself — a small, honest overage
  (a screen that just needs a few more lines) should not break the build.
- The CI gate is a HARD ceiling per file: `DEFAULT_CEILING` (850) for any handlers/*.py file
  not named below, or an explicit per-file ceiling in `KNOWN_OVERAGES` for files that legitimately
  sit above that today. Every entry in `KNOWN_OVERAGES` carries a one-line reason — it is a named,
  reviewed exception, not a blanket loosening of the rule.
- Raising a ceiling is a deliberate act: edit `KNOWN_OVERAGES` (or add a new entry) IN THE SAME
  COMMIT as the growth that needs it, with an updated reason. Do not bump a neighboring file's
  ceiling "while you're in here" — each entry is owned by whoever grew that file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

HANDLERS_DIR = Path(__file__).resolve().parent.parent / "handlers"

# Soft guideline every handlers/*.py module SHOULD stay under (see CONVENTIONS.md).
GUIDELINE = 800

# Hard CI ceiling for any handlers/*.py file NOT explicitly listed in KNOWN_OVERAGES below.
# Deliberately a little above GUIDELINE (per 13-07-PLAN.md's 850-900 note) so a small, honest
# overage doesn't break the build -- genuine regrowth toward god-file size still gets caught.
DEFAULT_CEILING = 850

# Named, reviewed exceptions: files that already sit above DEFAULT_CEILING as of 13-07 for a
# documented reason. Ceiling per file = current line count + ~5% slack, rounded. To raise one:
# edit the number + reason together, in the same commit as the growth that needs it.
KNOWN_OVERAGES: dict[str, tuple[int, str]] = {
    "registration.py": (
        2310,
        "FSM-движок (_ask_step/_advance/_decide_status/...), cmd_start, "
        "finalize_registration -- документированный разрыв 13-03 (registration.py "
        "остаётся агрегатором-ядром, а не god-файлом: reg_flow.py/reg_steps.py уже "
        "вынесены и оба комфортно под GUIDELINE)",
    ),
    "admin_gamification.py": (
        2020,
        "геймификация -- самый большой шов 13-04, растёт вместе с фичами игры "
        "(Phase 14/16)",
    ),
    "admin_settings.py": (
        2106,
        "+44 строки (Phase 20, разделы по flow делегата) -- вынос текстов тумблеров лендинга "
        "в settings_toggle_rows (единственный источник кнопок и для старого экрана, и для "
        "экранов разделов: одна подпись тумблера в двух местах = два разных текста), "
        "разрез «📝 Регистрация» на анкету + новую группу «📋 Заявки» (_APPS_FIELD_ORDER) и "
        "хвостовой seam-импорт handlers/admin_sections.py; сами экраны разделов и реестр "
        "SECTIONS живут в новом модуле, сюда не добавлены. "
        "+72 строки (quick 260825-ldi) -- «🔄 Синхронизация» (sync_sheet) получила маршрутизацию "
        "по городским вкладкам (city_row_tab), как у соседней «♻️ Пересобрать»: раскладка "
        "пользователей на main/city_users, пер-вкладочный try/except с fail-soft-хелперами и "
        "разбивка отчёта по вкладкам -- логика физически смежна существующему телу sync_sheet, "
        "выносить в отдельный файл ради одной функции не стали. "
        "(+2 строки 22.08: poll_intro_text/polls_sheet_tab в _*_FIELD_ORDER и HTML_SETTINGS после слияния «➕ пункт» + согласий + опросов; новые экраны живут в швах admin_settings_lists/admin_consent) "
        "настройки+инструменты таблицы+выгрузки -- блоки не смежны в исходном "
        "god-файле, разрез на два файла потребовал бы двух точек шва-импорта "
        "(см. 13-06-SUMMARY.md, Known Gap); +31 строка -- перепроверка прав "
        "на per-city запись в settings_edit_value (ревью 09.3); +33 строки (quick 260819) -- "
        "три тумблера лендинга (предотбор / сводка заявок / догонялка) и поля "
        "nudge_*/интервалов джоб в _*_FIELD_ORDER; +34 строки (quick 260820-rms) -- отбивка "
        "команды в settings_edit_value и показ списочной настройки по пунктам с "
        "предупреждением о полной замене (оба сюжета живут ровно в этих двух функциях, "
        "выносить их в отдельный файл — шов ради одной проверки); +15 строк (quick 260822) -- "
        "кнопки ➕/🗑/✏️ на экране списочной настройки и хвостовой seam-импорт "
        "admin_settings_lists.py (сами хендлеры живут в новом модуле); +7 строк (Phase 19-08) -- "
        "кнопка «🎨 Оформление» на лендинге настроек рядом с «📊 Дашборд» и хвостовой "
        "seam-импорт handlers/admin_miniapp.py (сами хендлеры живут в новом модуле); "
        "+3 строки (Phase 19.1-07) -- хвостовой seam-импорт handlers/admin_miniapp_theme.py "
        "(второй шов «🎨 Оформление»: пресеты и ручки кастома, сами хендлеры живут в новом модуле)",
    ),
    "admin_cities.py": (
        960,
        "города + сброс/импорт сезона + дедупликация -- один связный экран (13-05)",
    ),
    "admin_broadcasts.py": (
        860,
        "рассылки + pending_albums (13-05)",
    ),
    "admin_roles.py": (
        860,
        "роли + settings-guide (13-04)",
    ),
    "admin.py": (
        900,
        "сам агрегатор-ядро после 13-06, уже на границе GUIDELINE; +52 строки (Phase 15, "
        "STAT-03/D-10/D-18) -- городской скоуп render_stats_text по привязке staff.city + "
        "кнопка «🌐 Открыть дашборд» (_stats_keyboard_for)",
    ),
    "user_actions.py": (
        1145,
        "НЕ часть Phase 13 (не god-файл этого рефактора) -- перебор обнаружен при "
        "13-07 верификации, вырос отдельно за счёт карточки задания делегата "
        "(Phase 14/16); зафиксирован как отдельный известный разрыв, не переносим "
        "сюда рефактор Phase 13. Потолок поднят 900->1140 в 16-01 (GAME-UI-01): "
        "делегатский список/карточка задания получили пагинацию/RU-категории/статус-"
        "текст, плюс новый экран «🪙 Баланс» (история+рейтинг). Разрез на отдельный "
        "шов-файл (handlers/game_delegate.py) рассмотрен и отложен -- игровой блок "
        "физически смежный (строки 79-777), но 7+ существующих тестов обращаются к "
        "делегатским игровым функциям через `ua_mod.<func>` (не через отдельный "
        "модуль, в отличие от admin_gamification.py), и перенос потребовал бы правки "
        "всех них вне заявленного files_modified этого плана; откладываем разрез до "
        "отдельного прохода, когда его можно сделать вместе с обновлением тестов. "
        "Потолок поднят 1140->1145 в Phase 19-08 (D-10): точка входа «📱 Приложение» "
        "(open_miniapp_button) — реплай-кнопка -> inline web_app, дописана в самый "
        "конец файла (golden-снапшот фиксирует порядок роутера).",
    ),
}


def _handler_modules() -> list[Path]:
    return sorted(HANDLERS_DIR.glob("*.py"))


def _line_count(path: Path) -> int:
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def test_known_overages_reference_real_files():
    """Guards the allowlist itself: no stale entries for files that got deleted/renamed."""
    existing = {p.name for p in _handler_modules()}
    missing = sorted(name for name in KNOWN_OVERAGES if name not in existing)
    assert not missing, (
        f"KNOWN_OVERAGES lists module(s) that no longer exist under handlers/: {missing}. "
        "Remove the stale entry (the file was likely renamed or deleted)."
    )


@pytest.mark.parametrize("path", _handler_modules(), ids=lambda p: p.name)
def test_handler_module_under_size_ceiling(path: Path):
    """Fails loud when a handlers/*.py module crosses its size ceiling.

    Not in KNOWN_OVERAGES -> DEFAULT_CEILING applies. In KNOWN_OVERAGES -> that file's own
    ceiling applies instead. Either way: if this test fails, the module has regrown toward
    god-file size (or crossed its already-generous named exception) and needs a split -- see
    CONVENTIONS.md for the shared-router seam-import technique this phase established. If the
    growth is legitimate and a split is genuinely not worth it right now, raise the ceiling for
    THIS file in KNOWN_OVERAGES above, in the same commit, with an updated reason.
    """
    lines = _line_count(path)
    if path.name in KNOWN_OVERAGES:
        ceiling, reason = KNOWN_OVERAGES[path.name]
        assert lines <= ceiling, (
            f"{path.name}: {lines} lines exceeds its named exception ceiling ({ceiling}). "
            f"Recorded reason for the existing exception: {reason!r}. If this new growth is "
            "legitimate, raise the ceiling in KNOWN_OVERAGES (tests/test_module_size_"
            "convention_260816.py) in the same commit, with an updated reason -- otherwise "
            "split the module along a feature/callback-prefix seam (see CONVENTIONS.md)."
        )
    else:
        assert lines <= DEFAULT_CEILING, (
            f"{path.name}: {lines} lines exceeds the default ceiling ({DEFAULT_CEILING}, "
            f"guideline is ~{GUIDELINE}). Either split the module along a feature/"
            "callback-prefix seam (see CONVENTIONS.md's shared-router technique), or, if the "
            "growth is a deliberate and reviewed exception, add a named entry to "
            "KNOWN_OVERAGES in this file, in the same commit, with a one-line reason."
        )
