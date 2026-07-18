"""Polite HTTP with conditional GET and content-type routing."""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, urlparse, urlunparse

DEFAULT_TIMEOUT = 30


def _sanitize_url(url: str) -> str:
    """Percent-encode unsafe characters (e.g. spaces) in the URL path/query."""
    parts = urlparse(url)
    clean_path = quote(parts.path, safe="/:@!$&'()*+,;=-._~")
    clean_query = quote(parts.query, safe="/:@!$&'()*+,;=-._~?=")
    return urlunparse(parts._replace(path=clean_path, query=clean_query))


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    data: bytes
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300 and bool(self.data)

    @property
    def not_modified(self) -> bool:
        return self.status == 304


def fetch(
    url,
    user_agent,
    etag=None,
    last_modified=None,
    timeout=DEFAULT_TIMEOUT,
    opener=urllib.request.urlopen,
) -> FetchResult:
    url = _sanitize_url(url)
    headers = {"User-Agent": user_agent}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener(req, timeout=timeout) as resp:
            data = resp.read()
            return FetchResult(
                url=url,
                final_url=resp.geturl(),
                status=getattr(resp, "status", 200) or 200,
                content_type=resp.headers.get_content_type(),
                data=data,
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResult(url, url, 304, "", b"")
        return FetchResult(url, url, exc.code, "", b"", error=f"HTTP {exc.code}")
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        http.client.InvalidURL,
        http.client.IncompleteRead,
    ) as exc:
        return FetchResult(url, url, 0, "", b"", error=str(exc))


_BINARY_EXT = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".bmp",
    ".zip",
    ".gz",
    ".tar",
    ".rar",
    ".7z",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".mp4",
    ".mp3",
    ".avi",
    ".mov",
    ".wav",
    ".ogg",
    ".css",
    ".js",
    ".json",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
)


def classify(content_type, url) -> str:
    ct = (content_type or "").lower()
    path = urlparse(url).path.lower()
    if "pdf" in ct or path.endswith(".pdf"):
        return "pdf"
    if "html" in ct or "xml" in ct or ct.startswith("text/"):
        return "html"
    if ct:
        return "other"
    if path.endswith(_BINARY_EXT):
        return "other"
    return "html"


def ext_for(kind) -> str:
    return {"html": ".html", "pdf": ".pdf"}.get(kind, ".bin")
