"""Квик 260912-mcj: московское «сейчас» дашборда — третий осознанный литерал пояса Europe/Moscow
в проекте (после `services/timeutil.py` и `miniapp/timeutil.py`).

Образ дашборда собирается СВОИМ `dashboard/Dockerfile` (контекст сборки — корень репозитория,
но в образ явно копируется только `dashboard/` целиком + корневые `web_theme.py`/`tg_media.py`
+ растры `miniapp/static/pattern/`, см. докстринг Dockerfile). `from services.timeutil import
msk_now` уронил бы контейнер на старте `ModuleNotFoundError` — так прод падал дважды на похожей
ошибке (`web_theme` 31.08, `tg_media` 10.09). Сторожа «один литерал часового пояса»
(`tests/test_timezone_fix_260816.py::test_moscow_literal_declared_exactly_once` и
`test_moscow_literal_under_miniapp_declared_exactly_once`) смотрят только на
`services/*.py`+`handlers/*.py` и `miniapp/*.py`+`miniapp/routers/*.py` — `dashboard/` они не
покрывают, поэтому третья копия здесь не ломает эти сторожа (свой сторож — в
`tests/test_msk_timestamps_260912.py`, который держит `dashboard/*.py` без импорта
`services`/`database`, тот же класс защиты от падения образа).

Голая stdlib, ни одного импорта проекта — совпадает по духу с `services/timeutil.py`/
`miniapp/timeutil.py`.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def msk_now() -> datetime:
    """Naive московское «сейчас» дашборда — сутки KPI/графика и возраст сдачи задания

    (`game_submissions.submitted_at`, которую квик 260912-mcj перевёл на московский
    `services.timeutil.msk_now()`) считаются по этим же часам, а не по UTC контейнера.
    """
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)
