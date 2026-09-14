# Пустой файл обязателен: колесо argos-translate-lt (requirements.txt) ставит в site-packages
# свой пакет верхнего уровня `tests`, который без этого файла затеняет наш `tests/` (namespace
# package) — `from tests.test_roles_phase8 import ...` резолвится в чужой пакет и падает.
