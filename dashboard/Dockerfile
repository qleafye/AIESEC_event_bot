# Образ дашборда статистики (Phase 15, D-04). Контекст сборки — КОРЕНЬ репозитория
# (docker-compose: build.context=., dockerfile=dashboard/Dockerfile), чтобы в образ попал
# пакет dashboard/ целиком: код, шаблоны, static (tokens.css/app.css, brand/*.svg, vendor/).
#
# Это НЕ образ бота: сюда ставится только dashboard/requirements.txt (fastapi/uvicorn/jinja2),
# без aiogram, gspread и apscheduler. Общего с ботом — только примонтированный ./data:ro
# с forum.db и тот же .env стека (D-05).
FROM python:3.11-slim

WORKDIR /app

COPY dashboard/requirements.txt /app/dashboard/requirements.txt
RUN pip install --no-cache-dir -r /app/dashboard/requirements.txt

# Тот же UID/GID 1000, что и у бота: ./data принадлежит этому пользователю на хосте
# (chown -R 1000:1000 data), и дашборду достаточно права на чтение.
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser dashboard/ /app/dashboard/
# Корневой модуль темы веб-слоя (19.1): чистый Python без зависимостей, общий с ботом и Mini App.
COPY --chown=appuser:appuser web_theme.py /app/web_theme.py
RUN mkdir -p /app/data && chown appuser:appuser /app/data

USER appuser

# В slim нет curl — проверяем /health стандартной библиотекой.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# Без --proxy-headers: X-Forwarded-* разбирает ProxyHeadersMiddleware внутри create_app()
# по подсети DASHBOARD_TRUSTED_PROXIES (exec-форма CMD переменные окружения не разворачивает).
CMD ["uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8000"]
