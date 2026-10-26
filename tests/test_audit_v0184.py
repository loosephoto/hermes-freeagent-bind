"""v0.18.4 の監査（2026-10-27）で実測した不都合の回帰テスト。

監査の主張:
1. **部分的な config を渡してもサーバーが黙って切れない**。`_http_make_handler` が
   `config["path"]` を直引きしていたため、`{"host","port"}` だけを渡すと全リクエストが
   `KeyError: 'path'` で **RemoteDisconnected**（実測: 7/7 リクエスト。サーバー側は traceback だけ）。
   `_http_build_server(config)` はテスト用に分けた公開の入口なので、この前提を固定する。
2. **plan から達成済みの項目を外しても、達成の数が消えない**。`plan_progress` は今の plan の
   項目だけを数えるので 1/3 → 0/3 に見えていた（実測）。`plan_retired_done` と描画の注記で明示する。
3. **日本語テキストに日本語でない字が混ざっていない**。`patch` が日本語を壊すことがあり、
   実測で 8 箇所（`察觉` / `读过` / `現在` のハングル / `缓める` / `-mark` / `传到` / `別였다`）が
   出荷直前まで残っていた。cp932 で表せない CJK とハングルを機械的に洗う。
"""
import json
import os
import pathlib
import re
import sys
import tempfile
import threading
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("FREEAGENT_STATE_DIR", tempfile.mkdtemp(prefix="fa-v0184-test-"))
from freeagent_bind import server as S  # noqa: E402


class PartialHttpConfigTest(unittest.TestCase):
    """部分的な config で全リクエストを無言で切らない（実測の再現）。"""

    def test_partial_config_serves_instead_of_disconnecting(self):
        cfg = {"host": "127.0.0.1", "port": 0}          # path を意図的に省く
        httpd = S._http_build_server(cfg)
        self.addCleanup(httpd.server_close)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "0"}}}).encode()
        # 既定パス（/mcp）へ叩く。修正前は RemoteDisconnected になった。
        req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body,
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(200, resp.status)
            payload = json.loads(resp.read().decode("utf-8"))
        self.assertEqual("hermes-freeagent-bind", payload["result"]["serverInfo"]["name"])

    def test_helpers_fall_back_to_defaults(self):
        """**キーが無い**ときは既定へ落ちる（部分的な config を許す）。"""
        self.assertEqual(S.HTTP_DEFAULT_PATH, S._http_path({}))
        self.assertEqual("/mcp", S._http_path({"path": "mcp"}))     # 先頭の / を補う
        self.assertEqual(S.HTTP_DEFAULT_HOST, S._http_host({}))
        self.assertEqual(S.HTTP_DEFAULT_PORT, S._http_port({}))
        self.assertEqual(0, S._http_port({"port": 0}))               # 0 は OS に選ばせる正当な値
        self.assertEqual(S.HTTP_DEFAULT_PORT, S._http_port({"port": None}))

    def test_helpers_reject_explicit_bad_values(self):
        """**明示された不正値は既定へ読み替えない**（要求と違うポートで黙って待ち受けない）。

        実測 2026-10-27: 読み替える実装のせいで `serve_http({"port": "not-a-port"})` が 8765 で
        待ち受けてテストスイートが 51 分ハングした。値が「無い」ことと「不正」を混ぜない。
        """
        for bad in ("not-a-port", 70000, -1, [], True, 8765.7, "8765.0"):
            with self.assertRaises(ValueError, msg=f"port={bad!r} を既定へ読み替えている"):
                S._http_port({"port": bad})
        for bad_host in (123, ["127.0.0.1"]):
            with self.assertRaises(ValueError):
                S._http_host({"host": bad_host})
        for bad_path in (123, b"/mcp"):
            with self.assertRaises(ValueError):
                S._http_path({"path": bad_path})
        with self.assertRaises(SystemExit):
            S.serve_http({"port": "not-a-port"})            # 入口は起動前に止める


