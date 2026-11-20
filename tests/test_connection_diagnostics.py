import ast
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from local_diagnostics import SessionLog, redact


class AuthError(Exception): pass
class KeyErrorSSH(Exception): pass
class HandshakeError(Exception): pass


class ConnectionTests(unittest.TestCase):
    def client(self, error=None, tcp_error=None):
        tree = ast.parse((ROOT / "src/s5a3_deploy_tool.py").read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SSHClient")
        remote = SimpleNamespace(connect=Mock(side_effect=error), close=Mock(), set_missing_host_key_policy=Mock())
        connection = SimpleNamespace(close=Mock())
        tcp = Mock(return_value=connection, side_effect=tcp_error)
        paramiko = SimpleNamespace(SSHClient=lambda: remote, AutoAddPolicy=lambda: None,
                                   AuthenticationException=AuthError, BadHostKeyException=KeyErrorSSH)
        namespace = {"paramiko": paramiko, "socket": SimpleNamespace(create_connection=tcp)}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "ssh-client", "exec"), namespace)
        return namespace["SSHClient"]("192.0.2.1", "root", "fixture-password"), remote, connection, tcp

    def test_transport_deadline_exceeds_banner_deadline(self):
        client, remote, connection, tcp = self.client()
        self.assertIs(client.connect(), client)
        self.assertEqual(tcp.call_args.kwargs["timeout"], 10)
        args = remote.connect.call_args.kwargs
        self.assertGreater(args["timeout"], args["banner_timeout"])
        self.assertEqual(args["auth_timeout"], 30)
        self.assertIs(args["sock"], connection)

    def test_banner_failure_does_not_report_bad_password_and_closes_socket(self):
        for error in (HandshakeError("No existing session"), HandshakeError("Error reading SSH protocol banner")):
            client, remote, connection, _ = self.client(error)
            with self.assertRaisesRegex(RuntimeError, "未完成 SSH 握手.*尚未验证密码"):
                client.connect()
            self.assertIsNone(client.client)
            remote.close.assert_called_once()
            connection.close.assert_called_once()

    def test_authentication_failure_is_distinct(self):
        client, _, _, _ = self.client(AuthError("fixture-password"))
        with self.assertRaisesRegex(RuntimeError, "SSH 认证失败") as caught:
            client.connect()
        self.assertNotIn("fixture-password", str(caught.exception))

    def test_tcp_failure_does_not_attempt_authentication(self):
        client, remote, _, _ = self.client(tcp_error=socket.timeout("timed out"))
        with self.assertRaisesRegex(RuntimeError, "连接端口失败"):
            client.connect()
        remote.connect.assert_not_called()

    def test_local_logs_redact_secrets_and_survive_missing_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            logger = SessionLog(Path(temp) / "logs")
            text = 'password=example-private token: "fixture-token" https://user:private@example.test raw-password'
            clean = logger.write(text, ["raw-password"])
            saved = logger.path.read_text(encoding="utf-8")
            for value in ("example-private", "fixture-token", "user:private", "raw-password"):
                self.assertNotIn(value, clean)
                self.assertNotIn(value, saved)
            blocked = Path(temp) / "not-a-directory"; blocked.write_text("fixture")
            disabled = SessionLog(blocked / "logs")
            self.assertIsNone(disabled.path)
            self.assertEqual(disabled.write("still usable"), "still usable")


if __name__ == "__main__": unittest.main()
