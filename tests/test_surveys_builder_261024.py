"""Конструктор анкеты в админке: права, развилка входа, черновик, правка, превью, отправка."""
from handlers.access.admin_caps import required_capability


def test_survey_callbacks_and_states_require_broadcast():
    for key in ("psv_new", "psv_draft:1", "psv_qadd:1", "psv_res:1:0", "psv_copy:1", "poll_new_single"):
        assert required_capability(callback_data=key) == "broadcast", key
    assert required_capability(raw_state="SurveyBuild:title") == "broadcast"
    assert required_capability(raw_state="SurveyBuild:q_options") == "broadcast"
    assert required_capability(callback_data="sv:s:1") is None
