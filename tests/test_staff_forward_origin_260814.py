"""Quick 260814-fwd: добавление менеджера пересылкой сломалось об Bot API 7.0.

Bot API 7.0 (январь 2024) убрал из Message поля forward_from / forward_date /
forward_sender_name и заменил их одним forward_origin. Телеграм старые поля больше не шлёт,
поэтому `_resolve_staff_input` видел None у ЛЮБОЙ пересылки, проваливался в разбор текста и
отвечал «Не понял, кого добавить». Живой сценарий «перешлите сообщение от человека» —
основной способ добавить менеджера (ADMIN_GUIDE §22) — не работал вовсе.

Старые тесты этого не ловили, потому что подставляли `forward_from` вручную: фейк проходил,
реальный апдейт — нет. Здесь строятся НАСТОЯЩИЕ aiogram-модели MessageOrigin*, чтобы тест
падал ровно там же, где падал прод.
"""
import datetime

from aiogram.types import (
    Chat,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    User,
)

from handlers.admin import _resolve_staff_input


def _msg(**kwargs) -> Message:
    return Message(
        message_id=1,
        date=datetime.datetime.now(),
        chat=Chat(id=1, type="private"),
        **kwargs,
    )


def _now():
    return datetime.datetime.now()


def test_forward_origin_user_resolves_telegram_id():
    """Основной путь: переслали сообщение обычного человека."""
    user = User(id=900555, is_bot=False, first_name="Иван", username="ivan")
    msg = _msg(
        forward_origin=MessageOriginUser(type="user", date=_now(), sender_user=user),
        text="привет",  # у пересланного текстового сообщения текст ЕСТЬ — и раньше он же
    )                    # уходил в разбор как «имя менеджера», давая «Не понял»
    telegram_id, marker = _resolve_staff_input(msg)
    assert telegram_id == 900555
    assert marker is None


def test_forward_origin_hidden_user_asks_for_username_or_id():
    """Приватность «скрывать аккаунт при пересылке» — id физически не приходит."""
    msg = _msg(
        forward_origin=MessageOriginHiddenUser(
            type="hidden_user", date=_now(), sender_user_name="Иван"
        ),
        text="привет",
    )
    telegram_id, marker = _resolve_staff_input(msg)
    assert telegram_id is None
    assert "@username" in marker and "id" in marker


def test_forward_from_chat_or_channel_is_not_a_person():
    """Пересылка из чата/канала — там нет человека, которого можно назначить менеджером."""
    chat = Chat(id=-100123, type="supergroup", title="Оргчат")
    channel = Chat(id=-100456, type="channel", title="Канал")

    from_chat = _msg(
        forward_origin=MessageOriginChat(type="chat", date=_now(), sender_chat=chat),
        text="объявление",
    )
    from_channel = _msg(
        forward_origin=MessageOriginChannel(
            type="channel", date=_now(), chat=channel, message_id=7
        ),
        text="пост",
    )

    for msg in (from_chat, from_channel):
        telegram_id, marker = _resolve_staff_input(msg)
        assert telegram_id is None
        assert marker and "@username" in marker


def test_plain_username_and_numeric_id_still_work():
    """Две другие формы ввода не задеты правкой."""
    assert _resolve_staff_input(_msg(text="@ivan")) == (None, "@ivan")
    assert _resolve_staff_input(_msg(text="  900555  ")) == (900555, None)


def test_garbage_text_still_returns_human_error():
    telegram_id, marker = _resolve_staff_input(_msg(text="Иван Петров"))
    assert telegram_id is None
    assert "Не понял" in marker
