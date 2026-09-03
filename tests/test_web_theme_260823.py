"""Phase 19.1 Plan 02 Task 2 (D-03/D-04/D-07): `web_theme.py` — единственный корневой модуль,
где живёт знание о пресетах оформления (BlueBook/YouLead/Своя). Чистые функции — контраст,
осветление под тёмную тему, разрешение ручек поверх пресета, сборка текста CSS.

Тесты писаны ПЕРВЫМИ (RED) — на момент коммита `web_theme.py` ещё не существует, весь файл
обязан упасть с `ModuleNotFoundError`/`ImportError`, не пройти молча.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import web_theme

ROOT = Path(__file__).resolve().parent.parent


# ── contrast_ratio / relative_luminance ─────────────────────────────────────────────────

def test_contrast_ratio_white_vs_black_is_21():
    assert web_theme.contrast_ratio("#FFFFFF", "#000000") == pytest.approx(21.0, abs=0.05)


def test_contrast_ratio_same_color_is_1():
    assert web_theme.contrast_ratio("#037EF3", "#037EF3") == pytest.approx(1.0, abs=0.001)


# ── lighten_to_contrast ──────────────────────────────────────────────────────────────────

def test_lighten_to_contrast_reaches_target_against_dark_surface():
    result = web_theme.lighten_to_contrast("#037EF3", "#1C1F25")
    assert web_theme.contrast_ratio(result, "#1C1F25") >= 4.5


def test_lighten_to_contrast_never_darker_than_original():
    original = "#037EF3"
    result = web_theme.lighten_to_contrast(original, "#1C1F25")
    # «Осветление» = каждый канал не убывает.
    orig_rgb = tuple(int(original[i:i + 2], 16) for i in (1, 3, 5))
    result_rgb = tuple(int(result[i:i + 2], 16) for i in (1, 3, 5))
    assert all(r >= o for r, o in zip(result_rgb, orig_rgb))


def test_lighten_to_contrast_already_contrastive_color_unchanged():
    # Белый на тёмном фоне уже контрастен — шаг осветления не требуется.
    assert web_theme.lighten_to_contrast("#FFFFFF", "#1C1F25") == "#FFFFFF"


def test_lighten_to_contrast_hopeless_case_caps_at_white_and_terminates():
    # Фон почти белый — 4.5:1 недостижим осветлением к белому, но цикл обязан завершиться.
    result = web_theme.lighten_to_contrast("#037EF3", "#FEFEFE")
    assert result == "#FFFFFF"


# ── resolve_theme ────────────────────────────────────────────────────────────────────────

def test_resolve_theme_empty_settings_gives_full_bluebook_handle_set():
    resolved = web_theme.resolve_theme({})
    assert resolved["preset"] == "bluebook"
    assert resolved["accent"] == "#037EF3"
    assert resolved["secondary"] == "#F48924"
    assert resolved["bg"] == "#F3F4F7"
    assert resolved["heading_font"] == "raleway"
    assert resolved["playful_tone"] == "off"
    assert resolved["pattern_enabled"] == "off"


def test_resolve_theme_youlead_preset_is_blue_with_italic_and_pattern():
    # D-09 (03.09): принятые макеты сняты на синей палитре BlueBook — «событийность» пресета
    # «ЮЛид» несут паттерн, курсив и тон, а не смена акцента на оранжево-красный (был #F85A40
    # в версии 19.1-02, снят этим планом).
    resolved = web_theme.resolve_theme({"miniapp_theme_preset": "youlead"})
    assert resolved["accent"] == "#037EF3"
    assert resolved["secondary"] == "#F48924"
    assert resolved["heading_font"] == "raleway_italic"
    assert resolved["plate_pattern"] == "youlead"
    assert resolved["pattern_enabled"] == "on"


def test_presets_differ_by_pattern_and_heading_not_by_accent():
    bluebook = web_theme.PRESETS["bluebook"]
    youlead = web_theme.PRESETS["youlead"]
    assert bluebook["accent"] == youlead["accent"] == "#037EF3"
    assert bluebook["plate_pattern"] != youlead["plate_pattern"]
    assert bluebook["heading_font"] != youlead["heading_font"]
    assert bluebook["playful_tone"] != youlead["playful_tone"]


def test_theme_css_youlead_turns_headings_italic():
    css_youlead = web_theme.theme_css_text(web_theme.resolve_theme({"miniapp_theme_preset": "youlead"}))
    css_bluebook = web_theme.theme_css_text(web_theme.resolve_theme({"miniapp_theme_preset": "bluebook"}))
    assert "--font-heading-style: italic;" in css_youlead
    assert "--font-heading-style: normal;" in css_bluebook


def test_resolve_theme_garbage_hex_falls_back_to_active_preset():
    # D-09: оба пресета синие (#037EF3) — «активный пресет» здесь по-прежнему youlead, просто
    # его собственный accent теперь совпадает с bluebook, это не регресс отката к пресету.
    resolved = web_theme.resolve_theme({
        "miniapp_theme_preset": "youlead",
        "miniapp_accent": "#037EF3; } body { background: url(x) }",
        "miniapp_theme_secondary": "not-a-color",
        "miniapp_theme_bg": "",
    })
    assert resolved["accent"] == "#037EF3"  # пресет youlead, не литерал и не пусто
    assert resolved["secondary"] == "#F48924"
    assert resolved["bg"] == "#F3F4F7"


def test_resolve_theme_valid_handle_overrides_preset_starting_point():
    resolved = web_theme.resolve_theme({
        "miniapp_theme_preset": "bluebook",
        "miniapp_accent": "#123456",
    })
    assert resolved["accent"] == "#123456"
    # Остальные ручки по-прежнему из пресета.
    assert resolved["secondary"] == "#F48924"


def test_resolve_theme_unknown_preset_falls_back_to_bluebook():
    resolved = web_theme.resolve_theme({"miniapp_theme_preset": "does-not-exist"})
    assert resolved["preset"] == "bluebook"
    assert resolved["accent"] == "#037EF3"


# ── theme_css_text ───────────────────────────────────────────────────────────────────────

def test_theme_css_text_contains_light_and_dark_declarations():
    resolved = web_theme.resolve_theme({})
    css = web_theme.theme_css_text(resolved)
    assert ":root {" in css
    assert "--accent: #037EF3;" in css
    assert "--secondary: #F48924;" in css
    assert "--bg: #F3F4F7;" in css
    assert "--font-heading:" in css
    assert "--font-heading-style: normal;" in css
    assert ':root[data-theme="dark"] {' in css
    dark_block = css.split(':root[data-theme="dark"] {', 1)[1]
    assert "--accent:" in dark_block
    assert "--secondary:" in dark_block
    # Тёмный акцент отличается от светлого (осветлён под контраст).
    light_accent = "#037EF3"
    assert light_accent not in dark_block.split("}")[0] or "--accent: #037EF3;" not in dark_block


def test_theme_css_text_never_substitutes_unvalidated_value():
    resolved = {
        "preset": "bluebook",
        "accent": "#037EF3; } body { background: url(x) }",
        "secondary": "javascript:alert(1)",
        "bg": "",
        "heading_font": "raleway",
    }
    css = web_theme.theme_css_text(resolved)
    assert "url(x)" not in css
    assert "javascript:" not in css
    assert "body {" not in css
    # Фолбэк — значение пресета bluebook.
    assert "--accent: #037EF3;" in css
    assert "--secondary: #F48924;" in css
    assert "--bg: #F3F4F7;" in css


def test_theme_css_text_youlead_heading_font_is_italic():
    resolved = web_theme.resolve_theme({"miniapp_theme_preset": "youlead"})
    css = web_theme.theme_css_text(resolved)
    assert "--font-heading-style: italic;" in css


# ── Phase 23.1-02 (D-05/T-23.1-04): ручка `plate_pattern` — --plate-pattern* в CSS ─────────

def test_plate_pattern_builtin_youlead_name_resolves_to_asset_url():
    resolved = web_theme.resolve_theme({"miniapp_theme_preset": "youlead"})
    css = web_theme.theme_css_text(resolved)
    assert '--plate-pattern: url("/app/static/pattern/youlead.webp");' in css


def test_plate_pattern_none_gives_literal_none():
    resolved = web_theme.resolve_theme({"miniapp_theme_preset": "bluebook"})
    css = web_theme.theme_css_text(resolved)
    assert "--plate-pattern: none;" in css


def test_plate_pattern_valid_file_id_goes_through_file_proxy_at_low_opacity():
    file_id = "AgACAgIAAxkBAAI" + "c" * 15  # 30 символов, проходит FILE_ID-регэксп
    resolved = web_theme.resolve_theme({
        "miniapp_theme_preset": "bluebook",
        "miniapp_theme_pattern": file_id,
    })
    assert resolved["plate_pattern"] == file_id
    css = web_theme.theme_css_text(resolved)
    assert f'--plate-pattern: url("/app/api/file/{file_id}");' in css
    assert "--plate-pattern-opacity: 0.2;" in css


def test_plate_pattern_garbage_value_never_reaches_css_output():
    for garbage in ("'; }", "../../etc/passwd", "a" * 5):
        resolved = web_theme.resolve_theme({
            "miniapp_theme_preset": "bluebook",
            "miniapp_theme_pattern": garbage,
        })
        # Мусор не проходит валидацию ручки — resolve_theme откатывается к пресету.
        assert resolved["plate_pattern"] == "none"
        css = web_theme.theme_css_text(resolved)
        assert "passwd" not in css
        assert "etc" not in css
        assert "'; }" not in css
        assert garbage not in css

        # Тот же откат, даже если мусор попадает напрямую в theme_css_text, минуя resolve_theme
        # (T-19.1-05: вторая проверка, а не доверие уже пришедшему словарю).
        direct_css = web_theme.theme_css_text({"preset": "bluebook", "plate_pattern": garbage})
        assert "passwd" not in direct_css
        assert "etc" not in direct_css
        assert "'; }" not in direct_css
        assert garbage not in direct_css
        assert "--plate-pattern: none;" in direct_css


# ── Quick 260904-183 (BACKLOG-0904-REALTALK-PRESET): третий пресет «РилТолк» ──────────────

def test_resolve_theme_realtalk_preset_is_purple_orange_with_pattern():
    resolved = web_theme.resolve_theme({"miniapp_theme_preset": "realtalk"})
    assert resolved["accent"] == "#7552CC"
    assert resolved["secondary"] == "#FF8B10"
    assert resolved["bg"] == "#F3F4F7"
    assert resolved["heading_font"] == "raleway"
    assert resolved["plate_pattern"] == "realtalk"
    assert resolved["pattern_enabled"] == "on"
    assert resolved["playful_tone"] == "on"


def test_theme_css_realtalk_has_purple_accent_and_pattern_asset():
    resolved = web_theme.resolve_theme({"miniapp_theme_preset": "realtalk"})
    css = web_theme.theme_css_text(resolved)
    assert '--plate-pattern: url("/app/static/pattern/realtalk.webp");' in css
    assert "--font-heading-style: normal;" in css


def test_realtalk_accent_contrast_meets_threshold_unchanged():
    # contrast_ratio("#7552CC", "#FFFFFF") == 5.48 (посчитано заранее в плане) — porog 4.5
    # пройден, darken_to_contrast должен вернуть акцент без изменений.
    assert web_theme.contrast_ratio("#7552CC", "#FFFFFF") >= 4.5
    resolved = web_theme.resolve_theme({"miniapp_theme_preset": "realtalk"})
    assert resolved["accent_text"] == "#7552CC"


# ── aiogram-free import ─────────────────────────────────────────────────────────────────

def test_import_web_theme_does_not_load_aiogram():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    snippet = (
        "import web_theme\nimport sys\n"
        "print(sorted(m for m in sys.modules if m == 'aiogram' or m.startswith('aiogram.')))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet], cwd=str(ROOT), env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    loaded = eval(proc.stdout.strip().splitlines()[-1])  # noqa: S307 — список строк из нашего же print
    assert loaded == []
