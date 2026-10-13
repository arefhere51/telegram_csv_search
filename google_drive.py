"""Stream public Google Drive share links without Google Cloud API credentials.

The shared file must be accessible to "Anyone with the link". Private Drive files
cannot be downloaded by this credential-free implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
from typing import Callable
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx

DRIVE_CHUNK_BYTES = 1024 * 1024
_ALLOWED_HOSTS = {
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
    "drive.googleusercontent.com",
}
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_RESOURCE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_CSV_MIME_TYPES = {"text/csv", "application/csv", "application/vnd.ms-excel", "text/plain"}


class DriveDownloadError(RuntimeError):
    """A safe, user-displayable Drive download error."""


class DriveAccessDenied(DriveDownloadError):
    pass


class DriveFileTooLarge(DriveDownloadError):
    def __init__(self, actual: int, maximum: int):
        self.actual = actual
        self.maximum = maximum
        super().__init__("Drive file exceeds the configured upload limit.")


class DriveInsufficientSpace(DriveDownloadError):
    def __init__(self, required: int, available: int):
        self.required = required
        self.available = available
        super().__init__("There is not enough free disk space for the Drive download.")


class DriveNotCsv(DriveDownloadError):
    pass


@dataclass(frozen=True, slots=True)
class DriveFileReference:
    file_id: str
    resource_key: str | None = None


@dataclass(frozen=True, slots=True)
class DriveFileInfo:
    file_id: str
    name: str
    size: int | None
    mime_type: str


def parse_drive_share_link(value: str) -> DriveFileReference:
    """Extract a file ID and optional resource key from a Google Drive share URL."""
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    if (parsed.scheme.lower() != "https" or host not in _ALLOWED_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise ValueError("Send a secure Google Drive file-sharing link.")
    segments = [part for part in unquote(parsed.path).split("/") if part]
    if "folders" in segments:
        raise ValueError("A folder link is not a CSV file link.")
    query = parse_qs(parsed.query, keep_blank_values=True)
    candidates = {query.get("id", [""])[0].strip()} if query.get("id", [""])[0].strip() else set()
    for index, part in enumerate(segments[:-1]):
        if part == "d":
            candidates.add(segments[index + 1].strip())
    if len(candidates) != 1:
        raise ValueError("The Drive link does not contain one clear file ID.")
    file_id = candidates.pop()
    if not _FILE_ID_RE.fullmatch(file_id):
        raise ValueError("The Drive file ID in the link is not valid.")
    resource_key = query.get("resourcekey", [""])[0].strip() or query.get("resourceKey", [""])[0].strip() or None
    if resource_key and not _RESOURCE_KEY_RE.fullmatch(resource_key):
        raise ValueError("The resource key in the link is not valid.")
    return DriveFileReference(file_id=file_id, resource_key=resource_key)


class _DownloadFormParser(HTMLParser):
    """Find Google's large-file confirmation form without an HTML dependency."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[tuple[str, dict[str, str]]] = []
        self._action: str | None = None
        self._fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "form":
            self._action = attrs.get("action")
            self._fields = {}
        elif tag.lower() == "input" and self._action:
            name = attrs.get("name")
            if name:
                self._fields[name] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag.lower() == "form" and self._action:
            self.forms.append((self._action, self._fields.copy()))
            self._action = None
            self._fields = {}


def _validate_google_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
        raise DriveDownloadError("Google Drive returned an unexpected download address.")


def _filename_from_response(response: httpx.Response) -> str:
    disposition = response.headers.get("content-disposition", "")
    message = Message()
    message["content-disposition"] = disposition
    mime_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].lower()
    filename = message.get_filename()
    if not filename:
        if mime_type not in _CSV_MIME_TYPES:
            raise DriveNotCsv("Only CSV or TXT files can be imported from Google Drive.")
        filename = "drive_upload.txt" if mime_type == "text/plain" else "drive_upload.csv"
    filename = Path(filename.replace("\\", "/")).name.strip()
    if not filename:
        filename = "drive_upload.csv"
    if mime_type in {"text/html", "application/xhtml+xml"}:
        raise DriveAccessDenied("The file must be shared with anyone who has the link.")
    if Path(filename).suffix.lower() in {".csv", ".txt"}:
        return filename
    if mime_type in _CSV_MIME_TYPES:
        return filename + (".txt" if mime_type == "text/plain" else ".csv")
    raise DriveNotCsv("Only CSV or TXT files can be imported from Google Drive.")


