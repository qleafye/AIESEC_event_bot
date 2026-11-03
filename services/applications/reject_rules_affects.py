"""Предикат «правка настройки может сломать правило автоотказа» — без бот-фреймворка.

Веб-процесс Mini App не может импортировать `services.applications.reject_rules_notify`: тот на
уровне модуля тянет `services.scheduler`, а планировщик — процесс бота. Роутер настроек
спрашивает только этот модуль и, если ключ анкеты или списка вариантов задет, ставит в
очередь событие `reject_rules_recheck`. Само уведомление о паузе по-прежнему шлёт бот.
"""
from __future__ import annotations

from domain.cities import split_per_city_key
from domain.regform.engine import MULTI_CONFIG, SELECT_CONFIG

# Тот же закрытый набор трековых хвостов, что `services.i18n.i18n_sources._TRACK_SUFFIXES` — второй
# копией логики его снятия не считается (это два литерала, не алгоритм), но ПРИЁМ снятия хвоста
# (`cities.split_per_city_key` для городского, сравнение суффикса для трекового) — тот же самый:
# закрытое множество кодов городов, а не произвольная регулярка.
_TRACK_SUFFIXES = ("__party", "__short")

# Ключи списков вариантов вне SELECT_CONFIG/MULTI_CONFIG (reg_engine.options() резолвит их особой
# веткой) — закрытый список, тот же, что перечисляет план.
_EXTRA_OPTION_KEYS = frozenset({"education_status_options", "university_options", "source_options"})


def _option_list_keys() -> frozenset[str]:
    keys = {opt_key for opt_key, _default in SELECT_CONFIG.values()}
    keys.update(opt_key for opt_key, _default in MULTI_CONFIG.values())
    keys.update(_EXTRA_OPTION_KEYS)
    return frozenset(keys)


_OPTION_LIST_KEYS = _option_list_keys()  # SELECT_CONFIG/MULTI_CONFIG — статические литералы модуля


def _strip_reg_key_suffixes(key: str) -> str:
    """Снимает городской, потом трековый хвост — `reg_q_course__party__city__msk` ->
    `reg_q_course`. Тот же приём, что `services.i18n.i18n_sources._strip_dynamic_suffixes`."""
    split = split_per_city_key(key)
    base = split[0] if split else key
    for suffix in _TRACK_SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def affects_reject_rules(key: str) -> bool:
    """Чистый предикат: истинно, если запись этого ключа настроек могла сломать (или починить)
    условие правила автоотказа — тумблер вопроса анкеты (`reg_q_*`, с любым городским/трековым
    хвостом) либо список вариантов ответа, известный `SELECT_CONFIG`/`MULTI_CONFIG` или
    `_EXTRA_OPTION_KEYS`. Ложно СРАЗУ для всего остального — подавляющее большинство записей
    настроек (тексты, лимиты, капы, тумблеры не про анкету) к правилам отношения не имеют, и
    хук на воронке записи не имеет права стоить им лишнего запроса в базу."""
    base = _strip_reg_key_suffixes(key)
    if base.startswith("reg_q_"):
        return True
    return base in _OPTION_LIST_KEYS
