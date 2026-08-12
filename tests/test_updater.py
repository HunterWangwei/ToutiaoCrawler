import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile

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
    def test_prerelease_is_older_than_same_number_stable_release(self):
        self.assertLess(updater._version_tuple("1.7.1-test"), updater._version_tuple("1.7.1"))

    def test_prerelease_stage_order(self):
        versions = ["v2.0.0-alpha", "2.0.0-beta", "2.0.0-test", "2.0.0-rc1", "2.0.0"]
        keys = [updater._version_tuple(value) for value in versions]
        self.assertEqual(keys, sorted(keys))

    def test_newer_numeric_version_remains_newer(self):
        self.assertLess(updater._version_tuple("1.7.1"), updater._version_tuple("1.7.2-test"))

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
        with tempfile.TemporaryDirectory() as folder:
            current_exe = Path(folder) / "ToutiaoCrawler.exe"
            current_exe.write_bytes(b"old")
            with (
                patch.object(updater, "_session", return_value=session),
                patch.object(updater, "can_self_update", return_value=True),
                patch.object(updater.sys, "executable", str(current_exe)),
            ):
                target = updater.download_release(
                    release, progress=lambda done, total: events.append((done, total))
                )
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(target.name, "ToutiaoCrawler-99.99.99-test-progress.exe")
            self.assertEqual(events[0], (0, len(payload)))
            self.assertEqual(events[-1], (len(payload), len(payload)))

    def test_install_starts_versioned_exe_directly(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "ToutiaoCrawler-9.9.9.exe"
            target.write_bytes(b"exe")
            with (
                patch.object(updater, "can_self_update", return_value=True),
                patch.object(updater.subprocess, "Popen") as popen,
            ):
                updater.install_and_restart(target)
            popen.assert_called_once()
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [str(target.resolve())])
            self.assertEqual(kwargs["cwd"], str(target.parent.resolve()))

    def test_proxy_fallback_runs_after_primary_failure(self):
        attempts = []
        notices = []

        def operation(proxy, is_builtin):
            attempts.append((proxy, is_builtin))
            if not is_builtin:
                raise OSError("primary unavailable")
            return "ok"

        with patch.object(updater, "BUILTIN_UPDATE_PROXY", "socks5h://fallback.example:1080"):
            result = updater._run_with_proxy_fallback(
                operation, "", lambda: notices.append("fallback")
            )

        self.assertEqual(result, "ok")
        self.assertEqual(
            attempts,
            [("", False), ("socks5h://fallback.example:1080", True)],
        )
        self.assertEqual(notices, ["fallback"])

    def test_user_proxy_is_tried_before_builtin_proxy(self):
        with patch.object(updater, "BUILTIN_UPDATE_PROXY", "socks5h://fallback.example:1080"):
            self.assertEqual(
                updater._proxy_candidates("socks5h://user.example:1080"),
                [
                    ("socks5h://user.example:1080", False),
                    ("socks5h://fallback.example:1080", True),
                ],
            )