def _save_stream_response(
    response: httpx.Response,
    reference: DriveFileReference,
    destination: Path,
    max_bytes: int,
    progress_callback: Callable[[str, int, int | None], None] | None,
) -> DriveFileInfo:
    _validate_google_url(str(response.url))
    if response.status_code in {401, 403, 404}:
        raise DriveAccessDenied()
    if response.status_code >= 400:
        raise DriveDownloadError(f"Google Drive returned HTTP status {response.status_code}.")
    filename = _filename_from_response(response)
    mime_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].lower()
    try:
        total = int(response.headers["content-length"]) if response.headers.get("content-length") else None
    except ValueError:
        total = None
    if total is not None and total > max_bytes:
        raise DriveFileTooLarge(total, max_bytes)
    available = shutil.disk_usage(destination.parent).free
    if total is not None and available < total:
        raise DriveInsufficientSpace(total, available)
    if progress_callback:
        progress_callback(filename, 0, total)
    received = 0
    with destination.open("wb") as output:
        for chunk in response.iter_bytes(chunk_size=DRIVE_CHUNK_BYTES):
            if not chunk:
                continue
            received += len(chunk)
            if received > max_bytes:
                raise DriveFileTooLarge(received, max_bytes)
            output.write(chunk)
            if progress_callback:
                progress_callback(filename, received, total)
    if received == 0:
        raise DriveDownloadError("Google Drive returned an empty file.")
    if total is not None and received != total:
        raise DriveDownloadError("The downloaded file size did not match Google Drive.")
    return DriveFileInfo(reference.file_id, filename, received, mime_type)


def download_drive_csv(
    settings,
    reference: DriveFileReference,
    destination: Path,
    *,
    max_bytes: int,
    progress_callback: Callable[[str, int, int | None], None] | None = None,
) -> DriveFileInfo:
    """Download a publicly shared Drive CSV incrementally; no API key is used."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    available = shutil.disk_usage(destination.parent).free
    if available < 1:
        raise DriveInsufficientSpace(1, available)

    params = {"export": "download", "id": reference.file_id}
    if reference.resource_key:
        params["resourcekey"] = reference.resource_key
    url = "https://drive.google.com/uc"
    destination.unlink(missing_ok=True)
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120, connect=20)) as client:
            confirm_request: tuple[str, dict[str, str]] | None = None
            with client.stream("GET", url, params=params) as response:
                _validate_google_url(str(response.url))
                if response.status_code in {401, 403, 404}:
                    raise DriveAccessDenied()
                if response.status_code >= 400:
                    raise DriveDownloadError(f"Google Drive returned HTTP status {response.status_code}.")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    return _save_stream_response(response, reference, destination, max_bytes, progress_callback)
                html = response.read().decode("utf-8", errors="replace")
                parser = _DownloadFormParser()
                parser.feed(html)
                confirmation = next(
                    (value for name, value in response.cookies.items() if name.startswith("download_warning")),
                    None,
                )
                if parser.forms:
                    action, fields = parser.forms[0]
                    for name, value in fields.items():
                        if name.lower() == "confirm":
                            confirmation = value
                            break
                    if confirmation:
                        fields.setdefault("confirm", confirmation)
                    next_url = urljoin(str(response.url), action)
                    _validate_google_url(next_url)
                    confirm_request = (next_url, fields)
                elif confirmation:
                    params["confirm"] = confirmation
                    confirm_request = (url, params)
                else:
                    raise DriveAccessDenied()
            if confirm_request is None:
                raise DriveAccessDenied()
            confirm_url, confirm_params = confirm_request
            with client.stream("GET", confirm_url, params=confirm_params) as response:
                if response.headers.get("content-type", "").split(";", 1)[0].lower() in {"text/html", "application/xhtml+xml"}:
                    raise DriveAccessDenied()
                return _save_stream_response(response, reference, destination, max_bytes, progress_callback)
    except DriveDownloadError:
        destination.unlink(missing_ok=True)
        raise
    except httpx.HTTPError as exc:
        destination.unlink(missing_ok=True)
        raise DriveDownloadError("Google Drive could not be reached or the download was interrupted.") from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise
