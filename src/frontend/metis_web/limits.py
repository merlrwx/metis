"""Bound request bodies before any form parsing (CSRF checks parse forms)."""

import re

from starlette.responses import PlainTextResponse

FORM_LIMIT = 256 * 1024
FILE_LIMIT = 21 * 1024 * 1024
BATCH_LIMIT = 10 * FILE_LIMIT
UPLOAD_PATHS = (
    (re.compile(r"^/o/[^/]+/documents$"), BATCH_LIMIT),
    (re.compile(r"^/o/[^/]+/documents/[^/]+/replace$"), FILE_LIMIT),
)


class BodyTooLarge(Exception):
    pass


def limit_for(path: str) -> int:
    for pattern, limit in UPLOAD_PATHS:
        if pattern.match(path):
            return limit
    return FORM_LIMIT


class RequestSizeLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = limit_for(scope["path"])
        too_large = PlainTextResponse("Request body is too large.", status_code=413)
        declared = dict(scope.get("headers", [])).get(b"content-length")
        try:
            if declared is not None and int(declared) > limit:
                return await too_large(scope, receive, send)
        except ValueError:
            return await PlainTextResponse("Bad request.", status_code=400)(
                scope, receive, send
            )
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise BodyTooLarge()
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except BodyTooLarge:
            await too_large(scope, receive, send)
