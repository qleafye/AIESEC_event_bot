"""Тест компетенций — чистый подсчёт баллов и уровней."""
from __future__ import annotations

import json

from services import quiz


def _opts():
    return {
        1: [{"id": 11, "points": {1: 3}}, {"id": 12, "points": {1: 1}}],
        2: [{"id": 21, "points": {1: 0, 2: 4}}, {"id": 22, "points": {1: 2}}],
    }


def test_max_points():
    assert quiz.max_points_by_competency(_opts()) == {1: 5, 2: 4}
    assert quiz.max_points_by_competency({1: [{"id": 1, "points": {}}]}) == {}


def _levels(*thresholds):
    return [{"id": i + 1, "threshold": t} for i, t in enumerate(thresholds)]


def test_percent_levels_table():
    lv = _levels(0, 50, 80)
    by_threshold = lambda c, m: (quiz.level_for(c, m, lv, "percent") or {}).get("threshold")
    assert by_threshold(0, 5) == 0
    assert by_threshold(2, 5) == 0
    assert by_threshold(5, 10) == 50
    assert by_threshold(4, 5) == 80
    assert by_threshold(399, 500) == 50  # 79.8% не даёт 80


def test_points_mode():
    lv = _levels(0, 3, 6)
    assert quiz.level_for(6, 10, lv, "points")["threshold"] == 6
    assert quiz.level_for(5, 10, lv, "points")["threshold"] == 3


def test_below_all_thresholds_and_zero_max():
    lv = _levels(20, 50)
    assert quiz.level_for(1, 10, lv, "percent") is None
    assert quiz.level_for(0, 0, _levels(0), "percent") is None


def test_unknown_competency_skipped():
    scores = quiz.build_scores({1: 11, 2: 21}, _opts(), _levels(0), "percent", {1})
    assert set(scores) == {"1"}
    assert scores["1"] == {"points": 3, "max": 5, "level_id": 1}


def test_build_scores_json_safe():
    scores = quiz.build_scores({1: 11, 2: 22}, _opts(), _levels(0, 50), "percent")
    assert all(isinstance(k, str) for k in scores)
    assert json.loads(json.dumps(scores)) == scores
    assert scores["1"]["points"] == 5 and scores["2"]["points"] == 0
