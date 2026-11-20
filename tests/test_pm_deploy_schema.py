"""Exercise deployment orchestration with a fake SSH client; no network or GUI."""
import ast
from pathlib import Path
import shlex
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src" / "s5a3_deploy_tool.py"
tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
method = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "do_pm_deploy")
namespace = {"q": shlex.quote}
exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE), "exec"), namespace)


class FakeSSH:
    def __init__(self, indexed, fail=""):
        self.indexed, self.fail = indexed, fail
        self.commands = []
        self.closed = False

    def capture(self, command):
        return 0, ("yes" if self.indexed else "") if "indexes/items.json" in command else "ok"

    def run(self, command, callback, timeout):
        self.commands.append(command)
        return 1 if self.fail and self.fail in command else 0

    def close(self):
        self.closed = True


class FakeTool:
    do_pm_deploy = namespace["do_pm_deploy"]

    def __init__(self, cli):
        self.cli, self.verified = cli, False

    def _ssh(self, cfg): return self.cli
    def _pm_compose(self, cfg): return "docker compose -f fixture.yaml"
    def _log(self, *args): pass
    def _line_cb(self, line): pass
    def _verify_pm(self, cli, cfg): self.verified = True


class SchemaDeploymentTests(unittest.TestCase):
    cfg = {"pm_src": "/fixture/source", "pm_data": "/fixture/data", "pm_repo": ""}

    def deploy(self, indexed, fail=""):
        cli = FakeSSH(indexed, fail)
        tool = FakeTool(cli)
        tool.do_pm_deploy(self.cfg)
        return cli, tool

    def test_existing_indexes_still_initialize_schema_before_start(self):
        cli, tool = self.deploy(True)
        self.assertEqual([c.rsplit(" ", 1)[-1] for c in cli.commands], ["build", "schema", "edge"])
        self.assertTrue(tool.verified)
        self.assertTrue(cli.closed)

    def test_missing_indexes_initialize_schema_exactly_once(self):
        cli, tool = self.deploy(False)
        steps = [c.rsplit(" ", 1)[-1] for c in cli.commands]
        self.assertEqual(steps, ["build", "index-pvf", "index-items", "index-quests", "index-professions", "schema", "edge"])
        self.assertTrue(tool.verified)

    def test_failures_never_start_or_verify_services(self):
        for indexed, failure in [(True, " schema"), (False, " schema"), (False, " index-items"), (True, " build")]:
            with self.subTest(indexed=indexed, failure=failure):
                cli = FakeSSH(indexed, failure)
                tool = FakeTool(cli)
                with self.assertRaises(RuntimeError):
                    tool.do_pm_deploy(self.cfg)
                self.assertFalse(any(" up -d " in c for c in cli.commands))
                self.assertFalse(tool.verified)
                self.assertTrue(cli.closed)


if __name__ == "__main__":
    unittest.main()
