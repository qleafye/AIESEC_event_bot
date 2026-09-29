"""Общий модуль `chat_score.py`: формула рейтинга чата на нормализованных записях.

Одна формула на всех — тул по экспорту, реестр настроек и дашборд берут веса и расчёт отсюда.
Записи собираются в тесте руками: текста сообщений в ChatRecord нет вовсе, только длина.
"""
import dataclasses
from datetime import date, datetime, timedelta

from core import chat_score as cs

A, B, C = 101, 102, 103
T0 = datetime(2026, 9, 1, 10, 0, 0)


def _rec(mid, author, ts, text_len=10, **extra):
    return cs.ChatRecord(message_id=mid, author_id=author, ts=ts, text_len=text_len, **extra)


def _score(records, **kwargs):
    aggs = cs.aggregate_records(records, **kwargs)
    return aggs, cs.score_authors(aggs)


def test_bursts_merge_within_gap_only():
    records = [
        _rec(1, A, T0),
        _rec(2, A, T0 + timedelta(seconds=60)),    # та же реплика
        _rec(3, A, T0 + timedelta(seconds=240)),   # +180 с — новая
    ]
    aggs, _ = _score(records, burst_gap=120)
    assert [b.length for b in aggs[A].bursts] == [20, 10]


def test_burst_scores_by_kind():
    records = [
        _rec(1, A, T0, text_len=80),
        _rec(2, B, T0, text_len=0, has_media=True),
        _rec(3, C, T0, text_len=0, has_media=True, is_sticker=True),
        _rec(4, 104, T0, text_len=0),
    ]
    _, scores = _score(records)
    assert scores[A]["volume"] == 2.0
    assert scores[B]["volume"] == 1.0
    assert scores[C]["volume"] == 0.5
    assert scores[104]["volume"] == 0.0


def test_day_cap_limits_flood():
    records = [_rec(i, A, T0 + timedelta(minutes=3 * i), text_len=5) for i in range(40)]
    _, scores = _score(records)
    assert scores[A]["volume"] == 25.0


def test_reply_credits_both_sides_and_is_capped_per_message():
    records = [_rec(1, B, T0)]
    records += [
        _rec(10 + i, A, T0 + timedelta(minutes=10 * (i + 1)),
             reply_to_message_id=1, reply_to_author_id=B)
        for i in range(7)
    ]
    aggs, scores = _score(records)
    assert aggs[A].replies_given == 7
    assert aggs[B].replies_received == 7
    assert scores[B]["resonance"] == 2.0 * 5


def test_single_reply_gives_two_points_resonance():
    records = [_rec(1, B, T0), _rec(2, A, T0 + timedelta(minutes=5),
                                    reply_to_message_id=1, reply_to_author_id=B)]
    aggs, scores = _score(records)
    assert (aggs[A].replies_given, aggs[B].replies_received) == (1, 1)
    assert scores[B]["resonance"] == 2.0


def test_reply_to_self_or_to_nobody_is_not_counted():
    records = [
        _rec(1, A, T0),
        _rec(2, A, T0 + timedelta(hours=1), reply_to_message_id=1, reply_to_author_id=A),
        _rec(3, A, T0 + timedelta(hours=2), reply_to_message_id=99, reply_to_author_id=None),
    ]
    aggs, _ = _score(records)
    assert aggs[A].replies_given == 0
    assert aggs[A].replies_received == 0


def test_reactions_capped_and_givers_credited_even_without_messages():
    records = [_rec(1, A, T0, reactions_received=12, reaction_giver_ids=(B, C))]
    aggs, scores = _score(records)
    assert scores[A]["resonance"] == 0.5 * 10
    assert aggs[B].messages == 0
    assert scores[B]["giving"] == 0.25
    assert scores[C]["giving"] == 0.25


def test_reply_in_window_to_message_before_since_credits_target():
    records = [
        _rec(1, A, datetime(2026, 8, 31, 23, 0)),
        _rec(2, B, datetime(2026, 9, 1, 10, 0), reply_to_message_id=1, reply_to_author_id=A),
    ]
    aggs = cs.aggregate_records(records, since=date(2026, 9, 1), until=date(2026, 9, 2))
    assert aggs[A].messages == 0
    assert aggs[A].replies_received == 1