class PlanRetiredDoneTest(unittest.TestCase):
    """plan から達成済みの項目を外しても、達成の数が消えない（実測の再現）。"""

    def setUp(self):
        S._THOUGHTS.clear()
        S._THOUGHTS_LOADED = False

    def test_retired_done_is_reported(self):
        sid = S.tool_think({"thought": "A", "plan": ["A", "B", "C"],
                            "subgoal": 1, "subgoal_done": True})["session_id"]
        res = S.tool_think({"thought": "外す", "session_id": sid, "plan": ["B", "C"]})
        led = res["ledger"]
        # 今の plan の進捗は 0/2（達成済みの A は plan に無い）。実測の値をそのまま固定する。
        self.assertEqual({"done": 0, "total": 2}, led["plan_progress"])
        self.assertEqual(1, led["plan_retired_done"], "廃止した達成済みの件数を落としている")
        retired = {p["text"]: p for p in led["plan_retired"]}
        self.assertTrue(retired["A"]["done"], "廃止項目の達成の印が消えている")
        text = "".join(c.get("text", "") for c in S.handle_tool_call(
            {"name": "freeagent_think", "arguments": {"thought": "x", "session_id": sid,
                                                      "view": True}})["content"])
        self.assertIn("廃止した達成済み 1 件は含みません", text,
                      "進捗が減って見える理由が本文に出ていない")

    def test_no_note_when_nothing_retired_done(self):
        sid = S.tool_think({"thought": "A", "plan": ["A", "B"]})["session_id"]
        text = "".join(c.get("text", "") for c in S.handle_tool_call(
            {"name": "freeagent_think", "arguments": {"thought": "x", "session_id": sid,
                                                      "view": True}})["content"])
        self.assertNotIn("廃止した達成済み", text)


class JapaneseTextIntegrityTest(unittest.TestCase):
    """日本語のソースに日本語でない字が混ざっていない（`patch` の破壊の再発防止）。"""

    # 固有名詞（提供元の中国語表記）と、意図して使っている字だけを許す。
    ALLOWED_CHARS = set("熵团动炼")
    ALLOWED_LINE_MARKERS = ("한국어",)          # テストデータとしてのハングル

    def _bad_kind(self, ch):
        c = ord(ch)
        if 0xAC00 <= c <= 0xD7A3 or 0x1100 <= c <= 0x11FF:
            return "ハングル"
        if 0x3400 <= c <= 0x4DBF or 0x4E00 <= c <= 0x9FFF or 0xF900 <= c <= 0xFAFF:
            try:
                ch.encode("cp932")
            except UnicodeEncodeError:
                return "cp932外CJK"
        return None

    def test_no_foreign_script_in_japanese_text(self):
        root = pathlib.Path(ROOT)
        targets = [p for pat in ("src/**/*.py", "tests/**/*.py", "scripts/**/*.py")
                   for p in root.glob(pat) if "__pycache__" not in str(p)]
        targets += [root / n for n in ("README.md", "SPEC.md", "AGENTS.md")]
        # このファイル自身は**壊れた例をそのまま書いている**（何を探すかの仕様）ので除く。
        # 他のファイルに同じ混入が戻ったら落ちる。
        skip = {os.path.abspath(__file__)}
        bad = []
        for path in sorted(set(targets)):
            if os.path.abspath(path) in skip:
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(marker in line for marker in self.ALLOWED_LINE_MARKERS):
                    continue
                hits = sorted({f"{ch}({self._bad_kind(ch)})" for ch in line
                               if self._bad_kind(ch) and ch not in self.ALLOWED_CHARS})
                if hits:
                    bad.append(f"{path.relative_to(root)}:{i} {' '.join(hits)}")
        self.assertEqual([], bad, "日本語テキストに日本語でない字が混ざっている（patch の破壊の疑い）")

    def test_no_patch_artifact_markup(self):
        """`-mark` のような patch の継ぎ跡（利用者に見える文字列）を残さない。"""
        found = []
        root = pathlib.Path(ROOT)
        for path in sorted(root.glob("src/**/*.py")):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"[\u3040-\u30ff]-[a-z]{2,}", line):
                    found.append(f"{path.relative_to(root)}:{i}")
        self.assertEqual([], found, "日本語に ASCII の継ぎ跡が混ざっている")


if __name__ == "__main__":
    unittest.main()
