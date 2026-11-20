import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import self_update as update
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.public = base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        self.binary = b"MZ" + b"new-release" * 150
        self.data = {"format": 1, "product": "s5a3-deploy-tool", "version": "2026.11.20.4",
                     "url": f"https://github.com/{update.REPO}/releases/download/v2026.11.20.4/S5A3-ServerManager-2026.11.20.4.exe",
                     "size": len(self.binary), "sha256": hashlib.sha256(self.binary).hexdigest(), "notes": "fixture"}
        self.envelope = self.sign(self.data)
        original = update.verify
        self.verify = patch.object(update, "verify", side_effect=lambda value: original(value, self.public))

    def sign(self, data):
        raw = json.dumps(data).encode()
        return {"payload": base64.b64encode(raw).decode(), "signature": base64.b64encode(self.key.sign(raw)).decode()}

    def test_signature_product_origin_and_length_are_checked(self):
        self.assertEqual(update.verify(self.envelope, self.public), self.data)
        with self.assertRaises(Exception):
            update.verify({**self.envelope, "payload": base64.b64encode(b"tampered").decode()}, self.public)
        for change in [{"product": "other"}, {"url": "https://evil.example/file.exe"},
                       {"size": update.MAX_EXE + 1}, {"size": True}, {"version": "../bad"}]:
            with self.subTest(change=change), self.assertRaises(Exception):
                update.verify(self.sign(self.data | change), self.public)

    def test_download_checks_hash_and_refuses_downgrade(self):
        with self.verify, patch.object(update, "_open", return_value=io.BytesIO(self.binary)):
            folder = update.stage(self.envelope, "2026.11.20.3")
            self.assertEqual((folder / "download.exe").read_bytes(), self.binary)
            with self.assertRaises(ValueError):
                update.stage(self.envelope, "2026.11.20.4")
        with self.verify, patch.object(update, "_open", return_value=io.BytesIO(b"MZ" + b"x" * (len(self.binary) - 2))):
            with self.assertRaises(ValueError):
                update.stage(self.envelope, "2026.11.20.3")

    def test_release_check_ignores_equal_and_older_versions(self):
        for current, expected in [("2026.11.20.3", True), ("2026.11.20.4", False), ("2026.11.21.1", False)]:
            with self.verify, patch.object(update, "_open", return_value=io.BytesIO(json.dumps(self.envelope).encode())):
                self.assertEqual(bool(update.check(current)), expected)

    def run_install(self, successful, tamper=False):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            target = folder / "tool.exe"
            old = b"MZoriginal-program"
            target.write_bytes(old)
            config = folder / "s5a3_config.json"
            config.write_bytes(b"private-config-fixture")
            (folder / "download.exe").write_bytes(self.binary if not tamper else b"broken")
            job = {"target": str(target), "parent": 123, "current": "2026.11.20.3",
                   "envelope": self.envelope, "nonce": "test-nonce"}
            path = folder / "job.json"
            path.write_text(json.dumps(job), encoding="utf-8")
            calls = []
            class Child:
                pid = 321
                def poll(self): return None if successful else 1
                def terminate(self): pass
                def wait(self, timeout): return 0
            def launch(args, **kwargs):
                calls.append(args)
                if successful and "--update-ready" in args:
                    (folder / "ready.json").write_text(json.dumps({"nonce": job["nonce"], "pid": 654, "version": self.data["version"]}))
                return Child()
            with self.verify, patch.object(update, "_wait_parent") as wait, patch.object(update.subprocess, "Popen", side_effect=launch), patch.object(update.ctypes.windll.user32, "MessageBoxW", return_value=0):
                result = update.apply(path)
            self.assertEqual(config.read_bytes(), b"private-config-fixture")
            self.assertEqual(target.read_bytes(), self.binary if successful and not tamper else old)
            self.assertEqual(result, 0 if successful and not tamper else 1)
            if successful and not tamper:
                self.assertEqual(target.with_name("tool.exe.previous").read_bytes(), old)
            if tamper:
                wait.assert_not_called()
                self.assertEqual(calls, [])
            elif not successful:
                self.assertIn("--update-rollback", calls[-1])

    def test_successful_install_keeps_configuration_and_backup(self):
        self.run_install(True)

    def test_failed_start_restores_old_program_and_configuration(self):
        self.run_install(False)

    def test_tampered_stage_never_replaces_program(self):
        self.run_install(False, tamper=True)


if __name__ == "__main__":
    unittest.main()
