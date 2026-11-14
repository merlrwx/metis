"""Defensive response headers for the JSON API."""

API_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
)


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        sensitive = scope["path"].startswith(("/api/auth/", "/api/invitations/"))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if not sensitive or name.lower() != b"cache-control"
                ]
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value) for name, value in API_HEADERS if name not in present
                )
                if sensitive:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, send_with_headers)