def test_regularity_counts_distinct_days_inside_window():
    records = [
        _rec(1, A, datetime(2026, 9, 1, 10)),
        _rec(2, A, datetime(2026, 9, 1, 18)),
        _rec(3, A, datetime(2026, 9, 2, 10)),
        _rec(4, A, datetime(2026, 9, 5, 10)),  # вне окна
    ]
    aggs = cs.aggregate_records(records, since=date(2026, 9, 1), until=date(2026, 9, 3))
    scores = cs.score_authors(aggs)
    assert scores[A]["regularity"] == 3.0 * 2


def test_channel_posts_never_become_authors():
    records = [
        _rec(1, A, T0, is_channel_post=True),
        _rec(2, B, T0 + timedelta(minutes=1)),
    ]
    aggs = cs.aggregate_records(records)
    assert A not in aggs
    assert set(aggs) == {B}


def test_weights_from_settings_parses_and_falls_back():
    weights, gap = cs.weights_from_settings({"chat_rating_reply_weight": "1,5"})
    assert weights["resonance_reply"] == 1.5
    assert gap == cs.DEFAULT_BURST_GAP
    for bad in ("abc", "-3", None, "nan", "inf"):
        weights, _ = cs.weights_from_settings({"chat_rating_reply_weight": bad})
        assert weights["resonance_reply"] == cs.WEIGHTS["resonance_reply"]
    weights, _ = cs.weights_from_settings({"chat_rating_day_weight": "0"})
    assert weights["regularity_per_day"] == 0.0
    weights, _ = cs.weights_from_settings({"chat_rating_reply_cap": "7.0"})
    assert weights["resonance_reply_cap"] == 7 and isinstance(weights["resonance_reply_cap"], int)
    _, gap = cs.weights_from_settings({"chat_rating_burst_gap_seconds": "300"})
    assert gap == 300
    for bad_gap in ("0", "-5", "x"):
        _, gap = cs.weights_from_settings({"chat_rating_burst_gap_seconds": bad_gap})
        assert gap == cs.DEFAULT_BURST_GAP
    assert cs.weights_from_settings(None)[0] == cs.WEIGHTS


def test_setting_keys_cover_every_weight():
    assert set(cs.SETTING_KEYS) == set(cs.WEIGHTS)
    assert len(set(cs.SETTING_KEYS.values())) == len(cs.WEIGHTS)


def test_describe_formula_matches_tool_header():
    assert cs.describe_formula(cs.WEIGHTS) == (
        "Формула балла: score = volume + resonance + regularity + giving. "
        "Реплика: 1+min(3, log2(1+длина/80)) за текст, "
        "1 за медиа без текста, 0.5 за стикер/GIF; "
        "объём дня — не выше 25. "
        "Резонанс = 2×ответы(≤5/сообщение) "
        "+ 0.5×реакции(≤10/сообщение). "
        "Регулярность = 3×активных дней. "
        "Отдача = 0.25×поставленных реакций."
    )


def test_chat_record_has_no_text_field():
    names = [f.name for f in dataclasses.fields(cs.ChatRecord)]
    assert [n for n in names if "text" in n] == ["text_len"]


def test_module_is_stdlib_only():
    import pathlib
    src = pathlib.Path(cs.__file__).read_text(encoding="utf-8")
    imports = [ln.split()[1].split(".")[0] for ln in src.splitlines()
               if ln.startswith(("import ", "from "))]
    assert set(imports) <= {"math", "dataclasses", "collections", "datetime", "__future__"}


def test_zero_length_base_falls_back_to_default():
    # 0 в делителе формулы уронил бы дашборд делением на ноль.
    weights, _ = cs.weights_from_settings({"chat_rating_length_base": "0"})
    assert weights["burst_log_base"] == cs.WEIGHTS["burst_log_base"]
