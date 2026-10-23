"""`freeagent_panel` の MoA 集約層（`refine`）の回帰テスト（ネットワークを使わない）。

固定する契約:
  * `refine` を付けないと**余分な呼び出しをしない**（後方互換）
  * `refine: true` で統合役を 1 回だけ呼び、`refined` を返す（呼び出し数は 参加数 + 1）
  * 統合役は**参加していないモデル**を優先し、全員参加なら最上位が兼務する（理由を返す）
  * 統合が失敗しても**パネルの結果は返す**（`refined.error` に残す・例外を漏らさない）
  * 統合結果の「結論/確信度」を解析して返す（本文も残す）
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("FREEAGENT_STATE_DIR", os.path.join(tempfile.gettempdir(), "fa-refine-test"))
import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402

REFS = ["nous/a:free", "nous/b:free", "nous/c:free"]
EXTRA = "nous/d:free"


def _answer(text: str) -> dict:
    return {"text": text, "served_by": None, "cot_leak": False, "truncated": False, "tokens": {}}


PANEL = [
    _answer("結論: 391\n確信度: 80"),
    _answer("結論: 391\n確信度: 70"),
    _answer("結論: 392\n確信度: 40"),
]


class TestPanelRefine(unittest.TestCase):
    def _patches(self, refine_result: dict, pool=None):
        return [
            mock.patch.object(S, "free_model_refs", lambda **kw: list(pool or (REFS + [EXTRA]))),
            mock.patch.object(S, "ask_many", lambda refs, prompt, **kw: list(PANEL)),
            mock.patch.object(S, "call_model", lambda ref, prompt, **kw: refine_result),
            mock.patch.object(S, "rank_models", lambda refs: list(refs)),
            mock.patch.object(S, "PROVIDER_GATE", mock.MagicMock(
                report=lambda refs=None: {"per_provider": {}})),
        ]

    def _run(self, args, refine_result, pool=None):
        patches = self._patches(refine_result, pool)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return S.tool_panel({"question": "17*23 は？", **args})

    def test_without_refine_no_extra_call(self):
        calls = []

        def spy(ref, prompt, **kw):
            calls.append(ref)
            return _answer("結論: 391\n確信度: 90")

        with mock.patch.object(S, "free_model_refs", lambda **kw: list(REFS)), \
                mock.patch.object(S, "ask_many", lambda refs, prompt, **kw: list(PANEL)), \
                mock.patch.object(S, "call_model", spy):
            data = S.tool_panel({"question": "q", "models": list(REFS)})
        self.assertNotIn("refined", data)
        self.assertEqual(calls, [], "refine 無しで統合役を呼んだ")
        self.assertEqual(data["answered"], 3)

    def test_refine_calls_one_extra_model(self):
        data = self._run({"models": list(REFS), "refine": True},
                         _answer("結論: 391\n確信度: 95\n根拠: [1][2] を採用"))
        refined = data["refined"]
        self.assertEqual(refined["model"], EXTRA, "参加していないモデルを統合役にしていない")
        self.assertEqual(refined["reason"], "参加していないモデルの最上位")
        self.assertFalse(refined["from_panel"])
        self.assertEqual(data["calls"], len(REFS) + 1)
        self.assertEqual(refined["conclusion"], "391")
        self.assertEqual(refined["confidence"], 95)
        self.assertIn("根拠", refined["text"])

    def test_refine_prefers_outside_panel(self):
        # プールが参加者だけなら兼務になる（理由も変わる）。
        data = self._run({"models": list(REFS), "refine": True},
                         _answer("結論: 391\n確信度: 90"), pool=list(REFS))
        refined = data["refined"]
        self.assertTrue(refined["from_panel"])
        self.assertEqual(refined["model"], REFS[0])
        self.assertEqual(refined["reason"], "全員参加のため最上位が兼務")

    def test_refine_failure_keeps_panel_result(self):
        data = self._run({"models": list(REFS), "refine": True},
                         {"error": "HTTP 429: rate limited", "ref": EXTRA, "kind": "panel_refine"})
        self.assertEqual(data["answered"], 3, "統合が失敗したらパネル結果まで失った")
        self.assertIn("429", data["refined"]["error"])
        self.assertEqual(data["calls"], len(REFS) + 1)
        self.assertEqual(S.render("freeagent_panel", data).count("統合に失敗"), 1)

    def test_explicit_refine_model_wins(self):
        data = self._run({"models": list(REFS), "refine": True, "refine_model": "nous/z:free"},
                         _answer("結論: 391\n確信度: 60"))
        self.assertEqual(data["refined"]["model"], "nous/z:free")
        self.assertEqual(data["refined"]["reason"], "refine_model の指定")

    def test_render_shows_refined_conclusion(self):
        data = self._run({"models": list(REFS), "refine": True},
                         _answer("結論: 391\n確信度: 95"))
        text = S.render("freeagent_panel", data)
        self.assertIn("統合（nous/d:free）", text)
        self.assertIn("391", text)


if __name__ == "__main__":
    unittest.main()
