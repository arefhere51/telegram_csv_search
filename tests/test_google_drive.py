from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from google_drive import (
    DriveAccessDenied,
    DriveFileTooLarge,
    DriveFileReference,
    DriveNotCsv,
    download_drive_csv,
    parse_drive_share_link,
)

FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
RESOURCE_KEY = "AbCdEfGhIjKlMnOpQrStUvWxyz0123456789"


class FakeResponse:
    def __init__(self, *, body=b"", status_code=200, headers=None, url="https://drive.google.com/uc"):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.cookies = {}
        self.text = body.decode("utf-8", errors="replace")
        self.closed = False

    def iter_bytes(self, chunk_size):
        for start in range(0, len(self.body), 3):
            yield self.body[start:start + 3]

    def close(self):
        self.closed = True

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class GoogleDriveLinkTests(unittest.TestCase):
    def test_file_share_link_and_resource_key_are_parsed(self):
        self.assertEqual(
            parse_drive_share_link(f"https://drive.google.com/file/d/{FILE_ID}/view?resourcekey={RESOURCE_KEY}"),
            DriveFileReference(FILE_ID, RESOURCE_KEY),
        )

    def test_open_link_with_id_query_is_parsed(self):
        self.assertEqual(
            parse_drive_share_link(f"https://drive.google.com/open?id={FILE_ID}"),
            DriveFileReference(FILE_ID),
        )

    def test_folder_external_and_http_links_are_rejected(self):
        links = [
            f"https://drive.google.com/drive/folders/{FILE_ID}",
            f"https://example.com/file/d/{FILE_ID}/view",
            f"http://drive.google.com/file/d/{FILE_ID}/view",
        ]
        for link in links:
            with self.subTest(link=link), self.assertRaises(ValueError):
                parse_drive_share_link(link)

    def test_conflicting_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_drive_share_link(
                f"https://drive.google.com/file/d/{FILE_ID}/view?id=2AbCdEfGhIjKlMnOpQrStUvWxYz012345"
            )


class GoogleDriveDownloadTests(unittest.TestCase):
    def setUp(self):
        self.settings = object()  # Drive downloader no longer requires Google Cloud credentials.

    def test_public_download_is_streamed_without_cloud_credentials(self):
        payload = b"name,phone\nLeila,09120000000\n"
        response = FakeResponse(
            body=payload,
            headers={
                "content-type": "text/csv; charset=utf-8",
                "content-disposition": 'attachment; filename="customers.csv"',
                "content-length": str(len(payload)),
            },
        )
        client = FakeClient([response])
        progress = []
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "file.part"
            with patch("google_drive.httpx.Client", return_value=client):
                info = download_drive_csv(
                    self.settings,
                    DriveFileReference(FILE_ID, RESOURCE_KEY),
                    destination,
                    max_bytes=1000,
                    progress_callback=lambda name, received, total: progress.append((name, received, total)),
                )
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(info.name, "customers.csv")
            self.assertEqual(info.size, len(payload))
            self.assertEqual(progress[0], ("customers.csv", 0, len(payload)))
            self.assertEqual(progress[-1][1:], (len(payload), len(payload)))
            self.assertEqual(client.calls[0][1]["params"]["resourcekey"], RESOURCE_KEY)

    def test_confirmation_form_is_followed_for_large_file(self):
        payload = b"a,b\n1,2\n"
        html = b'<form action="https://drive.usercontent.google.com/download"><input name="id" value="FILE"><input name="export" value="download"><input name="confirm" value="token"></form>'
        first = FakeResponse(body=html, headers={"content-type": "text/html"})
        second = FakeResponse(
            body=payload,
            url="https://drive.usercontent.google.com/download",
            headers={"content-type": "text/csv", "content-disposition": 'attachment; filename="x.csv"', "content-length": str(len(payload))},
        )
        client = FakeClient([first, second])
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "file.part"
            with patch("google_drive.httpx.Client", return_value=client):
                info = download_drive_csv(self.settings, DriveFileReference(FILE_ID), destination, max_bytes=100)
            self.assertEqual(info.name, "x.csv")
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(client.calls[1][1]["params"]["confirm"], "token")

    def test_file_over_limit_is_rejected_before_download(self):
        response = FakeResponse(
            body=b"x" * 101,
            headers={"content-type": "text/csv", "content-disposition": 'attachment; filename="large.csv"', "content-length": "101"},
        )
        client = FakeClient([response])
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "partial.part"
            with patch("google_drive.httpx.Client", return_value=client):
                with self.assertRaises(DriveFileTooLarge):
                    download_drive_csv(self.settings, DriveFileReference(FILE_ID), destination, max_bytes=100)
            self.assertFalse(destination.exists())

    def test_private_file_access_page_is_rejected(self):
        response = FakeResponse(body=b"<html>Sign in</html>", headers={"content-type": "text/html"})
        client = FakeClient([response])
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "partial.part"
            with patch("google_drive.httpx.Client", return_value=client):
                with self.assertRaises(DriveAccessDenied):
                    download_drive_csv(self.settings, DriveFileReference(FILE_ID), destination, max_bytes=1000)
            self.assertFalse(destination.exists())

    def test_non_csv_mime_type_is_rejected(self):
        response = FakeResponse(
            body=b"data",
            headers={"content-type": "application/pdf", "content-length": "4"},
        )
        client = FakeClient([response])
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "partial.part"
            with patch("google_drive.httpx.Client", return_value=client):
                with self.assertRaises(DriveNotCsv):
                    download_drive_csv(self.settings, DriveFileReference(FILE_ID), destination, max_bytes=1000)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
