"""v0.36.1: 説明文の列挙の追従と、テストの一時ディレクトリの後始末（ネット不要）。

監査で見つかった 3 型の退行を固定する:

1. `sources` の説明が `KB_BACKENDS` の全部を名指ししているか（mdn / devdocs が 4 版ぶん抜けていた）
2. README の冒頭と接続確認の例のツール数が実装と一致しているか（11 のまま 12 本になっていた）
3. import 時に作る `FREEAGENT_STATE_DIR` がプロセス終了で消えるか（1 回の全実行で 7 個残っていた）
"""

import glob
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from _tmpstate import isolated_state  # noqa: E402
from freeagent_bind import server as S  # noqa: E402


class SourcesDescriptionCoverageTest(unittest.TestCase):
    """`sources` の説明はモデルの唯一の窓口なので、実装の在庫を全部名指しする。"""

    def _desc(self, tool_name):
        tool = [t for t in S.TOOLS if t["name"] == tool_name][0]
        return tool["inputSchema"]["properties"]["sources"]["description"]

    def test_every_backend_is_named(self):
        names = list(S.KB_BACKENDS)
        self.assertGreater(len(names), 0)
        for tool_name in ("freeagent_lookup", "freeagent_grounded"):
            desc = self._desc(tool_name)
            missing = [n for n in names if n not in desc]
            self.assertEqual([], missing, f"{tool_name} の sources 説明に無いソース: {missing}")

    def test_metadata_only_backend_is_described_as_such(self):
        """devdocs は本文を返さない＝grounded の根拠にならない。説明にその制約を書く。"""
        desc = self._desc("freeagent_grounded")
        self.assertIn("devdocs", desc)
        idx = desc.index("devdocs")
        self.assertTrue("本文" in desc[idx:idx + 160] or "索引" in desc[idx:idx + 160],
                        desc[idx:idx + 200])


class ReadmeToolCountTest(unittest.TestCase):
    """ツール数を手で書いた行が実装からずれたら落とす（v0.36.1 まで 2 箇所ずれていた）。"""

    def setUp(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            self.readme = fh.read()

    def test_headline_count(self):
        self.assertIn(f"- {len(S.TOOLS)} 個のツール", self.readme)
        self.assertNotIn(f"- {len(S.TOOLS) - 1} 個のツール", self.readme)

    def test_mcp_test_example_count(self):
        self.assertIn(f"**{len(S.TOOLS)} tools** が出れば", self.readme)
        self.assertNotIn(f"**{len(S.TOOLS) - 1} tools** が出れば", self.readme)


class TempStateCleanupTest(unittest.TestCase):
    """import 時に作る状態ディレクトリは、プロセスが終わったら残らない。"""

    MODULES = [
        "test_audit_v0184",
        "test_audit_v084",
        "test_audit_v086",
        "test_no_output",
        "test_orchestration_v11",
        "test_parallel_budget",
        "test_selection_strategies",
    ]

    def test_helper_keeps_existing_value(self):
        with mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": "/already/set"}):
            self.assertEqual("/already/set", isolated_state("fa-unused-"))

    def test_helper_creates_and_registers(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FREEAGENT_STATE_DIR", None)
            path = isolated_state("fa-hygiene-")
            try:
                self.assertTrue(os.path.isdir(path))
                self.assertEqual(path, os.environ["FREEAGENT_STATE_DIR"])
            finally:
                os.environ.pop("FREEAGENT_STATE_DIR", None)
                __import__("shutil").rmtree(path, ignore_errors=True)

    PREFIXES = (
        "fa-v0184-test-", "fa-v084-test-", "fa-v086-test-", "fa-nooutput-test-",
        "fa-orch-test-", "fa-budget-test-", "fa-select-test-",
    )

    def _leaked(self):
        """子プロセスが作る prefix のディレクトリだけを見る（他のテストの分は無関係）。"""
        out = set()
        for pref in self.PREFIXES:
            out |= set(glob.glob(os.path.join(tempfile.gettempdir(), pref + "*")))
        return out

    def test_imported_modules_leave_nothing(self):
        code = (
            "import os, sys\n"
            f"sys.path.insert(0, {os.path.join(ROOT, 'tests')!r})\n"
            + "".join(f"import {m}\n" for m in self.MODULES)
            + "print(os.environ['FREEAGENT_STATE_DIR'])\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "FREEAGENT_STATE_DIR"}
        env["PYTHONPATH"] = os.path.join(ROOT, "src")
        env["PYTHONIOENCODING"] = "utf-8"
        before = self._leaked()
        proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(0, proc.returncode, proc.stderr[-2000:])
        used = (proc.stdout.strip().splitlines() or [""])[-1]
        self.assertTrue(used, "子プロセスが状態ディレクトリを報告していない")
        self.assertFalse(os.path.exists(used), f"終了後も残っている: {used}")
        self.assertEqual(before, self._leaked(), "一時ディレクトリが増えた")


if __name__ == "__main__":
    unittest.main()
