import ast
import hashlib
import json
import unittest
import tempfile
from pathlib import Path
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "src/s5a3_deploy_tool.py").read_text(encoding="utf-8-sig"))
FUNCTIONS = [node for node in TREE.body
             if isinstance(node, ast.FunctionDef) and node.name in {
                 "_run_as_file_owner", "_pm_token_reset_helper", "_pm_gm_login_check_helper"}]
NS = {"q": lambda value: "'" + str(value).replace("'", "'\\''") + "'"}
exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]), "owner-helper", "exec"), NS)


class TokenResetOwnerTests(unittest.TestCase):
    def test_uses_gateway_file_owner_and_never_root(self):
        command = NS["_run_as_file_owner"](
            "/opt/purplemagic/data2/gateway.json",
            "env HOME=/tmp python3 /tmp/pm-reset-token.py 2>&1")
        self.assertIn("stat -c '%U' '/opt/purplemagic/data2/gateway.json'", command)
        self.assertIn('test "$owner" != root', command)
        self.assertIn('runuser -u "$owner" -- env HOME=/tmp python3', command)
        self.assertNotIn("dnf90gm", command)

    def test_quotes_configured_path(self):
        command = NS["_run_as_file_owner"]("/opt/data/with'quote/gateway.json", "true")
        self.assertIn(NS["q"]("/opt/data/with'quote/gateway.json"), command)

    def test_reset_updates_gm_service_and_both_gateway_hashes(self):
        helper = NS["_pm_token_reset_helper"]("/opt/purplemagic/data2")
        compile(helper, "generated-token-reset-helper", "exec")
        self.assertIn("service_path = base / 'service.json'", helper)
        self.assertIn("gateway['management']['loginKeySha256']", helper)
        self.assertIn("gateway['launcher_hub']['admin_token_sha256']", helper)
        self.assertIn("service['loginKeySha256']", helper)
        self.assertIn("for path in reversed(written)", helper)

    def test_reset_helper_synchronizes_fixture_configs(self):
        token = "local-test-token-value"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "private").mkdir()
            gateway_path = base / "gateway.json"
            service_path = base / "service.json"
            token_path = base / "admin-token.txt"
            payload_path = base / "token-payload.json"
            gateway_path.write_text(json.dumps({
                "management": {"loginKeySha256": "old"},
                "launcher_hub": {"admin_token_sha256": "old"},
            }))
            service_path.write_text(json.dumps({"loginKeySha256": "old"}))
            token_path.write_text("previous-token\n")
            digest = hashlib.sha256(token.encode()).hexdigest()
            payload_path.write_text(json.dumps({"token": token, "sha256": digest}))
            helper = NS["_pm_token_reset_helper"](str(base), str(payload_path))
            exec(compile(helper, "fixture-token-reset", "exec"), {})
            gateway = json.loads(gateway_path.read_text())
            service = json.loads(service_path.read_text())
            self.assertEqual(gateway["management"]["loginKeySha256"], digest)
            self.assertEqual(gateway["launcher_hub"]["admin_token_sha256"], digest)
            self.assertEqual(service["loginKeySha256"], digest)
            self.assertEqual(token_path.read_text(), token + "\n")

    def test_reset_checks_real_gm_login_without_logging_token(self):
        helper = NS["_pm_gm_login_check_helper"]()
        compile(helper, "generated-gm-login-check", "exec")
        self.assertIn("/api/auth/login", helper)
        self.assertIn("/api/auth/logout", helper)
        self.assertNotIn("print(token", helper)


if __name__ == "__main__":
    unittest.main()
