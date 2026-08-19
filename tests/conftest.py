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

Это НЕ фикстура с общим стейтом: БД каждый тест по-прежнему заводит сам
(`config.DB_PATH = tmp_path / ...`), conftest ничего не сбрасывает между тестами.
"""
from handlers import registration, user_actions, admin, payment  # noqa: F401  -- порядок как в main.py
