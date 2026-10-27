"""v0.25.0 の契約: 状態ファイルは**プロセスをまたいでも**壊れない.

何を固定するか（すべて実プロセスを起こして確かめる・ネットワーク不要）:
  * `model_stats.json`: 2 プロセス × 25 件の観測で **50 件が残る**（実測: 旧実装は 2 プロセス × 50 件で
    期待 100 件に対し **50 件**＝片方の記録が丸ごと消える）。
  * `cooldowns.json`: 2 プロセスが別のモデルを冷やすと **両方**が残る（同時に書き込んでも後勝ちしない）。
  * `quota.json`: 上限 `rpd=10` に対して 4 プロセス × 3 回の確保を **合計 10 回で止める**
    （旧実装は各プロセスが別々に数えて上限を超える）。
  * 読み側: 他プロセスが更新した統計を、**プロセスを再起動せずに**取り込む（mtime 検知）。

`tests/test_perf_v0240.py` が「1 プロセス内の見え方」を固定しているのに対し、こちらは
**プロセスの境界**を固定する（同じ `FREEAGENT_STATE_DIR` を stdio と常駐 HTTP が共有する構成）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

import freeagent_bind.server as S  # noqa: E402

_ORIG_STATE = os.environ.get("FREEAGENT_STATE_DIR")


def _run(script: str, state_dir: str, env_extra: dict | None = None, timeout: float = 120):
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": SRC, "FREEAGENT_STATE_DIR": state_dir,
                # 既定の推論・知識 API を触らせない（このテストは状態ファイルだけを見る）
                "FREEAGENT_HARNESS_WARN": "0"})
    env.update(env_extra or {})
    code = "import sys; sys.path.insert(0, r'%s')\n" % SRC + "from freeagent_bind import server as S\n" + script
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                          text=True, timeout=timeout)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-v0250-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(self._restore_env)
        os.environ["FREEAGENT_STATE_DIR"] = self.tmp
        self.addCleanup(self._reset_memory)
        self._reset_memory()

    def _restore_env(self):
        if _ORIG_STATE is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = _ORIG_STATE

    def _reset_memory(self):
        S._STATS["models"] = {}
        S._STATS_LOADED = False
        S._STATS_MTIME = None
        S._COOLDOWN.clear()
        S._COOLDOWN_LOADED = False
        S._COOLDOWN_MTIME = None
        S._QUOTA.clear()
        S._QUOTA_LOADED = False
        S._QUOTA_MTIME = None

    def _spawn(self, script: str, env_extra: dict | None = None):
        env = dict(os.environ)
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": SRC,
                    "FREEAGENT_STATE_DIR": self.tmp, "FREEAGENT_HARNESS_WARN": "0"})
        env.update(env_extra or {})
        code = "import sys; sys.path.insert(0, r'%s')\n" % SRC + \
               "from freeagent_bind import server as S\n" + script
        return subprocess.Popen([sys.executable, "-c", code], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _join(self, procs):
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, f"子プロセスが失敗した:\n{out}\n{err}")


class TestCrossProcessStats(_Base):
    def test_two_processes_do_not_lose_observations(self):
        script = ("for _ in range(25):\n"
                  "    S.note_observation('p/m', 'ask', lat_ms=10.0)\n")
        procs = [self._spawn(script) for _ in range(2)]
        self._join(procs)
        data = json.load(open(os.path.join(self.tmp, "model_stats.json"), encoding="utf-8"))
        row = data["models"]["p/m"]["kinds"]["ask"]
        # 実測（修正前）: 2 プロセス × 50 件で期待 100 件に対し **50 件**しか残らない
        self.assertAlmostEqual(row["n"], 50.0, delta=0.5,
                               msg=f"観測が消えている（n={row['n']}）")
        self.assertAlmostEqual(row["ok"], 50.0, delta=0.5)

    def test_reading_process_sees_another_process_updates(self):
        """読み側は**再起動せずに**他プロセスの学習を取り込む（mtime 検知）。"""
        self.assertIsNone(S.model_mean_latency("p/late"))
        proc = self._spawn("S.note_observation('p/late', 'ask', lat_ms=1234.0)\n")
        self._join([proc])
        # 自分のプロセスは一度も観測していないが、ファイルが変わったので読み直す
        latency = S.model_mean_latency("p/late")
        self.assertIsNotNone(latency,
                             "他プロセスの観測が見えていない（起動時に 1 回しか読んでいない）")
        self.assertAlmostEqual(float(latency or 0.0), 1234.0, delta=1.0)

    def test_concurrent_writers_keep_file_parseable(self):
        script = ("for _ in range(10):\n"
                  "    S.note_observation('p/x', 'ask')\n")
        procs = [self._spawn(script) for _ in range(4)]
        self._join(procs)
        data = json.load(open(os.path.join(self.tmp, "model_stats.json"), encoding="utf-8"))
        self.assertEqual(data.get("version"), 1)
        self.assertAlmostEqual(data["models"]["p/x"]["kinds"]["ask"]["n"], 40.0, delta=1.0)


class TestCrossProcessCooldown(_Base):
    def test_cooldowns_from_two_processes_are_merged(self):
        a = self._spawn("S.note_cooldown('p/a', 60.0, 'HTTP 429')\n")
        b = self._spawn("S.note_cooldown('p/b', 60.0, 'HTTP 404')\n")
        self._join([a, b])
        data = json.load(open(os.path.join(self.tmp, "cooldowns.json"), encoding="utf-8"))
        self.assertEqual(set(data["cooldowns"]), {"p/a", "p/b"},
                         f"他プロセスのクールダウンが消えている: {sorted(data['cooldowns'])}")

    def test_expired_entries_are_dropped_by_merge(self):
        a = self._spawn("S.note_cooldown('p/gone', 1.0, 'x')\n")
        self._join([a])
        time.sleep(1.2)
        b = self._spawn("S.note_cooldown('p/live', 60.0, 'x')\n")
        self._join([b])
        data = json.load(open(os.path.join(self.tmp, "cooldowns.json"), encoding="utf-8"))
        self.assertEqual(set(data["cooldowns"]), {"p/live"})


class TestCrossProcessQuota(_Base):
    def test_limit_is_shared_by_all_processes(self):
        # `quota_record` と同じ順序（確保できたときだけ送ってトークンを足す）
        script = ("for _ in range(3):\n"
                  "    if S.quota_reserve('nous') is None:\n"
                  "        S.quota_add_tokens('nous', 10)\n")
        env = {"FREEAGENT_QUOTA_LIMITS": "nous:rpd=10"}
        procs = [self._spawn(script, env) for _ in range(4)]
        self._join(procs)
        data = json.load(open(os.path.join(self.tmp, "quota.json"), encoding="utf-8"))
        row = data["providers"]["nous"]
        # 4 プロセス × 3 回 = 12 要求のうち、**共有の上限 10** で止まる
        self.assertEqual(int(row["day_req"]), 10,
                         f"上限が合算されていない（day_req={row['day_req']}）")
        self.assertEqual(int(row["day_tok"]), 100, "確保した分のトークンだけが計上されるべき")


class TestFileLockPrimitive(_Base):
    def test_lock_is_exclusive_across_processes(self):
        """ロックの内側で寝ている子プロセスがいる間、親は取れない（timeout で False）。"""
        path = os.path.join(self.tmp, "guard.json")
        holder = self._spawn(
            "import time\n"
            f"with S._file_lock(r'{path}', timeout=5.0) as got:\n"
            "    print('held', got, flush=True)\n"
            "    time.sleep(1.5)\n")
        time.sleep(0.6)                      # 子がロックを取るまで待つ
        start = time.monotonic()
        with S._file_lock(path, timeout=0.3) as got:
            self.assertFalse(got, "同じロックを二重に取れてしまった")
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.2 + 0.6, f"timeout を守っていない（{elapsed:.2f} 秒待った）")
        self._join([holder])

    def test_lock_released_on_process_exit(self):
        """プロセスが落ちても OS がロックを解放する（残骸のロックファイルで永久に待たない）。"""
        path = os.path.join(self.tmp, "crash.json")
        holder = self._spawn(
            f"import os\n"
            f"with S._file_lock(r'{path}', timeout=5.0):\n"
            "    print('held', flush=True)\n"
            "    os._exit(9)\n")
        holder.wait(timeout=60)
        start = time.monotonic()
        with S._file_lock(path, timeout=2.0) as got:
            self.assertTrue(got, "落ちたプロセスのロックが残っている（stale lock）")
        self.assertLess(time.monotonic() - start, 1.0)


if __name__ == "__main__":
    unittest.main()
