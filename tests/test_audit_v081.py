"""v0.8.1 の健全性監査で見つかった 15 件の不都合の回帰テスト。

各テストは「見つかった実測値」を固定する（同じ.with な症状に戻ったら落ちる）。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from freeagent_bind import server as S  # noqa: E402


class TestKbReadWallClockBudget(unittest.TestCase):
    """kb_http の読み取りは**壁時計**で打ち切る（AGENTS 規約 27 / SPEC §6.1）。

    実測の穴: `urlopen(timeout=20)` は「ソケット 1 回の待ち」の上限であってリクエスト全体の
    壁時計上限ではない。1 バイトずつ drip する応答では 98.0 秒（KB_TIMEOUT の 5 倍）かかった。
    """

    def _drip_server(self, delay=0.2, total=200):
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                body = b"x" * total
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for i in range(len(body)):
                        self.wfile.write(body[i:i + 1])
                        self.wfile.flush()
                        time.sleep(delay)
                except Exception:
                    pass

            def log_message(self, format, *args):  # noqa: A002 - 親の API 名
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return port

    def test_drip_response_is_cut_at_the_wall_clock_budget(self):
        port = self._drip_server(delay=0.2, total=200)   # 壁時計で ≈ 40 秒
        started = time.time()
        with mock.patch.object(S, "KB_TIMEOUT", 2.0):
            status, body = S.kb_http(f"http://127.0.0.1:{port}/x")
        elapsed = time.time() - started
        self.assertLess(elapsed, 6.0, f"壁時計予算を超えて待った（{elapsed:.1f} 秒）")
        self.assertEqual(status, 0, "予算超過は失敗として扱う（status 0）")
        self.assertIn("予算", body)

    def test_normal_response_is_read_completely(self):
        port = self._drip_server(delay=0.0, total=64)
        with mock.patch.object(S, "KB_TIMEOUT", 20.0):
            status, body = S.kb_http(f"http://127.0.0.1:{port}/x")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 64, "本文を全部読まなければならない")


class TestHttpOriginAndHostGuard(unittest.TestCase):
    """常駐 HTTP は `Origin` / `Host` を検証する（MCP StreamableHTTP の仕様 MUST）。"""

    def setUp(self):
        cfg = S.http_config(["--transport", "http", "--host", "127.0.0.1", "--port", "0"])
        self.server = S._http_build_server(cfg)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _raw(self, extra_lines):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "1"}}}).encode()
        lines = ["POST /mcp HTTP/1.1",
                 "Content-Type: application/json",
                 "Accept: application/json, text/event-stream",
                 f"Content-Length: {len(body)}",
                 "Connection: close"] + list(extra_lines)
        req = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        s = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        s.sendall(req)
        data = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        except Exception:
            pass
        s.close()
        return data.decode("utf-8", "replace")

    def test_cross_site_origin_is_rejected(self):
        out = self._raw(["Origin: https://evil.example"])
        self.assertIn("403", out.split("\r\n", 1)[0], "他サイトの Origin は 403 で拒否する")

    def test_origin_null_is_rejected(self):
        out = self._raw(["Origin: null"])
        self.assertIn("403", out.split("\r\n", 1)[0], "Origin: null は 403 で拒否する")

    def test_spoofed_host_is_rejected(self):
        out = self._raw(["Host: evil.example"])
        self.assertIn("403", out.split("\r\n", 1)[0], "偽装した Host は 403 で拒否する")

    def test_same_origin_and_cli_are_allowed(self):
        for extra in ([], [f"Origin: http://127.0.0.1:{self.port}"],
                      [f"Origin: http://localhost:{self.port}"],
                      [f"Host: 127.0.0.1:{self.port}"]):
            out = self._raw(extra)
            self.assertIn("200", out.split("\r\n", 1)[0], f"許可すべき要求が弾かれた: {extra}")


class TestThinkPlanMerge(unittest.TestCase):
    """計画の差し替えは**同一性（id）と達成済み**を保つ（§2.7）。"""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fa-plan-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.state})
        patcher.start()
        self.addCleanup(patcher.stop)
        S._THOUGHTS.clear()
        S._ensure_thoughts_loaded()

    def test_replacing_plan_keeps_done_and_id_of_same_text(self):
        sid = S.tool_think({"thought": "A完了", "plan": ["A", "B"],
                            "subgoal": 1, "subgoal_done": True})["session_id"]
        out = S.tool_think({"thought": "差し替え", "session_id": sid, "plan": ["B"]})["ledger"]
        retired = {p["text"]: p for p in out.get("plan_retired") or []}
        self.assertIn("A", retired, "抜けた項目は廃止として残す（黙って消さない）")
        self.assertTrue(retired["A"]["done"], "達成済みの印は残す")
        self.assertEqual(retired["A"]["id"], 1, "同一性は id で保たれる")
        self.assertEqual([p["text"] for p in out["plan"]], ["B"])
        self.assertNotEqual(out["plan"][0]["id"], 1, "別項目の id を再利用しない")

    def test_new_text_gets_an_unused_id(self):
        sid = S.tool_think({"thought": "計画", "plan": ["A", "B"]})["session_id"]
        out = S.tool_think({"thought": "差し替え", "session_id": sid, "plan": ["C"]})["ledger"]
        self.assertEqual(out["plan"][0]["id"], 3, "新しい文面は未使用の id を割り当てる")

    def test_retired_subgoal_is_still_referencable(self):
        """差し替え後も、廃止済み項目の id は subgoal として参照できる（宙に浮かない）。"""
        sid = S.tool_think({"thought": "A完了", "plan": ["A", "B"],
                            "subgoal": 1, "subgoal_done": True})["session_id"]
        S.tool_think({"thought": "差し替え", "session_id": sid, "plan": ["B"]})
        res = S.handle_tool_call({"name": "freeagent_think",
                                  "arguments": {"thought": "A を続ける", "session_id": sid, "subgoal": 1}})
        self.assertNotIn("error", res["structuredContent"],
                         "廃止済み項目への参照が宙に浮いた")

    def test_unknown_subgoal_is_still_an_error(self):
        sid = S.tool_think({"thought": "計画", "plan": ["A"]})["session_id"]
        res = S.handle_tool_call({"name": "freeagent_think",
                                  "arguments": {"thought": "x", "session_id": sid, "subgoal": 99}})
        self.assertIn("計画にありません", res["structuredContent"]["error"])


class TestThinkNeededFlag(unittest.TestCase):
    """`next_thought_needed` の null は「未指定」扱い。終端なら next_call を出さない。"""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fa-need-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.state})
        patcher.start()
        self.addCleanup(patcher.stop)
        S._THOUGHTS.clear()
        S._ensure_thoughts_loaded()

    def test_null_is_treated_as_unspecified(self):
        out = S.tool_think({"thought": "x", "next_thought_needed": None})
        self.assertTrue(out["next_thought_needed"], "null は未指定 → 既定 True")
        self.assertIsNotNone(out.get("next_call"))

    def test_absent_key_is_true(self):
        self.assertTrue(S.tool_think({"thought": "x"})["next_thought_needed"])

    def test_false_stops_the_next_call(self):
        out = S.tool_think({"thought": "終端", "next_thought_needed": False})
        self.assertFalse(out["next_thought_needed"])
        self.assertIsNone(out.get("next_call"), "終端なのに次の思考を誘導しない")

    def test_alias_is_honoured(self):
        self.assertFalse(S.tool_think({"thought": "x", "needs_more_thoughts": False})["next_thought_needed"])
        self.assertTrue(S.tool_think({"thought": "y", "needs_more_thoughts": None})["next_thought_needed"])


class TestLookupShowsUnknownSources(unittest.TestCase):
    """指定したのに使えなかったソース名を content（人間向け）に出さない。"""

    def test_unknown_sources_appear_in_content(self):
        out = S.handle_tool_call({"name": "freeagent_lookup",
                                  "arguments": {"query": "rust", "sources": ["arxiv", "typo_src"]}})
        text = (out.get("content") or [{}])[0].get("text", "")
        self.assertIn("typo_src", text, "未知のソース名が content に出ない")
        self.assertEqual(out["structuredContent"]["unknown_sources"], ["typo_src"])


class TestDescriptionMarkers(unittest.TestCase):
    """description の 3 マーカー（規約 5 / SPEC §7）を全ツールに揃え、重複させない。"""

    MARKERS = ("【使う条件】", "【使わない条件】", "【競合より優先】")

    def test_all_tools_have_all_markers_exactly_once(self):
        for tool in S.TOOLS:
            desc = tool.get("description") or ""
            for marker in self.MARKERS:
                self.assertIn(marker, desc, f"{tool['name']}: {marker} が無い")
                self.assertEqual(desc.count(marker), 1, f"{tool['name']}: {marker} が重複")

    def test_provider_description_lists_every_provider(self):
        tool = next(t for t in S.TOOLS if t["name"] == "freeagent_models")
        desc = tool["inputSchema"]["properties"]["provider"]["description"]
        for provider in S.PROVIDER_ORDER:
            self.assertIn(provider, desc, f"provider 説明に {provider} が無い")

    def test_grounded_sources_description_mentions_question_not_query(self):
        tool = next(t for t in S.TOOLS if t["name"] == "freeagent_grounded")
        desc = tool["inputSchema"]["properties"]["sources"]["description"]
        self.assertIn("question", desc, "grounded の sources 説明は実引数名（question）を書く")
        self.assertNotIn("**query は英語の語**", desc, "実在しない引数名（query）を誘導しない")
        self.assertEqual(tool["inputSchema"]["required"], ["question"])

    def test_lang_description_states_wikidata_is_ja_fixed(self):
        for name in ("freeagent_lookup", "freeagent_grounded"):
            tool = next(t for t in S.TOOLS if t["name"] == name)
            desc = tool["inputSchema"]["properties"]["lang"]["description"]
            self.assertIn("wikidata は既定 ja 固定", desc,
                          f"{name}: lang の説明が実装（wikidata は ja 固定）と乖離")


class TestModelsResponseShape(unittest.TestCase):
    """`freeagent_models` の返り値は**モードでキーを変えない**。"""

    def test_both_keys_are_always_present(self):
        with mock.patch.object(S, "all_models", lambda ttl=600.0: []), \
             mock.patch.object(S, "provider_status", lambda: []), \
             mock.patch.object(S, "free_model_refs", lambda free_only=True, chat_only=False: ["a/b"]):
            searched = S.tool_models({"query": "x"})
            listed = S.tool_models({})
        for key in ("models", "free_models"):
            self.assertIn(key, searched, f"検索モードで {key} が無い")
            self.assertIn(key, listed, f"非検索モードで {key} が無い")
        self.assertEqual(set(searched) - set(listed), {"query"},
                         "返り値の違いは検索の情報だけ")


if __name__ == "__main__":
    unittest.main()
