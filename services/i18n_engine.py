"""Phase 27 (27-03, LANG-04/LANG-10) — драйверы машинного перевода за одним контрактом.

Решение владельца (чекпоинт 27-01, `27-CONTEXT.md` «Решения владельца на чекпоинте 27-01»):
**embedded с выгрузкой** — `argos-translate-lt` в процессе бота, модель грузится ТОЛЬКО когда
очередь перевода непуста и выгружается (`EmbeddedArgosDriver.unload`) сразу после того, как
`services/i18n_worker.py::drain` её опустошил (в покое — ~0 МБ сверх базового интерпретатора).
Сайдкар LibreTranslate НЕ поднимается по умолчанию (хост держит 237 МБ физически свободных на
~26 контейнеров — постоянные 400-700 МБ сайдкара туда не влезают), но код HTTP-драйвера
остаётся рабочим фоллбэком за тем же контрактом — переключение `delegate_lang_driver`
("embedded" -> "http") в реестре, а не правка кода (`settings_schema.py`).

Оба драйвера реализуют `TranslationDriver.translate_batch` — СИНХРОННЫЙ метод (зовётся из
`asyncio.to_thread`, CLAUDE.md/27-RESEARCH.md Pitfall 7: ct2-инференс — CPU-bound C++, прямой
вызов в event loop подвесил бы long polling на десятки секунд). Контракт: длина ответа ВСЕГДА
равна длине запроса (в т.ч. на пустом входе — `[] -> []`); отдельная строка, которую не удалось
перевести, возвращает `""` — решение «оставить в очереди (сбой драйвера) или записать пустую
строку (fail-soft разметки)» принимает воркер, не драйвер.

`argostranslate` импортируется ТОЛЬКО лениво, внутри `EmbeddedArgosDriver._boot()` (идиома уже
есть в проекте — `services/scheduler.py` лениво импортирует `services.game_digest` — и
обязательна здесь: полный прогон тестов без 156 МБ модели в `./data/argos/` упал бы на
импорте, Pitfall 9 замера 27-01). `grep -c "^import argostranslate\|^from argostranslate"
services/i18n_engine.py` обязан быть `0` — это acceptance criterion плана, не стиль.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from settings_schema import get_setting_typed

logger = logging.getLogger(__name__)

_DEFAULT_HTTP_URL = "http://libretranslate:5000"


class TranslationDriver:
    """Контракт обоих драйверов. `unload()` — no-op по умолчанию (HTTP-драйвер не держит
    ничего тяжёлого в памяти процесса бота); `EmbeddedArgosDriver` его переопределяет."""

    def translate_batch(self, texts: list[str]) -> list[str]:
        raise NotImplementedError

    def unload(self) -> None:
        return None


class EmbeddedArgosDriver(TranslationDriver):
    """Ленивая загрузка модели `translate-ru_en-1_9` из `ARGOS_PACKAGES_DIR` (том `./data`,
    сеть в рантайме не нужна — модель доставляется заранее, см. `27-01-SUMMARY.md`, ручная
    доставка `.argosmodel` через `scp`). Модели нет -> ОДНА строка в лог за всё время жизни
    процесса (`_unavailable`, не переретраится на каждый батч — Pitfall «без спама в лог»),
    дальше `translate_batch` тихо отдаёт пустые строки той же длины."""

    def __init__(self) -> None:
        self._ready = False
        self._unavailable = False
        self._translate = None

    def _boot(self) -> None:
        if self._unavailable or self._ready:
            return
        try:
            packages_dir = os.environ.setdefault(
                "ARGOS_PACKAGES_DIR", os.path.join("data", "argos"),
            )
            import argostranslate.package
            import argostranslate.translate

            self._translate = argostranslate.translate

            if not self._has_ru_en_package(argostranslate.package):
                # Наличие `.argosmodel` в ARGOS_PACKAGES_DIR — необходимое, но НЕ достаточное
                # условие: argos-translate-lt читает установленные пакеты (распакованные в
                # тот же каталог), а не файлы-архивы лежащие рядом. Без install_from_path()
                # get_installed_packages() пуст и translate() падает на NoneType, даже если
                # .argosmodel физически на месте (найдено на стенде, ручная доставка scp
                # кладёт именно архив).
                self._install_from_dir(argostranslate.package, packages_dir)

            if not self._has_ru_en_package(argostranslate.package):
                logger.error(
                    "EmbeddedArgosDriver: пакет ru->en не установлен и не найден "
                    "в %s — положите файл translate-ru_en-1_9.argosmodel в этот каталог "
                    "(ручная доставка scp, см. 27-01-SUMMARY.md); драйвер отключается на "
                    "оставшееся время жизни процесса, делегат видит русский", packages_dir,
                )
                self._unavailable = True
                return

            self._ready = True
        except Exception as exc:  # noqa: BLE001 — намеренно широкий (D-04, fail-soft)
            logger.error(
                "EmbeddedArgosDriver: модель недоступна (%s) — драйвер отключается на "
                "оставшееся время жизни процесса, делегат видит русский", exc,
            )
            self._unavailable = True

    @staticmethod
    def _has_ru_en_package(package_module) -> bool:
        installed = package_module.get_installed_packages()
        return any(
            getattr(pkg, "from_code", None) == "ru" and getattr(pkg, "to_code", None) == "en"
            for pkg in installed
        )

    @staticmethod
    def _install_from_dir(package_module, packages_dir: str) -> None:
        if not os.path.isdir(packages_dir):
            return
        for name in sorted(os.listdir(packages_dir)):
            if not name.endswith(".argosmodel"):
                continue
            path = os.path.join(packages_dir, name)
            package_module.install_from_path(path)
            logger.info("EmbeddedArgosDriver: установлен пакет из %s", path)

    @staticmethod
    def _is_no_translation_installed_error(exc: Exception) -> bool:
        """Фактический сбой на стенде: пакет НЕ установлен (только `.argosmodel`-архив лежит
        рядом, ничего не распаковано) -> `argostranslate.translate.get_translation_from_codes`
        отдаёт `None`, и вызов `.translate()` падает `AttributeError` на `NoneType`. Эта
        ошибка ловится на КАЖДОЙ строке батча одинаково — логировать её построчно значит
        заспамить лог N одинаковыми строками на пустом месте; отличается от единичного сбоя
        перевода конкретной строки (тот остаётся per-line, batch продолжается)."""
        return isinstance(exc, AttributeError) and "get_translation" in str(exc)

    def translate_batch(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        if not self._ready and not self._unavailable:
            self._boot()
        if self._unavailable:
            return ["" for _ in texts]
        out: list[str] = []
        for text in texts:
            try:
                out.append(self._translate.translate(text, "ru", "en"))
            except Exception as exc:  # noqa: BLE001 — намеренно широкий (fail-soft)
                if self._is_no_translation_installed_error(exc):
                    logger.error(
                        "EmbeddedArgosDriver: модель не установлена (%s) — драйвер "
                        "отключается на оставшееся время жизни процесса, делегат видит "
                        "русский", exc,
                    )
                    self._unavailable = True
                    out.extend("" for _ in range(len(texts) - len(out)))
                    return out
                # Сбой конкретной строки (не «драйвер сломан целиком») не должен ронять
                # остальной батч — одна строка теряется, соседние переводятся как обычно.
                logger.error("EmbeddedArgosDriver: перевод строки не удался (%s)", exc)
                out.append("")
        return out

    def unload(self) -> None:
        """Явная выгрузка (owner-decision 27-01: «модель грузится ТОЛЬКО когда очередь
        непуста и выгружается после опустошения»). `del` ссылки на модуль перевода +
        `gc.collect()` — у `argostranslate`/ct2 нет метода `.close()`/`.unload()`, снять RSS
        можно только избавившись от последней ссылки на загруженные веса и попросив сборщик
        мусора пройти сразу (замер 27-01: холодная загрузка 2.4 с — цена повторной загрузки
        на следующий непустой тик приемлема ради ~237 МБ, которые иначе висели бы всегда)."""
        if not self._ready:
            return
        self._translate = None
        self._ready = False
        import gc

        gc.collect()
        logger.info("EmbeddedArgosDriver: модель выгружена (очередь перевода пуста)")


class HttpLibreTranslateDriver(TranslationDriver):
    """HTTP-фоллбэк — сайдкар LibreTranslate (docker-compose.yml, профиль `i18n`, не
    поднимается по умолчанию). `POST {base_url}/translate` с массивом строк за один запрос
    (LibreTranslate принимает и отдаёт массив — `27-RESEARCH.md`, «Code Examples»). Не более
    одного запроса в секунду (`_throttle`), бэкофф на HTTP 429 с уважением `Retry-After`,
    явный таймаут. Любая сетевая ошибка -> список пустых строк ТОЙ ЖЕ длины (не бросает
    исключение наружу — решение «оставить в очереди» принимает воркер по итогам самого
    вызова `translate_batch`, здесь же — контракт «длина ответа = длина запроса» даже на
    отказе)."""

    _MIN_INTERVAL_SECONDS = 1.0
    _MAX_RETRIES = 3

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._last_call_monotonic = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_monotonic
        wait = self._MIN_INTERVAL_SECONDS - elapsed
        if wait > 0:
            time.sleep(wait)

    def translate_batch(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        url = f"{self._base_url}/translate"
        payload = {"q": list(texts), "source": "ru", "target": "en", "format": "text"}
        backoff = 1.0
        for _ in range(self._MAX_RETRIES):
            self._throttle()
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(url, json=payload)
            except httpx.HTTPError as exc:
                logger.error("HttpLibreTranslateDriver: сбой запроса (%s)", exc)
                return ["" for _ in texts]
            finally:
                self._last_call_monotonic = time.monotonic()

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else backoff)
                backoff *= 2
                continue

            try:
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.error("HttpLibreTranslateDriver: ответ не распознан (%s)", exc)
                return ["" for _ in texts]

            out = data.get("translatedText") if isinstance(data, dict) else data
            if isinstance(out, str):
                out = [out]
            if not isinstance(out, list) or len(out) != len(texts):
                logger.error("HttpLibreTranslateDriver: неожиданный формат ответа сервера")
                return ["" for _ in texts]
            return [str(item) if item is not None else "" for item in out]

        logger.error("HttpLibreTranslateDriver: превышен лимит ретраев (HTTP 429)")
        return ["" for _ in texts]


_driver_instance: TranslationDriver | None = None
_driver_signature: tuple | None = None


async def get_driver() -> TranslationDriver:
    """Драйвер по реестровой настройке `delegate_lang_driver` ("embedded"/"http", дефолт
    "embedded" — решение владельца 27-01). Кешируется в модуле по сигнатуре конфигурации:
    модель Argos грузится максимум один раз за время жизни процесса (до первой явной
    `unload()`), смена настройки на `http` без рестарта переключает на новый экземпляр
    HTTP-драйвера немедленно."""
    global _driver_instance, _driver_signature

    driver_name = await get_setting_typed("delegate_lang_driver") or "embedded"
    if driver_name == "http":
        base_url = await get_setting_typed("delegate_lang_http_url") or _DEFAULT_HTTP_URL
        signature = ("http", base_url)
    else:
        driver_name = "embedded"
        signature = ("embedded",)

    if _driver_instance is not None and _driver_signature == signature:
        return _driver_instance

    if driver_name == "http":
        _driver_instance = HttpLibreTranslateDriver(base_url)
    else:
        _driver_instance = EmbeddedArgosDriver()
    _driver_signature = signature
    return _driver_instance
