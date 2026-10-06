import json
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class MetisApi:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict | None = None,
        form: dict[str, str] | None = None,
        upload: tuple[str, bytes, str] | None = None,
        source_id: str | None = None,
        raw: bool = False,
    ):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif upload is not None:
            body, content_type = self._multipart(upload, source_id)
            headers["Content-Type"] = content_type

        request = Request(
            f"{self.base_url}{path}", data=body, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=60 if upload is not None else 30) as response:
                content = response.read()
        except HTTPError as error:
            detail = error.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            raise ApiError(error.code, str(detail)) from error
        except (URLError, TimeoutError, OSError) as error:
            raise ApiError(
                0, "Cannot connect to the Metis API. Check the backend and try again."
            ) from error

        if raw:
            return content
        if not content:
            return None
        try:
            return json.loads(content)
        except (ValueError, UnicodeDecodeError) as error:
            raise ApiError(
                502, "The Metis API returned an invalid response."
            ) from error

    def register(self, email: str, name: str, password: str) -> dict:
        return self.request(
            "POST",
            "/api/auth/register",
            payload={"email": email, "name": name, "password": password},
        )

    def login(self, email: str, password: str) -> str:
        response = self.request(
            "POST",
            "/api/auth/token",
            form={"username": email, "password": password},
        )
        if not isinstance(response, dict) or not isinstance(
            response.get("access_token"), str
        ):
            raise ApiError(502, "The Metis API did not return a sign-in token.")
        return response["access_token"]

    @staticmethod
    def _multipart(
        upload: tuple[str, bytes, str], source_id: str | None
    ) -> tuple[bytes, str]:
        filename, content, mime_type = upload
        safe_filename = "".join(
            character
            if character.isascii() and (character.isalnum() or character in "._- ")
            else "_"
            for character in filename
        )
        safe_filename = safe_filename.strip() or "upload"
        safe_mime_type = (
            mime_type
            if mime_type
            and all(
                character.isascii() and (character.isalnum() or character in "/.+-_ ")
                for character in mime_type
            )
            else "application/octet-stream"
        )
        boundary = f"metis-{uuid.uuid4().hex}"
        chunks = []
        if source_id:
            chunks.append(
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="source_id"\r\n\r\n'
                f"{source_id}\r\n".encode()
            )
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'
            f"Content-Type: {safe_mime_type}\r\n\r\n".encode()
        )
        chunks.extend((content, b"\r\n", f"--{boundary}--\r\n".encode()))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
