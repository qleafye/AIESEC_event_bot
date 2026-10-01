"""Общий conftest: канонический порядок импорта хендлеров для ЛЮБОГО подмножества тестов.

Зачем. Роутеры живут в `handlers.admin` / `handlers.registration` / `handlers.user_actions` /
`handlers.payment`; швы (`handlers/admin_*.py`, `handlers/reg_flow.py`, `handlers/reg_steps.py`)
регистрируют свои хендлеры в ТОТ ЖЕ роутер и импортируются из тела владельца роутера
(seam-импорты в `admin.py:~690+`, `registration.py:~2258`). Поэтому порядок регистрации
хендлеров — а значит first-match и golden-снапшот `test_refac_snapshot_260816.py`,
completeness-инварианты `test_roles_phase8.py` — зависит от того, какой модуль пакета
`handlers` попал в `sys.modules` ПЕРВЫМ в процессе:

- `handlers.admin` / `handlers.registration` первым (так делает `main.py` в проде и первый по
  алфавиту файл тестов при полном прогоне) — канонический порядок;
- шов первым (`admin_cities`, `admin_reg_config`, `reg_flow`) — его хендлеры регистрируются
  ПОСЛЕ хендлеров владельца (сдвиг в хвост роутера), снапшот/roles-тесты падают;
- шов, у которого владелец импортирует конкретные имена (`admin_settings`, `admin_broadcasts`,
  `admin_moderation`, `admin_roles`) — `ImportError: cannot import name ... from partially
  initialized module` прямо на сборке (файлы `test_sheet_tabs_settings_260815.py`,
  `test_broadcast_429_phase3.py`, `test_staff_forward_origin_260814.py` до этого conftest
  не запускались поодиночке и ломали любой `--lf`-перегон, куда попадали первыми).

pytest импортирует conftest.py ДО сборки любого тестового модуля, поэтому импорт здесь
гарантирует канонический порядок при любом наборе/порядке файлов в аргументах — полный
прогон, `--lf`, один файл, произвольная перестановка. Порядок ниже = `main.py`.

С quick 260819 тот же канонический импорт живёт в проде — `handlers/__init__.py` (любой
`import handlers.x` инициализирует владельцев первыми; закреплено subprocess-тестом
`test_seam_import_order_260819.py`). Строка ниже оставлена как явная документация порядка и
страховка на случай, если `__init__` когда-нибудь снова опустеет.

Это НЕ фикстура с общим стейтом: БД каждый тест по-прежнему заводит сам
(`config.DB_PATH = tmp_path / ...`), conftest ничего не сбрасывает между тестами.
"""
from handlers import registration, user_actions, admin, payment  # noqa: F401  -- порядок как в main.py

# Квик форум-ночь (A2): часть тестов (например, tests/test_content_percity_consumers.py::
# test_approve_text_party_track_no_party_override_still_ignores_city) намеренно НЕ выставляет
# config.DB_PATH сама -- рассчитывает на то, что предыдущий тест В ТОМ ЖЕ ФАЙЛЕ уже открыл
# свою tmp_path-БД, и эта БД переживает до конца сессии. Прогон файла целиком поэтому
# детерминированно зелёный, а прогон ОДНОГО такого теста -- нет: config.DB_PATH остаётся
# дефолтом из config.py ("data/forum.db"), и есть ли там рабочая (пусть пустая) таблица
# bot_settings, зависит от случайного состояния файла на диске конкретной машины/ворктри --
# в свежем `git worktree add` схемы там ещё нет, отсюда "no such table: bot_settings" именно
# и только в свежем ворктри (не дефект get_setting: она и так fail-soft на ОТСУТСТВИЕ строки,
# просто не переживает ОТСУТСТВИЕ таблицы). Чтобы прогон отдельного теста не зависел от
# постороннего файла на диске, готовим здесь -- один раз на процесс (воркер pytest-xdist) --
# валидную пустую схему и подставляем её в config.DB_PATH ДО сборки тестов; тесты, которые
# сами вызывают fast_init_db()/init_db() со своим tmp_path, тут же перезапишут config.DB_PATH
# и разницы не заметят.
import os as _os
import tempfile as _tempfile

from config import config as _config
from tests._dbtpl import fast_init_db as _fast_init_db

_config.DB_PATH = _os.path.join(
    _tempfile.mkdtemp(prefix="gsd_conftest_default_db_"), "default.db"
)
_fast_init_db()


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _reset_checkin_evening_done():
    """`services.checkin_broadcast._evening_done` — память процесса «вечерняя рассылка QR на эту
    дату уже отработала». Между тестами одного воркера она не должна переживать: тест, где
    вечерняя джоба сработала, иначе отключал бы догон в следующем тесте с той же датой."""
    from services import checkin_broadcast as _cb
    _cb._evening_done.clear()
    yield


@_pytest.fixture(autouse=True)
def _reset_sos_hint_throttle():
    """`handlers.group_chat._sos_hint_sent` — память процесса «подсказку по этой заявке в этом
    чате уже давали». Номера заявок и чатов в тестах повторяются, поэтому между тестами одного
    воркера она не должна переживать. Модуль не импортируем сами — только чистим, если тест
    его уже загрузил (импорт хендлеров из фикстуры менял бы порядок импорта в чужих тестах)."""
    import sys as _sys
    _gc = _sys.modules.get("handlers.group_chat")
    if _gc is not None:
        _gc._sos_hint_sent.clear()
    yield
