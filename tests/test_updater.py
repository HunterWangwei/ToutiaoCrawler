import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

import updater


class FakeResponse:
    def __init__(self, *, content=b"", text="", headers=None):
        self.content = content
        self.text = text
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        midpoint = max(1, len(self.content) // 2)
        yield self.content[:midpoint]
        yield self.content[midpoint:]


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def get(self, *args, **kwargs):
        return next(self.responses)


class UpdaterProgressTests(unittest.TestCase):
    def test_download_reports_progress_and_verifies_hash(self):
        payload = b"new-executable-content"
        expected = hashlib.sha256(payload).hexdigest()
        session = FakeSession([
            FakeResponse(text=expected),
            FakeResponse(content=payload, headers={"Content-Length": str(len(payload))}),
        ])
        events = []
        release = {
            "version": "99.99.99-test-progress",
            "checksum_url": "https://example/checksum",
            "exe_url": "https://example/exe",
        }
        with patch.object(updater, "_session", return_value=session):
            target = updater.download_release(release, progress=lambda done, total: events.append((done, total)))
        try:
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(events[0], (0, len(payload)))
            self.assertEqual(events[-1], (len(payload), len(payload)))
        finally:
            Path(target).unlink(missing_ok=True)
