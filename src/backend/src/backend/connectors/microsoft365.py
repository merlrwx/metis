"""Read a configured Microsoft 365 document-library drive through Graph."""

import asyncio
import json
import os
import posixpath
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from backend.connectors.base import (
    ConnectorError,
    DocumentChanges,
    ExpiredCheckpoint,
    RemoteDocument,
)
from backend.services.ingestion import MAX_UPLOAD_BYTES

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Microsoft365Source:
    def __init__(self, source_id: UUID):
        prefix = f"METIS_M365_{source_id.hex.upper()}_"
        try:
            if os.environ.get(prefix + "ORGANISATION_LIBRARY") != "true":
                raise ValueError("Library permission approval is required")
            self.tenant_id = str(UUID(os.environ[prefix + "TENANT_ID"]))
            self.client_id = str(UUID(os.environ[prefix + "CLIENT_ID"]))
            self.client_secret = os.environ[prefix + "CLIENT_SECRET"]
            self.drive_id = os.environ[prefix + "DRIVE_ID"]
            if not self.client_secret or not 0 < len(self.drive_id) <= 512:
                raise ValueError("Missing connector settings")
        except (KeyError, ValueError):
            raise ConnectorError(
                "Microsoft 365 source credentials or library approval are not configured"
            ) from None
        self.drive_url = f"{GRAPH_BASE}/drives/{quote(self.drive_id, safe='')}"
        self.opener = build_opener(NoRedirect())
        self.token = None

    def _read(self, request, limit):
        with self.opener.open(request, timeout=30) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ConnectorError("Connector response exceeds its size limit")
        return data

    def _authenticate(self):
        if self.token is None:
            body = urlencode(
                {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                }
            ).encode()
            request = Request(
                f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            payload = json.loads(self._read(request, 1024 * 1024))
            self.token = payload["access_token"]
            if not isinstance(self.token, str) or not self.token:
                raise ConnectorError("OAuth response did not contain an access token")

    def _validate_graph_url(self, url):
        if not isinstance(url, str):
            raise ConnectorError("Graph pagination URL is invalid")
        parts = urlsplit(url)
        if (
            len(url) > 8192
            or parts.scheme != "https"
            or parts.netloc != "graph.microsoft.com"
            or not posixpath.normpath(unquote(parts.path)).startswith(
                unquote(urlsplit(self.drive_url).path) + "/"
            )
        ):
            raise ConnectorError("Graph pagination URL is outside the configured drive")

    def _graph_json(self, url):
        self._validate_graph_url(url)
        self._authenticate()
        return json.loads(
            self._read(
                Request(url, headers={"Authorization": f"Bearer {self.token}"}),
                4 * 1024 * 1024,
            )
        )

    async def check_connection(self):
        root = await asyncio.to_thread(self._graph_json, self.drive_url + "/root")
        if not isinstance(root.get("name"), str) or "folder" not in root:
            raise ConnectorError("Configured library root is invalid")
        return {"library_name": root["name"][:255]}

    async def list_documents(self, checkpoint=None):
        url = checkpoint or self.drive_url + "/root/delta"
        changes = {}
        try:
            for _ in range(100):
                page = await asyncio.to_thread(self._graph_json, url)
                for item in page["value"]:
                    if "file" not in item and "deleted" not in item:
                        continue
                    external_id = item["id"]
                    if (
                        not isinstance(external_id, str)
                        or not 0 < len(external_id) <= 512
                    ):
                        raise ConnectorError("Invalid external document ID")
                    web_url = item.get("webUrl")
                    if web_url:
                        link = urlsplit(web_url)
                        if (
                            len(web_url) > 2048
                            or link.scheme != "https"
                            or not link.hostname
                            or not link.hostname.endswith(".sharepoint.com")
                            or link.username
                            or link.password
                            or link.port not in (None, 443)
                        ):
                            raise ConnectorError(
                                "Source link is outside Microsoft 365 SharePoint"
                            )
                    modified = item.get("lastModifiedDateTime")
                    etag = item.get("eTag") or item.get("cTag") or modified or ""
                    if "deleted" not in item and (
                        not isinstance(etag, str) or not 0 < len(etag) <= 1024
                    ):
                        raise ConnectorError(
                            "External document has no valid change marker"
                        )
                    changes[external_id] = RemoteDocument(
                        external_id,
                        item.get("name", ""),
                        item.get("file", {}).get("mimeType", ""),
                        etag,
                        datetime.fromisoformat(modified.replace("Z", "+00:00"))
                        if modified
                        else None,
                        item.get("webUrl"),
                        "deleted" in item,
                    )
                    if len(changes) > 10000:
                        raise ConnectorError(
                            "Delta response exceeds the document limit"
                        )
                url = page.get("@odata.nextLink")
                if not url:
                    delta = page["@odata.deltaLink"]
                    self._validate_graph_url(delta)
                    return DocumentChanges(
                        list(changes.values()), delta, checkpoint is None
                    )
            raise ConnectorError("Delta response exceeds the pagination limit")
        except HTTPError as error:
            if error.code == 410:
                raise ExpiredCheckpoint("Graph delta checkpoint expired") from None
            raise ConnectorError("Microsoft Graph document listing failed") from None
        except (URLError, KeyError, ValueError, TypeError):
            raise ConnectorError("Microsoft Graph document listing failed") from None

    def _download(self, document):
        self._authenticate()
        url = f"{self.drive_url}/items/{quote(document.external_id, safe='')}/content"
        headers = {"Authorization": f"Bearer {self.token}"}
        for _ in range(4):
            try:
                return self._read(Request(url, headers=headers), MAX_UPLOAD_BYTES)
            except HTTPError as error:
                if error.code != 302:
                    raise
                url = error.headers.get("Location", "")
                parts = urlsplit(url)
                if (
                    parts.scheme != "https"
                    or not parts.hostname
                    or not parts.hostname.endswith(".sharepoint.com")
                    or parts.username
                    or parts.password
                    or parts.port not in (None, 443)
                ):
                    raise ConnectorError(
                        "Download redirect is outside Microsoft 365 SharePoint"
                    ) from None
                # Preauthenticated download URLs never receive the Graph bearer token.
                headers = {}
        raise ConnectorError("Download redirect limit exceeded")

    async def fetch_document(self, document):
        try:
            return await asyncio.to_thread(self._download, document)
        except (HTTPError, URLError, KeyError, ValueError):
            raise ConnectorError("Microsoft Graph document download failed") from None
