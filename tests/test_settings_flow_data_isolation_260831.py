"""Сторож изоляции данных потоков правки настроек (EditSetting).

Найдено ревью фазы 20 как живой баг с потерей данных: три входа в `EditSetting.waiting_for_*`
домешивали свои ключи в то, что осталось в состоянии от прошлого экрана (`update_data`), а
приёмник документа разбирал `raw_file_key` РАНЬШЕ `file_setting`. Достижимая
последовательность — экран, с которого менеджер ушёл, висит на одно нажатие вверх по чату:

  1. «🧾 PDF согласий» -> нажал согласие: в данных `raw_file_key = consent_pdf_offer`;
  2. ничего не прислал, пролистал вверх, нажал «📎 🎁 Бонус за регистрацию»: к данным
     ДОБАВЛЯЕТСЯ `file_setting = reg_bonus`, старый ключ жив;
  3. прислал PDF бонуса -> он молча уезжает в `consent_pdf_offer` («✅ PDF согласия
     сохранён!»), а `reg_bonus_doc_file_id` не пишется вовсе.

Юридический документ, который видит делегат, подменяется посторонним файлом, и в интерфейсе
об этом нет ни намёка. Инвариант, который здесь стережётся: КАЖДЫЙ вход в поток задаёт данные
FSM целиком (`set_data`), поэтому ключ прошлого потока пережить переход не может.

pytest-asyncio в этом окружении нет — каждый async-вызов через asyncio.run(), config.DB_PATH
смотрит в tmp_path (конвенция проекта).
"""
import asyncio

from database import db
from handlers import admin_settings as st
from handlers.states import EditSetting

from tests.test_admin_sections_ia20 import (
    ADMIN_ID,
    FakeAnswerMessage,
    FakeCallback,
    FakeDocument,
    FakePhoto,
    FakeState,
    _assert_is_group_screen,
    _roles_ready,
)


def _abandoned_consent_prompt(state) -> None:
    """Шаг 1-2 сценария: открыт промпт PDF согласия, менеджер ушёл на «📎 Бонус»."""
    asyncio.run(st.consent_pdf_set(FakeCallback("consent_pdf_set:offer"), state))
    assert asyncio.run(state.get_data()).get("raw_file_key") == "consent_pdf_offer"
    asyncio.run(st.settings_file_start(FakeCallback("settings_file:reg_bonus"), state))


def test_abandoned_consent_prompt_does_not_hijack_the_next_file(tmp_path):
    """Файл бонуса ложится в бонус, а согласие остаётся нетронутым."""
    _roles_ready(tmp_path)
    state = FakeState()
    _abandoned_consent_prompt(state)

    # Вход в «📎 Бонус» ЗАБРАЛ данные себе: чужого ключа в состоянии нет вовсе.
    assert asyncio.run(state.get_data()) == {"file_setting": "reg_bonus"}

    msg = FakeAnswerMessage(document=FakeDocument("bonus-doc-id", "application/pdf"))
    asyncio.run(st.settings_receive_file_doc(msg, state))

    assert asyncio.run(db.get_setting("reg_bonus_doc_file_id")) == "bonus-doc-id"
    assert asyncio.run(db.get_setting("consent_pdf_offer")) is None
    assert "PDF согласия" not in " ".join(t for t, _ in msg.sent if t)
    _assert_is_group_screen(msg.screen[1], "event")


def test_abandoned_consent_prompt_does_not_reject_the_next_photo(tmp_path):
    """Вторая половина того же бага: с протухшим `raw_file_key` фото бонуса отвергалось
    бессмысленным «Согласие принимается только PDF-документом, не фото.»"""
    _roles_ready(tmp_path)
    state = FakeState()
    _abandoned_consent_prompt(state)

    msg = FakeAnswerMessage(photo=[FakePhoto("bonus-photo-id")])
    asyncio.run(st.settings_receive_file_photo(msg, state))

    assert asyncio.run(db.get_setting("reg_bonus_photo_file_id")) == "bonus-photo-id"
    assert "Согласие принимается" not in " ".join(t for t, _ in msg.sent if t)


def test_abandoned_text_edit_does_not_steer_the_photo_cancel(tmp_path):
    """Навигационная половина: брошенная ТЕКСТОВАЯ правка уводила отмену загрузки фото на
    экран группы того текста. «После одобрения» живёт в «📋 Заявки», фото — в «🎪 Событие»,
    поэтому промах видно по экрану возврата."""
    _roles_ready(tmp_path)
    state = FakeState()
    asyncio.run(st.settings_edit_start(FakeCallback("settings_edit:approve_text"), state))
    assert asyncio.run(state.get_data()) == {"setting_key": "approve_text"}

    asyncio.run(st.settings_photo_start(FakeCallback("settings_photo:start"), state))
    assert asyncio.run(state.get_data()) == {"photo_setting": "start"}
    assert asyncio.run(state.get_state()) == EditSetting.waiting_for_photo.state

    cb = FakeCallback("settings_cancel")
    asyncio.run(st.cancel_edit_setting_callback(cb, state))
    _assert_is_group_screen(cb.message.markup, "event")


def test_return_hint_follows_the_state_not_the_first_key_found(tmp_path):
    """Пояс поверх подтяжек: даже если данные всё-таки окажутся смешанными (старый вход,
    ручная правка хранилища), развилка идёт по СОСТОЯНИЮ — оно однозначно называет поток."""
    mixed = {"setting_key": "approve_text", "photo_setting": "start"}
    assert st._return_hint_from_state(mixed, EditSetting.waiting_for_photo.state) == {
        "setting_key": "start_photo_file_id"
    }
    mixed_file = {"setting_key": "approve_text", "file_setting": "reg_bonus"}
    assert st._return_hint_from_state(mixed_file, EditSetting.waiting_for_file.state) == {
        "setting_key": "reg_bonus_doc_file_id"
    }
    # Состояние не названо — запасной порядок по данным, как было до правки.
    assert st._return_hint_from_state(mixed) == {"setting_key": "approve_text"}
    assert st._return_hint_from_state({}) == {}
