"""Правила города для рейтинга чата (пресет СПб «коины») — чистая функция score_city_rules.

Никаких записей в БД и сообщений: только расчёт по записям чата и датам событий.
"""
from datetime import date, datetime, timedelta

from core import chat_score as cs

TEAM, A, B = 900, 101, 102
T0 = datetime(2026, 9, 1, 10, 0, 0)


def _rec(mid, author, ts=T0, text_len=10, **extra):
    return cs.ChatRecord(message_id=mid, author_id=author, ts=ts, text_len=text_len, **extra)


def _reply(mid, author, post_id, text_len, ts=None, post_author=TEAM):
    return _rec(mid, author, ts or T0 + timedelta(minutes=mid), text_len,
                reply_to_message_id=post_id, reply_to_author_id=post_author)


def _rules(**over):
    rules = dict(cs.RULES_DEFAULTS)
    rules.update(over)
    return rules


def _score(records=(), *, rules=None, referral=None, social=None, checkin=None, **kw):
    return cs.score_city_rules(
        list(records), team_ids={TEAM}, rules=rules or _rules(),
        referral_dates=referral or {}, social_dates=social or {}, checkin_days=checkin or {},
        **kw,
    )


def test_defaults_are_spb_preset():
    assert cs.RULES_DEFAULTS == {
        "post_min_chars": 0, "comment_points": 10, "valuable_min_chars": 500,
        "valuable_points": 20, "referral_points": 5, "social_points": 15, "social_max": 3,
        "checkin_points": 20, "checkin_max": 2,
    }
    assert cs.RULE_SETTING_KEYS["comment_points"] == "chat_rules_comment_points"
    assert set(cs.RULE_SETTING_KEYS) == set(cs.RULES_DEFAULTS)


def test_any_team_message_is_a_post_by_default():
    res = _score([_rec(1, TEAM, text_len=5), _reply(2, A, 1, 30)])
    assert res[A]["comment_points"] == 10
    assert res[A]["comments"] == 1


def test_post_min_chars_filters_short_team_messages():
    res = _score([_rec(1, TEAM, text_len=150), _reply(2, A, 1, 30)],
                 rules=_rules(post_min_chars=200))
    assert A not in res


def test_channel_post_is_a_post():
    res = _score([_rec(1, None, text_len=50, is_channel_post=True),
                  _reply(2, A, 1, 30, post_author=None)])
    assert res[A]["comment_points"] == 10


def test_valuable_comment_and_one_award_per_post():
    res = _score([_rec(1, TEAM), _reply(2, A, 1, 100), _reply(3, A, 1, 600)])
    assert res[A]["comment_points"] == 20
    assert res[A]["total"] == 20
    assert (res[A]["comments"], res[A]["valuable"]) == (1, 1)


def test_reply_to_non_post_or_topic_root_gives_nothing():
    records = [
        _rec(1, B),                                   # сообщение делегата — не пост
        _reply(2, A, 1, 600, post_author=B),
        _rec(3, A, T0 + timedelta(hours=1)),          # корень топика: reply_to нет
    ]
    assert _score(records) == {}
    short_team = [_rec(1, TEAM, text_len=10), _reply(2, A, 1, 50)]
    assert _score(short_team, rules=_rules(post_min_chars=100)) == {}


def test_team_never_receives_points():
    res = _score([_rec(1, TEAM), _reply(2, TEAM, 1, 600)],
                 referral={TEAM: [date(2026, 9, 1)]}, checkin={TEAM: [date(2026, 10, 30)]})
    assert TEAM not in res


def test_referrals_uncapped():
    days = [date(2026, 9, d) for d in range(1, 8)]
    res = _score(referral={A: days})
    assert res[A]["referrals"] == 7
    assert res[A]["referral_points"] == 35


def test_social_capped_and_week_view_after_cap_is_zero():
    days = [date(2026, 9, d) for d in (1, 2, 3, 10, 11)]
    res = _score(social={A: days})
    assert res[A]["social"] == 3
    assert res[A]["social_points"] == 45
    week = _score(social={A: days}, since=date(2026, 9, 8), until=date(2026, 9, 14))
    assert week.get(A, {}).get("social_points", 0) == 0


def test_checkins_capped_on_distinct_days():
    res = _score(checkin={A: [date(2026, 10, 3), date(2026, 10, 30), date(2026, 10, 31)]})
    assert res[A]["checkins"] == 2
    assert res[A]["checkin_points"] == 40


def test_week_view_is_difference_of_cumulative_totals():
    records = [
        _rec(1, TEAM, ts=datetime(2026, 9, 1, 9)),
        _reply(2, A, 1, 100, ts=datetime(2026, 9, 2, 10)),   # неделя 1: 10
        _reply(3, A, 1, 700, ts=datetime(2026, 9, 9, 10)),   # неделя 2: апгрейд до 20
    ]
    w1 = _score(records, since=date(2026, 9, 1), until=date(2026, 9, 7))
    w2 = _score(records, since=date(2026, 9, 8), until=date(2026, 9, 14))
    total = _score(records)
    assert w1[A]["comment_points"] == 10
    assert w2[A]["comment_points"] == 10
    assert total[A]["comment_points"] == 20


def test_post_before_window_still_counts():
    records = [_rec(1, TEAM, ts=datetime(2026, 8, 20)),
               _reply(2, A, 1, 50, ts=datetime(2026, 9, 2))]
    res = _score(records, since=date(2026, 9, 1), until=date(2026, 9, 7))
    assert res[A]["comment_points"] == 10


def test_zero_amount_zeroes_column_only():
    res = _score([_rec(1, TEAM), _reply(2, A, 1, 30)], rules=_rules(referral_points=0),
                 referral={A: [date(2026, 9, 1)] * 3})
    assert res[A]["referral_points"] == 0
    assert res[A]["total"] == 10


def test_rules_from_settings_parses_and_falls_back():
    rules = cs.rules_from_settings({"chat_rules_comment_points": "12,5"})
    assert rules["comment_points"] == 12.5
    rules = cs.rules_from_settings({"chat_rules_comment_points": "мусор",
                                    "chat_rules_social_max": "-1",
                                    "chat_rules_valuable_min_chars": "300.0"})
    assert rules["comment_points"] == cs.RULES_DEFAULTS["comment_points"]
    assert rules["social_max"] == cs.RULES_DEFAULTS["social_max"]
    assert rules["valuable_min_chars"] == 300 and isinstance(rules["valuable_min_chars"], int)
    assert cs.rules_from_settings(None) == cs.RULES_DEFAULTS


def test_describe_rules_is_human_text():
    lines = cs.describe_rules(cs.RULES_DEFAULTS, "баллы")
    text = "\n".join(lines)
    assert "Комментарий под сообщением команды: 10 баллы" in text
    assert "длиннее" not in text.split("\n")[0]  # длина поста не упоминается при 0
    assert "chat_rules" not in text
    with_len = cs.describe_rules(dict(cs.RULES_DEFAULTS, post_min_chars=200), "коины")
    assert "200" in with_len[0]


def test_valuable_points_zero_means_long_comment_is_regular():
    res = _score([_rec(1, TEAM), _reply(2, A, 1, 900)], rules=_rules(valuable_points=0))
    assert res[A]["comment_points"] == 10
    assert res[A]["valuable"] == 0
