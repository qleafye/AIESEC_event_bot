"""Phase 09.3 (CITY-09) — СТРУКТУРНЫЙ СТОРОЖ: снесённые пер-городные пикеры не возвращаются.

Контракт файла: «Фаза 09.3 свела выбор города к шапке; ни один из снесённых пикеров не
должен вернуться.» Планы 04-07 сняли ТРИ независимых входа в выбор города, заменив их одним
контекстом — шапкой (`admin_selected_city`):
    - `settings_city:*` / `settings_city_pick:*` / `settings_city_clear:*` /
      `settings_city_clear_go:*` / `_per_city_screen` — редактор настройки (09.3-06);
    - `menu_city` / `menu_city_pick:*` / `menu_city_toggle:*` / `menu_city_clear:*` /
      `menu_city_clear_go:*` / `_menu_city_text` / `_menu_city_kb` /
      `_menu_city_list_screen` — экран кнопок меню (09.3-07, этот план).

Идиома `_non_comment_source` (тот же приём, что `tests/test_roles_phase8.py::
test_gate_no_legacy_admin_check_remains`): исходник читается целиком, но строки, целиком
являющиеся комментарием (первый непробельный символ — «#»), выбрасываются -- объясняющий
комментарий («старый пикер `menu_city_pick` удалён») не может ни завалить, ни фальшиво
озеленить гейт литералом внутри самого себя.
"""
from pathlib import Path

from handlers.admin_caps import ADMIN_CAPS, required_capability


REPO_ROOT = Path(__file__).resolve().parent.parent


def _non_comment_source(path: Path) -> str:
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()[:1] != "#"
    )


# ── Снесённые колбэки не вернулись в handlers/admin.py ──────────────────────────────────────

_DEAD_SUBSTRINGS = [
    "settings_city:",
    "settings_city_pick",
    "settings_city_clear",
    "_per_city_screen",
    "menu_city_pick",
    "menu_city_toggle",
    "menu_city_clear",
    "_menu_city_text",
    "_menu_city_kb",
]


def test_admin_py_has_no_legacy_picker_substrings():
    source = _non_comment_source(REPO_ROOT / "handlers" / "admin.py")
    for needle in _DEAD_SUBSTRINGS:
        assert needle not in source, f"снесённый пикер вернулся: {needle!r}"


def test_admin_py_has_no_legacy_menu_city_list_screen_helper():
    # Отдельная проверка -- "_menu_city_list_screen" содержит "_menu_city_kb"/"_menu_city_text"
    # только как под-подстроку своего собственного имени в некоторых grep-движках; здесь имя
    # целиком, чтобы не полагаться на порядок подстрок из списка выше.
    source = _non_comment_source(REPO_ROOT / "handlers" / "admin.py")
    assert "_menu_city_list_screen" not in source


# ── Снесённые ключи не вернулись в ADMIN_CAPS ────────────────────────────────────────────────

def test_admin_caps_has_no_legacy_city_picker_keys():
    """Импортируем сам словарь (а не читаем файл строками) -- проверка бьёт по факту
    резолва, а не по тексту исходника."""
    for key in ADMIN_CAPS:
        assert not key.startswith("settings_city"), key
        assert not key.startswith("menu_city"), key


# ── Новые ключи реально закрыты правом "settings", а не просто присутствуют в словаре ──────

def test_new_city_edit_prefixes_resolve_to_settings_capability():
    samples = [
        "settings_edit_city:start_text",
        "settings_reset_city:start_text",
        "settings_reset_city_go:start_text:spb",
        "menu_reset_city",
        "menu_reset_city_go:spb",
    ]
    for cb in samples:
        assert required_capability(callback_data=cb) == "settings", cb


def test_every_admin_caps_key_with_surviving_prefixes_resolves_to_settings():
    """Каждый ключ словаря (не только точечный пример выше), начинающийся с одного из трёх
    выживших семейств, обязан резолвиться в право "settings" -- случайно добавленный ключ с
    другим правом молча расширил бы доступ к пер-городному письму."""
    prefixes = ("settings_edit_city", "settings_reset_city", "menu_reset_city")
    matched = [key for key in ADMIN_CAPS if key.startswith(prefixes)]
    assert matched, "ни один из выживших префиксов не найден в ADMIN_CAPS -- сторож проверял бы пустоту"
    for key in matched:
        cap = required_capability(callback_data=key.rstrip("*") if key.endswith("*") else key)
        assert cap == "settings", key


def test_menu_reset_city_exact_key_wins_over_go_prefix():
    """Точное имя "menu_reset_city" не должно перехватываться префиксом
    "menu_reset_city_go:*" -- required_capability резолвит точное совпадение первым."""
    assert "menu_reset_city" in ADMIN_CAPS
    assert required_capability(callback_data="menu_reset_city") == ADMIN_CAPS["menu_reset_city"]
