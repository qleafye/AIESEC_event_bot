"""День форума на сайте открывает экран Mini App.

Список резерва здесь не считается: у образа дашборда нет сервиса бота.
Страница только ведёт на тот же экран, который зовёт отбор шапки.
"""

RESERVE_HREF = "/app/#/reserve"
NUDGE_HREF = "/app/#/nudge"
LETTER_HREF = "/app/#/letter"
TRAINING_MOVE_HREF = "/app/#/training-move"
LIFT_REJECT_HREF = "/app/#/lift-reject"


def reserve_page() -> str:
    return (
        "<!doctype html><meta charset=\"utf-8\">"
        "<title>Позвать резерв</title>"
        "<p>Список тот же, что в приложении. Отдельного отбора на сайте нет.</p>"
        f"<p><a href=\"{RESERVE_HREF}\">Позвать резерв</a></p>"
    )


def nudge_page() -> str:
    return (
        "<!doctype html><meta charset=\"utf-8\">"
        "<title>Догонялка</title>"
        "<p>Список тот же, что в приложении. Отдельного отбора на сайте нет.</p>"
        f"<p><a href=\"{NUDGE_HREF}\">Догонялка</a></p>"
    )


def letter_page() -> str:
    return (
        "<!doctype html><meta charset=\"utf-8\">"
        "<title>Написать</title>"
        "<p>Письмо уходит из приложения. Отдельной отправки на сайте нет.</p>"
        f"<p><a href=\"{LETTER_HREF}\">Написать</a></p>"
    )


def lift_reject_page() -> str:
    return (
        "<!doctype html><meta charset=\"utf-8\">"
        "<title>Снять отказ</title>"
        "<p>Одобрение одной заявки идёт в приложении. Отдельной записи на сайте нет.</p>"
        f"<p><a href=\"{LIFT_REJECT_HREF}\">Снять отказ</a></p>"
    )


def training_move_page() -> str:
    return (
        "<!doctype html><meta charset=\"utf-8\">"
        "<title>Перенос сканов</title>"
        "<p>Перенос идёт в приложении. Отдельной записи на сайте нет.</p>"
        f"<p><a href=\"{TRAINING_MOVE_HREF}\">Перенос сканов</a></p>"
    )
