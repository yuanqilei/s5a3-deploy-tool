import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "src/s5a3_deploy_tool.py").read_text(encoding="utf-8-sig"))
FUNCTION = next(node for node in TREE.body
                if isinstance(node, ast.FunctionDef) and node.name == "_run_as_file_owner")
NS = {"q": lambda value: "'" + str(value).replace("'", "'\\''") + "'"}
exec(compile(ast.Module(body=[FUNCTION], type_ignores=[]), "owner-helper", "exec"), NS)


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


if __name__ == "__main__":
    unittest.main()
