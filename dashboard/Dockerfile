# Образ дашборда статистики (Phase 15, D-04). Контекст сборки — КОРЕНЬ репозитория
# (docker-compose: build.context=., dockerfile=dashboard/Dockerfile), чтобы в образ попал
# пакет dashboard/ целиком: код, шаблоны, static (tokens.css/app.css, brand/*.svg, vendor/).
#
# Это НЕ образ бота: сюда ставится только dashboard/requirements.txt (fastapi/uvicorn/jinja2),
# без aiogram, gspread и apscheduler. Общего с ботом — только примонтированный ./data
# с forum.db (том без `:ro` — WAL требует -shm; только-чтение даёт `mode=ro` в dashboard/db.py)
# и тот же .env стека (D-05).
FROM python:3.11-slim

WORKDIR /app

COPY dashboard/requirements.txt /app/dashboard/requirements.txt
RUN pip install --no-cache-dir -r /app/dashboard/requirements.txt

# Тот же UID/GID 1000, что и у бота: ./data принадлежит этому пользователю на хосте
# (chown -R 1000:1000 data), и дашборду достаточно права на чтение.
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser dashboard/ /app/dashboard/
# Общие с ботом модули из пакета core/ — поштучно, не каталогом: остальные модули core/
# тянут database.db/aiosqlite, которых в образе нет. `core/__init__.py` пустой нарочно —
# без него `from core import web_theme` не найдёт пакет, а с импортами внутри потянул бы лишнее.
COPY --chown=appuser:appuser core/__init__.py /app/core/__init__.py
# Модуль темы веб-слоя (19.1): чистый Python без зависимостей, общий с ботом и Mini App.
COPY --chown=appuser:appuser core/web_theme.py /app/core/web_theme.py
# Резолвер типов медиа (08.09): чистая stdlib, общий с ботом — без него образ падает
# на старте `ModuleNotFoundError: tg_media` (прод 10.09). Сторож: tests/test_dashboard_docker.py.
COPY --chown=appuser:appuser core/tg_media.py /app/core/tg_media.py
# Статистика прихода (бэклог чек-ина п.10): общие с ботом запросы блока «Приход», чистая stdlib.
COPY --chown=appuser:appuser core/arrival_stats.py /app/core/arrival_stats.py
# Санитайзер секретов (25.09): чистая stdlib, маскирует токен бота в логах.
COPY --chown=appuser:appuser core/secret_redact.py /app/core/secret_redact.py
# Формула рейтинга чата (квик 260927): чистая stdlib, общая с ботом и тулом по экспорту.
COPY --chown=appuser:appuser core/chat_score.py /app/core/chat_score.py
# Фаза 26-01 (RT-01): в образ едут ТОЛЬКО растры орнамента, ни одного `.py` Mini App —
# раскладка каталогов в образе совпадает с репозиторием, поэтому `dashboard.main.PATTERN_DIR`
# (`BASE_DIR.parent / "miniapp" / "static" / "pattern"`) резолвится одинаково и локально, и
# в контейнере.
COPY --chown=appuser:appuser miniapp/static/pattern/ /app/miniapp/static/pattern/
RUN mkdir -p /app/data && chown appuser:appuser /app/data

USER appuser

# В slim нет curl — проверяем /health стандартной библиотекой.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# Без --proxy-headers: X-Forwarded-* разбирает ProxyHeadersMiddleware внутри create_app()
# по подсети DASHBOARD_TRUSTED_PROXIES (exec-форма CMD переменные окружения не разворачивает).
CMD ["uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8000"]
