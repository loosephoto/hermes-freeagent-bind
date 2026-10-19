#!/usr/bin/env python3
"""v0.8.2 の回帰テスト。

監査（2026-10-20）で実測した 2 件の不都合を退行として固定する。

1. **台帳が複数プロセス間で壊れる**（P1）: `thoughts.json` のロックはプロセス内だけなので、
   別プロセス（MCP 子プロセス = KANBAN ワーカーなど）が同時に書くと last-writer-wins で
   他方の思考が黙って消える。実測（修正前）: 4 プロセスの同時書き込みで 5 試行中 3 回で欠落。
   さらに読み取りの陳腐化（`_ensure_thoughts_loaded` が一度読むと二度と読まない）が起きて、
   別プロセスの追記が常駐プロセスから永久に見えず、親の保存でファイル上から消えた。

2. **`subgoal` の契約不一致**（P2）: schema は「plan の 1 始まり」（= 位置番号）だが
   実装は plan 項目の `id` で照合していた。差し替えで位置と id がずれると、
   位置 1 の項目ではなく**廃止済み項目を完了扱い**にする。実測（修正前）: 20 回反復で 20 回再現。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from freeagent_bind import server as S  # noqa: E402


class TestSubgoalContract(unittest.TestCase):
    """`subgoal` は plan 項目の id で指定する（位置でも受け付ける）。§6.10。"""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fa-v082-sub-")
        self.addCleanup(lambda: shutil.rmtree(self.state, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {
            "FREEAGENT_STATE_DIR": self.state,
            "FREEAGENT_THOUGHTS_PATH": os.path.join(self.state, "thoughts.json"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        S._THOUGHTS.clear()
        S._THOUGHTS_LOADED = False

    def _plan(self, led):
        return {p["text"]: p for p in (led.get("plan") or [])}

    def _retired(self, led):
        return {p["text"]: p for p in (led.get("plan_retired") or [])}

    def test_position_marks_the_live_item_not_the_retired_one(self):
        """差し替え後に subgoal=1（位置）を送ると、**位置 1 の項目**が完了になる。

        修正前は位置 1 の項目が未完了のまま、廃止済み項目が完了扱いになっていた。
        """
        sid = S.tool_think({"thought": "A", "plan": ["A", "B", "C"],
                            "subgoal": 1, "subgoal_done": True})["session_id"]
        S.tool_think({"thought": "差し替え", "session_id": sid, "plan": ["B", "C"]})
        out = S.tool_think({"thought": "位置 1 を完了", "session_id": sid,
                            "subgoal": 1, "subgoal_done": True})
        led = out["ledger"]
        self.assertTrue(self._plan(led)["B"]["done"],
                        "位置 1 の項目が完了になっていない")
        self.assertFalse(self._plan(led)["C"]["done"], "無関係な項目が完了になった")

    def test_retired_item_is_not_marked_done_by_a_live_reference(self):
        """廃止済み項目は**達成済み扱いしない**（記録は残す）。"""
        sid = S.tool_think({"thought": "A", "plan": ["A", "B"],
                            "subgoal": 1, "subgoal_done": True})["session_id"]
        S.tool_think({"thought": "差し替え", "session_id": sid, "plan": ["B"]})
        out = S.tool_think({"thought": "位置 1", "session_id": sid,
                            "subgoal": 1, "subgoal_done": True})
        led = out["ledger"]
        self.assertIn("A", self._retired(led), "廃止済み項目は記録に残す")
        self.assertTrue(self._retired(led)["A"]["done"], "差し替え前に達成済みだった印は保つ")
        self.assertTrue(self._plan(led)["B"]["done"], "位置 1 の B が完了になる")

    def test_id_reference_still_works(self):
        """id を明示すれば、その項目（位置によらず）を完了にする。"""
        sid = S.tool_think({"thought": "計画", "plan": ["A", "B"]})["session_id"]
        out = S.tool_think({"thought": "id を明示", "session_id": sid,
                            "subgoal": 2, "subgoal_done": True})
        self.assertTrue(self._plan(out["ledger"])["B"]["done"])

    def test_unknown_subgoal_is_still_an_error(self):
        sid = S.tool_think({"thought": "計画", "plan": ["A"]})["session_id"]
        res = S.handle_tool_call({"name": "freeagent_think",
                                  "arguments": {"thought": "x", "session_id": sid, "subgoal": 99}})
        self.assertIn("計画にありません", res["structuredContent"]["error"])

    def test_plan_progress_counts_only_live_items(self):
        """`plan_progress` は現在の plan の項目だけで数える（表示と実態を揃える）。"""
        sid = S.tool_think({"thought": "A", "plan": ["A", "B"],
                            "subgoal": 1, "subgoal_done": True})["session_id"]
        out = S.tool_think({"thought": "差し替え", "session_id": sid, "plan": ["B"]})
        self.assertEqual(out["ledger"]["plan_progress"], {"done": 0, "total": 1})

    def test_plan_items_expose_position(self):
        """`plan[].position` は 1 始まりの並び順（`id` は同一性で別）。"""
        sid = S.tool_think({"thought": "計画", "plan": ["A", "B", "C"]})["session_id"]
        out = S.tool_think({"thought": "差し替え", "session_id": sid, "plan": ["B", "C"]})
        plan = out["ledger"]["plan"]
        self.assertEqual([p["position"] for p in plan], [1, 2])
        self.assertEqual([p["id"] for p in plan], [2, 3], "id は同一性で変わらない")


class TestLedgerCrossProcess(unittest.TestCase):
    """台帳のプロセス間競合で思考が消えないこと（P1）。"""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fa-v082-xp-")
        self.addCleanup(lambda: shutil.rmtree(self.state, ignore_errors=True))
        self.thoughts = os.path.join(self.state, "thoughts.json")
        patcher = mock.patch.dict(os.environ, {
            "FREEAGENT_STATE_DIR": self.state,
            "FREEAGENT_THOUGHTS_PATH": self.thoughts,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        S._THOUGHTS.clear()
        S._THOUGHTS_LOADED = False

    def _child(self, script: str, *args: str) -> None:
        env = dict(os.environ)
        env["FAB_SRC"] = os.path.join(ROOT, "src")
        env["FREEAGENT_STATE_DIR"] = self.state
        env["FREEAGENT_THOUGHTS_PATH"] = self.thoughts
        path = os.path.join(self.state, "child.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        proc = subprocess.run([sys.executable, path, *args], env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=180)
        self.assertIn("OK", proc.stdout or "", f"子プロセスが失敗: {proc.stderr[-400:]}")

    def test_concurrent_processes_do_not_lose_steps(self):
        """4 プロセスが同時に同じ台帳へ書いても、ステップが欠落しない。"""
        child = (
            "import os, sys, time\n"
            "sys.path.insert(0, os.environ['FAB_SRC'])\n"
            "from freeagent_bind import server as S\n"
            "t = float(sys.argv[2])\n"
            "while time.time() < t:\n"
            "    time.sleep(0.001)\n"
            "S.tool_think({'thought': 'race-' + sys.argv[1], 'session_id': sys.argv[3]})\n"
            "sys.stdout.write('OK\\n')\n"
        )
        sid = S.tool_think({"thought": "base", "plan": ["p"], "subgoal": 1})["session_id"]
        # 4 プロセスが**同じ時刻**に書き込み（実測の再現手順）。
        start = time.time() + 1.2
        procs = []
        for i in range(4):
            env = dict(os.environ)
            env["FAB_SRC"] = os.path.join(ROOT, "src")
            env["FREEAGENT_STATE_DIR"] = self.state
            env["FREEAGENT_THOUGHTS_PATH"] = self.thoughts
            path = os.path.join(self.state, f"child{i}.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(child)
            procs.append(subprocess.Popen([sys.executable, path, str(i), str(start), sid],
                                          env=env, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL))
        for p in procs:
            p.wait(timeout=180)
        with open(self.thoughts, encoding="utf-8") as fh:
            data = json.load(fh)
        row = (data.get("chains") or {}).get(sid) or {}
        texts = [s.get("text") for s in (row.get("steps") or [])]
        race = [t for t in texts if t and t.startswith("race-")]
        self.assertEqual(len(race), 4,
                         f"同時書き込みで思考が欠落した: {sorted(texts)}")

    def test_other_process_writes_are_visible_after_reload(self):
        """別プロセスの追記は**読み直せば見える**（陳腐化の解消）。"""
        child = (
            "import os, sys\n"
            "sys.path.insert(0, os.environ['FAB_SRC'])\n"
            "from freeagent_bind import server as S\n"
            "S.tool_think({'thought': 'from-other', 'session_id': sys.argv[1]})\n"
            "sys.stdout.write('OK\\n')\n"
        )
        sid = S.tool_think({"thought": "base", "plan": ["p"], "subgoal": 1})["session_id"]
        self._child(child, sid)
        out = S.tool_think({"thought": "", "session_id": sid, "view": True})
        texts = [s.get("text") for s in (out.get("ledger") or {}).get("latest") or []]
        self.assertIn("from-other", texts,
                      "別プロセスの追記が見えない（読み取りの陳腐化）")

    def test_local_save_does_not_erase_other_process_steps(self):
        """自分の台帳を保存しても、ファイル上の他プロセスの手が消えない。"""
        child = (
            "import os, sys\n"
            "sys.path.insert(0, os.environ['FAB_SRC'])\n"
            "from freeagent_bind import server as S\n"
            "S.tool_think({'thought': 'from-other', 'session_id': sys.argv[1]})\n"
            "sys.stdout.write('OK\\n')\n"
        )
        sid = S.tool_think({"thought": "base", "plan": ["p"], "subgoal": 1})["session_id"]
        self._child(child, sid)
        S.tool_think({"thought": "mine", "session_id": sid})
        with open(self.thoughts, encoding="utf-8") as fh:
            data = json.load(fh)
        row = (data.get("chains") or {}).get(sid) or {}
        texts = [s.get("text") for s in (row.get("steps") or [])]
        self.assertIn("from-other", texts, "自分の保存で他プロセスの手が消えた")

    def test_lock_file_is_not_left_in_the_state_dir_when_thoughts_disabled(self):
        """台帳が無効ならロックファイルを作らない（状態ディレクトリを汚さない）。"""
        fresh = tempfile.mkdtemp(prefix="fa-v082-nolock-")
        self.addCleanup(lambda: shutil.rmtree(fresh, ignore_errors=True))
        path = os.path.join(fresh, "thoughts.json")
        with mock.patch.dict(os.environ, {"FREEAGENT_THOUGHTS_PATH": path}), \
                mock.patch.object(S, "THOUGHTS_ENABLED", False):
            S.tool_think({"thought": "x"})
        self.assertEqual([f for f in os.listdir(fresh) if f.endswith(".lock")], [],
                         "台帳が無効なのにロックファイルが残っている")


if __name__ == "__main__":
    unittest.main()
