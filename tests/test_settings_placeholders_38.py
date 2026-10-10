import subprocess
import sys

import domain.settings.placeholders as sp
from domain.settings.schema import SETTINGS_SCHEMA


def test_missing_detected():
    r = sp.check("session_enroll_confirmed_text", "Оплатите вовремя", "Оплатите до {deadline}")
    assert r.missing and r.missing[0][0] == "deadline" and r.missing[0][1]


def test_unknown_with_suggestion():
    r = sp.check("session_enroll_confirmed_text", "до {dedline}", "до {deadline}")
    assert ("dedline", "deadline") in r.unknown


def test_unknown_without_suggestion():
    r = sp.check("session_enroll_confirmed_text", "{zzz}", "")
    assert r.unknown == [("zzz", None)]


def test_braces_without_name_ignored():
    r = sp.check("session_enroll_confirmed_text", '{ } {1} {"a": 1}', "{ } {1}")
    assert r.ok


def test_per_city_key_resolves_base():
    assert "deadline" in sp.expected_placeholders("session_enroll_confirmed_text__city__msk")


def test_every_registry_placeholder_has_label():
    bad = []
    for key, entry in SETTINGS_SCHEMA.items():
        default = entry.get("default")
        for s in default if isinstance(default, list) else [default]:
            if isinstance(s, str):
                exp = sp.expected_placeholders(key)
                for n in sp.TOKEN_RE.findall(s):
                    if not exp.get(n):
                        bad.append((key, n))
    assert not bad, bad


def test_prompt_only_keys_covered():
    assert len(sp.PROMPT_ONLY_PLACEHOLDERS) == 9
    for key, names in sp.PROMPT_ONLY_PLACEHOLDERS.items():
        exp = sp.expected_placeholders(key)
        for n in names:
            assert exp.get(n), (key, n)


def test_every_prompt_token_is_known_to_its_key():
    """Подстановка, которую подсказка обещает менеджеру, не должна считаться «неизвестной»."""
    bad = []
    for key, entry in SETTINGS_SCHEMA.items():
        prompt = entry.get("prompt")
        if isinstance(prompt, str):
            exp = sp.expected_placeholders(key)
            bad += [(key, n) for n in sp.TOKEN_RE.findall(prompt) if n not in exp]
    assert not bad, bad


def test_hint_text():
    h = sp.hint("session_enroll_confirmed_text")
    assert "{deadline}" in h and "Скобки бот заменит сам" in h
    assert sp.hint("no_such_key") == ""


def test_problem_text():
    r = sp.check("session_enroll_confirmed_text", "до {dedline}", "до {deadline}")
    t = sp.problem_text("session_enroll_confirmed_text", r)
    assert "Не знаю {dedline} — может, {deadline}?" in t


def test_module_does_not_load_aiogram():
    code = "import sys, domain.settings.placeholders; sys.exit(1 if 'aiogram' in sys.modules else 0)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert r.returncode == 0, r.stderr
