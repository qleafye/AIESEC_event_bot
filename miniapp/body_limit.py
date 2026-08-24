"""260824-8qw (HG-01, DoS): предел тела запроса на уровне ASGI — до разбора формы.

`POST /app/api/uploads` спуливал тело на диск любого размера: потолок 20 МБ проверялся
только внутри хендлера ПОСЛЕ `await request.form()`, а `max_part_size` starlette 0.45.3 к
файловым частям не применяется вовсе (`starlette/formparsers.py`, ветка
`if self._current_part.file is None` — потолок считается только для НЕфайловых частей).
Единственная защита раньше — проверка `Content-Length` в хендлере, а `Content-Length` —
заголовок, который клиент присылает добровольно: `Transfer-Encoding: chunked` (легальный
HTTP/1.1, его используют и браузеры, и мобильные клиенты без злого умысла) его не несёт
вовсе — Starlette всё равно собрала бы тело целиком, прежде чем хендлер успел бы что-то
проверить. Требовать `Content-Length` тоже нельзя — это отсекло бы легальных клиентов
заодно с атакой.

Приём: `BodyLimitMiddleware` — чистый ASGI-middleware (НЕ `starlette.middleware.base.
BaseHTTPMiddleware` — тот сначала полностью собирает `Request` поверх исходного `receive`
и только потом отдаёт управление обработчику, то есть тело уже принято целиком к моменту,
когда мы могли бы его оборвать). Тело читается САМИМ middleware — до вызова внутреннего
приложения — сообщениями `http.request`, с подсчётом байт; как только сумма превышает
потолок, ответ 413 уходит немедленно, а внутреннее приложение (роутинг, зависимости,
`request.form()`) не вызывается вовсе — на диск не спулится ни байта, в память попадёт
максимум потолок + один чанк. Если тело уместилось в потолок, накопленные ASGI-сообщения
проигрываются внутреннему приложению как обычный `receive` — оно не замечает разницы.
"""
from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyTooLarge(Exception):
    """Внутренний сигнал: тело запроса превысило потолок.

    `limit` — число, которое уходит в JSON-ответе. Может отличаться от порога, на котором
    сработал обрыв (`report_limits` округляет вниз до потолка ФАЙЛА — то, что видит
    человек, — а не до потолка тела с multipart-обвязкой поверх файла)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"body exceeds {limit} bytes")


class BodyLimitMiddleware:
    """`__init__(app, *, limits, default, report_limits=None)`.

    `limits` — таблица «точный path -> потолок тела в байтах» (порог, на котором рвётся
    приём); `default` — потолок для всех путей вне таблицы. `report_limits` — необязательная
    таблица «path -> число для JSON-ответа», если оно должно отличаться от порога обрыва
    (ровно этот случай — `/app/api/uploads`: порог обрыва больше на multipart-обвязку,
    а в ответе остаётся потолок файла, который уже показывает `GET /uploads/limits`)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limits: dict[str, int],
        default: int,
        report_limits: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.limits = limits
        self.default = default
        self.report_limits = report_limits or {}

    def _ceiling(self, path: str) -> int:
        return self.limits.get(path, self.default)

    def _reported(self, path: str) -> int:
        return self.report_limits.get(path, self._ceiling(path))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        ceiling = self._ceiling(path)
        reported = self._reported(path)

        # Предварительная отсечка по `Content-Length` (когда клиент его прислал) — 413 без
        # единого прочитанного байта тела. Не единственная защита (см. докстринг модуля).
        headers = dict(scope.get("headers") or ())
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared = int(raw_length)
            except ValueError:
                declared = None
            if declared is not None and declared > ceiling:
                await self._send_413(scope, send, reported)
                return

        buffered: list[Message] = []
        received = 0
        exhausted = False  # видели more_body=False -- исходный поток дочитан целиком

        async def counted_receive() -> Message:
            nonlocal received, exhausted
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > ceiling:
                    raise BodyTooLarge(reported)
                if not message.get("more_body", False):
                    exhausted = True
            return message

        try:
            while not exhausted:
                message = await counted_receive()
                buffered.append(message)
                if message["type"] != "http.request":
                    break
        except BodyTooLarge:
            # Обрыв ДО вызова внутреннего приложения: роутинг, зависимости и
            # `request.form()` не запускаются вовсе -- ни байта на диск.
            await self._send_413(scope, send, reported)
            return

        queue = buffered

        async def replay_receive() -> Message:
            if queue:
                return queue.pop(0)
            # Оборона: сервер прислал больше, чем обещал предыдущим `more_body=False`.
            return await counted_receive()

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, replay_receive, tracking_send)
        except BodyTooLarge:
            if response_started:
                # Ответ уже начат -- второй `http.response.start` ASGI не разрешает.
                raise
            await self._send_413(scope, send, reported)

    @staticmethod
    async def _send_413(scope: Scope, send: Send, limit: int) -> None:
        response = JSONResponse({"reason": "too_large", "limit": limit}, status_code=413)
        # `Response.__call__` не читает `receive` -- второй параметр ему не нужен.
        await response(scope, _unused_receive, send)


async def _unused_receive() -> Message:  # pragma: no cover - Response.__call__ его не зовёт
    return {"type": "http.disconnect"}


__all__ = ["BodyLimitMiddleware", "BodyTooLarge"]
