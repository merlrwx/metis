"""Templates, filters and the shared page context."""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from metis_web.session import FLASH_COOKIE

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Model output is untrusted: raw HTML is disabled and links are validated.
_markdown = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")

STATUS_LABELS = {
    "indexed": ("Indexed", "ok"),
    "completed": ("Indexed", "ok"),
    "pending": ("Queued", "busy"),
    "queued": ("Queued", "busy"),
    "processing": ("Processing", "busy"),
    "failed": ("Failed", "bad"),
    "deleted": ("Removed", "muted"),
}
OUTCOME_LABELS = {
    "partially_answered": "Partially answered",
    "clarification_needed": "Needs clarification",
    "insufficient_evidence": "No supporting evidence",
}
ROLE_LABELS = {"owner": "Owner", "admin": "Admin", "member": "Member"}


def markdown(text: str | None) -> Markup:
    return Markup(_markdown.render(text or ""))  # noqa: S704 - html disabled above


def parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def when(value, fallback: str = "—") -> str:
    parsed = parse_time(value)
    if parsed is None:
        return fallback
    utc = parsed.astimezone(UTC)
    return f"{utc.day} {utc.strftime('%b %Y, %H:%M')} UTC"


def ago(value, fallback: str = "—") -> str:
    parsed = parse_time(value)
    if parsed is None:
        return fallback
    seconds = int((datetime.now(UTC) - parsed).total_seconds())
    if seconds < 60:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return when(value)


def status(value: str | None) -> tuple[str, str]:
    if value is None:
        return ("Metadata only", "muted")
    return STATUS_LABELS.get(value, (value.replace("_", " ").capitalize(), "muted"))


def safe_url(value: str | None) -> str | None:
    try:
        parts = urlsplit(value or "")
    except ValueError:
        return None
    return value if parts.scheme in {"http", "https"} and parts.netloc else None


def device(user_agent: str | None) -> str:
    agent = (user_agent or "").lower()
    if not agent:
        return "Unknown device"
    if agent == "metis-web":
        return "Metis web app"
    browser = next(
        (
            name
            for key, name in (
                ("edg/", "Edge"),
                ("firefox", "Firefox"),
                ("chrome", "Chrome"),
                ("safari", "Safari"),
                ("curl", "curl"),
                ("python", "API client"),
            )
            if key in agent
        ),
        "Browser",
    )
    system = next(
        (
            name
            for key, name in (
                ("android", "Android"),
                ("iphone", "iOS"),
                ("ipad", "iPadOS"),
                ("windows", "Windows"),
                ("mac os", "macOS"),
                ("linux", "Linux"),
            )
            if key in agent
        ),
        "",
    )
    return f"{browser} on {system}" if system else browser


templates.env.filters.update(
    markdown=markdown,
    when=when,
    ago=ago,
    status=status,
    safe_url=safe_url,
    device=device,
)
templates.env.globals.update(
    OUTCOME_LABELS=OUTCOME_LABELS, ROLE_LABELS=ROLE_LABELS, ASSET_VERSION="1"
)


def flash(request: Request, message: str, kind: str = "success") -> None:
    request.state.flash_out = (kind, message)


def render(
    request: Request, name: str, context: dict | None = None, status_code: int = 200
) -> HTMLResponse:
    codec = request.app.state.codec
    incoming = codec.decode_flash(request.cookies.get(FLASH_COOKIE))
    if incoming:
        request.state.flash_consumed = True
    pending = getattr(request.state, "flash_out", None)
    messages = [incoming] if incoming else []
    if pending:
        messages.append({"kind": pending[0], "message": pending[1]})
        request.state.flash_out = None
    page = {
        "request": request,
        "csrf_token": codec.csrf_token(request.state.csrf_cookie),
        "flashes": messages,
        "signed_in": bool(request.state.auth),
        **getattr(request.state, "page", {}),
        **(context or {}),
    }
    return templates.TemplateResponse(request, name, page, status_code=status_code)


def redirect(request: Request, url: str, message: str | None = None, kind="success"):
    if message:
        flash(request, message, kind)
    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        response = HTMLResponse("")
        response.headers["HX-Redirect"] = url
        return response
    return RedirectResponse(url, status_code=303)
