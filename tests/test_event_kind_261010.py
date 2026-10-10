"""Вид события в текстах делегату (`services.text_fill.event_kind`): карточка «… в цифрах» и
похожие фразы говорят «на форуме» / «на конференции» / «на мероприятии» по «🎭 Тип события»,
а не «на форуме» на любом событии. Пустой тип — форум (дефолт реестра), прежний вывод тот же."""
from services.forum import forum_stats_card as card
from services.text_fill import event_kind


def test_event_kind_by_type():
    assert event_kind("forum") == "форуме"
    assert event_kind("skillup") == "форуме"
    assert event_kind("conference") == "конференции"
    assert event_kind("custom") == "мероприятии"
    assert event_kind(None) == "форуме"
    assert event_kind("conference", "en") == "conference"
    assert event_kind("custom", "en") == "event"
    assert event_kind(None, "en") == "forum"


def test_card_labels_follow_event_type():
    assert card.label_set("ru")["days"] == "Дней на форуме"
    assert card.label_set("ru", "conference")["days"] == "Дней на конференции"
    assert card.label_set("ru", "custom")["sessions"] == "Сессий на мероприятии"
    assert card.label_set("en")["days"] == "Forum days"
    assert card.label_set("en", "conference")["sessions"] == "Conference sessions"
    assert card.label_set("en", "custom")["days"] == "Event days"
    # Остальные подписи и шаблоны не задеты.
    assert card.label_set("ru", "conference")["rank_fmt"] == "{rank} из {total}"
    assert all("{kind}" not in v and "{Kind}" not in v for v in card.label_set("ru", "custom").values())


def test_hero_caption_follows_event_type():
    assert card._hero_caption("days", 2, "ru") == "дня на форуме"
    assert card._hero_caption("days", 5, "ru", "conference") == "дней на конференции"
    assert card._hero_caption("sessions", 1, "ru", "custom") == "сессия на мероприятии"
    assert card._hero_caption("days", 1, "en", "conference") == "day at the conference"
    assert card._hero_caption("sessions", 3, "en") == "sessions at the forum"


def test_card_renders_for_conference():
    png = card.render_card_sync(
        dict(card._PREVIEW_STATS), None, "ru", "#123456", event_name="Съезд", event_type="conference",
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
