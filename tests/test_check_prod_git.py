"""Сверка прод ↔ git: чистая функция и статическая проводка GIT_SHA.

Docker и сеть не вызываются. Метки и флаг грязи подставляются в compare_prod_git.
"""
from tests._paths import REPO_ROOT

import pytest
import yaml

from tools.check_prod_git import (
    SERVICES,
    compare_prod_git,
    compose_ps_argv,
    git_dirty_argv,
    git_head_argv,
    inspect_labels_argv,
)

HEAD = "a" * 40
OTHER = "b" * 40
COMPOSE = REPO_ROOT / "docker-compose.yml"
_REBUILD = "GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build"


def _matched() -> dict[str, str | None]:
    return {name: HEAD for name in SERVICES}


def test_clean_tree_and_matching_labels_exit_ok():
    ok, lines = compare_prod_git(HEAD, False, _matched())
    assert ok is True
    assert lines == [
        f"Коммит на диске: {HEAD}",
        "Дерево чистое",
        "bot: совпадает",
        "miniapp: совпадает",
        "yl26-dashboard: совпадает",
    ]


def test_head_newline_still_matches():
    ok, lines = compare_prod_git(HEAD + "\n", False, _matched())
    assert ok is True
    assert lines[0] == f"Коммит на диске: {HEAD}"


def test_dirty_tree_fails_even_when_images_match():
    ok, lines = compare_prod_git(HEAD, True, _matched())
    assert ok is False
    assert lines[1] == "Дерево грязное"
    assert [ln for ln in lines if ln.endswith("совпадает")] == [
        "bot: совпадает",
        "miniapp: совпадает",
        "yl26-dashboard: совпадает",
    ]
    assert lines[-1] == f"Пересоберите из чистого дерева: {_REBUILD}"


@pytest.mark.parametrize(
    "revision, phrase",
    [
        (OTHER, "в образе другой коммит"),
        ("", "метки нет, образ собран без GIT_SHA"),
        ("unknown", "метки нет, образ собран без GIT_SHA"),
        ("  unknown  ", "метки нет, образ собран без GIT_SHA"),
        (None, "контейнер не запущен"),
    ],
)
def test_one_service_mismatch(revision, phrase):
    revisions = _matched()
    revisions["miniapp"] = revision
    ok, lines = compare_prod_git(HEAD, False, revisions)
    assert ok is False
    assert "miniapp: " + phrase in lines
    assert "bot: совпадает" in lines
    assert "yl26-dashboard: совпадает" in lines
    assert sum(ln.startswith("Пересоберите из чистого дерева:") for ln in lines) == 1
    assert ".env" not in "\n".join(lines)


def test_missing_service_is_not_silent_success():
    ok, lines = compare_prod_git(HEAD, False, {"bot": HEAD})
    assert ok is False
    assert "miniapp: контейнер не запущен" in lines
    assert "yl26-dashboard: контейнер не запущен" in lines


def test_unknown_label_is_not_a_match_against_commit_named_unknown():
    ok, lines = compare_prod_git(
        "unknown",
        False,
        {name: "unknown" for name in SERVICES},
    )
    assert ok is False
    assert all(ln.endswith("метки нет, образ собран без GIT_SHA") for ln in lines[2:5])


def test_compare_does_not_touch_subprocess_or_files():
    assert "subprocess" not in compare_prod_git.__code__.co_names
    assert "open" not in compare_prod_git.__code__.co_names
    assert "run" not in compare_prod_git.__code__.co_names


def test_probe_commands_only_read():
    assert git_head_argv() == ["git", "rev-parse", "HEAD"]
    assert git_dirty_argv() == ["git", "diff-index", "--quiet", "HEAD"]
    assert compose_ps_argv("bot") == ["docker", "compose", "ps", "-q", "bot"]
    inspect = inspect_labels_argv("abc")
    assert inspect[:3] == ["docker", "inspect", "--format"]
    assert "org.opencontainers.image.revision" not in inspect[3]
    assert "{{json .Config.Labels}}" in inspect[3]
    for argv in (git_head_argv(), git_dirty_argv(), compose_ps_argv("miniapp"), inspect):
        assert "up" not in argv
        assert "--build" not in argv
        assert ".env" not in " ".join(argv)


@pytest.mark.parametrize("rel", ["Dockerfile", "dashboard/Dockerfile"])
def test_git_sha_label_is_after_pip_and_copy(rel):
    body = [
        ln.strip()
        for ln in (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    pip_at = max(i for i, ln in enumerate(body) if "pip install" in ln)
    copy_at = max(i for i, ln in enumerate(body) if ln.startswith("COPY "))
    arg_at = body.index("ARG GIT_SHA=unknown")
    label_at = body.index("LABEL org.opencontainers.image.revision=$GIT_SHA")
    assert pip_at < copy_at < arg_at < label_at


def test_compose_passes_git_sha_and_leaves_libretranslate():
    cfg = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = cfg["services"]
    for name in SERVICES:
        build = services[name]["build"]
        assert isinstance(build, dict)
        assert build["context"] == "."
        assert build["args"]["GIT_SHA"] == "${GIT_SHA:-unknown}"
    assert services["yl26-dashboard"]["build"]["dockerfile"] == "dashboard/Dockerfile"
    assert "dockerfile" not in services["bot"]["build"]
    assert "dockerfile" not in services["miniapp"]["build"]
    libre = services["libretranslate"]
    assert "build" not in libre
    assert libre["image"] == "libretranslate/libretranslate:v1.9.6"


def test_conventions_mention_the_check():
    text = (REPO_ROOT / "docs" / "CONVENTIONS.md").read_text(encoding="utf-8")
    assert "python tools/check_prod_git.py" in text
    assert "GIT_SHA" in text
