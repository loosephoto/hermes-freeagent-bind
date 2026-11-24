"""Markdown・JSON 処理強化（5 項目）の回帰テスト."""
import json
import os
import tempfile
import unittest
from unittest import mock

import freeagent_bind.server as S


class TestJsonFirstParsers(unittest.TestCase):
    def test_parse_labeled_json_and_fallback(self):
        # JSON 優先（コードフェンス付き・小数 confidence・"なし" 除外）
        raw = '```json\n{"conclusion": "非同期処理が適切", "confidence": 0.85, "question": "なし"}\n```'
        got = S.parse_labeled(raw)
        self.assertEqual(got["conclusion"], "非同期処理が適切")
        self.assertEqual(got["confidence"], 85)
        self.assertEqual(got["question"], "")
        self.assertEqual(got["labels_found"], 3)

        # 途中切断 JSON は行ラベルへ安全にフォールバック
        cut = '{"conclusion": "途中切れ\n結論: 行ラベルで回収\n確信度: 72\nメインに確認したい点: 予算上限'
        fb = S.parse_labeled(cut)
        self.assertEqual(fb["conclusion"], "行ラベルで回収")
        self.assertEqual(fb["confidence"], 72)
        self.assertEqual(fb["question"], "予算上限")

    def test_parse_review_json_prevents_axes_false_positive(self):
        # 本文に "security には問題ない" とあっても、JSON の axes に含まれなければ誤爆しない
        raw = json.dumps({
            "conclusion": "security には問題ないが correctness に懸念あり",
            "severity": "required",
            "axes": ["correctness"],
            "findings": [
                {"axis": "correctness", "location": "L42", "reason": "ゼロ除算の可能性"},
                "[correctness] L88 — 境界値漏れ",
            ],
        }, ensure_ascii=False)
        got = S.parse_review(raw)
        self.assertEqual(got["severity"], "required")
        self.assertEqual(got["axes"], ["correctness"])
        self.assertEqual(len(got["findings"]), 2)
        self.assertIn("[correctness] L42 — ゼロ除算の可能性", got["findings"])

        # JSON に axes が無くても findings からだけ抽出（本文の否定表現に誤爆しない）
        raw2 = json.dumps({
            "conclusion": "security と performance は問題なし",
            "severity": "optional",
            "findings": ["[readability] 変数名が曖昧"],
        }, ensure_ascii=False)
        got2 = S.parse_review(raw2)
        self.assertEqual(got2["axes"], ["readability"])

    def test_parse_compliance_json_and_fallback(self):
        raw = json.dumps({
            "verdict": "partial",
            "findings": ["[要件2] エラー時の再試行が未実装", "[要件3] ログ形式が未一致"],
        }, ensure_ascii=False)
        got = S.parse_compliance(raw)
        self.assertEqual(got["verdict"], "partial")
        self.assertEqual(len(got["findings"]), 2)

        # 語彙外は unknown
        bad = json.dumps({"verdict": "maybe", "findings": []})
        self.assertEqual(S.parse_compliance(bad)["verdict"], "unknown")

    def test_parse_verdict_json_and_fallback(self):
        raw = json.dumps({
            "verdict": "needs_revision",
            "objection": "前提のキャッシュ有効期限が未検証",
            "oversight": "なし",
            "confidence": 82,
        }, ensure_ascii=False)
        got = S.parse_verdict(raw)
        self.assertEqual(got["verdict"], "要修正")
        self.assertEqual(got["objection"], "前提のキャッシュ有効期限が未検証")
        self.assertEqual(got["oversight"], "")
        self.assertEqual(got["confidence"], 82)
        self.assertEqual(got["labels_found"], 4)

    def test_parse_alternatives_json_and_truncated_fallback(self):
        raw = json.dumps({"alternatives": ["案A: キュー分離", "なし", {"text": "案B: キャッシュ事前温め"}]}, ensure_ascii=False)
        self.assertEqual(S.parse_alternatives(raw), ["案A: キュー分離", "案B: キャッシュ事前温め"])

        # 明示的な空配列
        self.assertEqual(S.parse_alternatives('{"alternatives": []}'), [])

        # 途中切断された不完全 JSON + 行形式フォールバック（最終行を捨てる）
        cut = '{"alternatives": [\n代替: 案1\n代替: 途中切れのコマン'
        self.assertEqual(S.parse_alternatives(cut, truncated=True), ["案1"])


class TestMarkdownPrompts(unittest.TestCase):
    def test_consult_prompt_uses_markdown_headings(self):
        p = S._consult_prompt(
            "設計方針は？",
            main_reply="ローカル完結が前提",
            round_no=1,
            peers=[{"model": "m1", "conclusion": "案A"}],
            mode="review",
            draft="def f(): pass",
            basis="仕様書本文",
            stage="compliance",
        )
        for heading in (
            "## 問い",
            "## 査読対象の下書き",
            "## 仕様・計画（これに適合するかを見る）",
            "## メインからの回答（権威ある前提として扱う）",
            "## 他の参加者の回答（第1ラウンド）",
        ):
            self.assertIn(heading, p)

    def test_delegate_verify_prefix_uses_markdown_headings(self):
        self.assertIn("## 完了報告の条件", S.DELEGATE_VERIFY_PREFIX)
        self.assertIn("## 依頼", S.DELEGATE_VERIFY_PREFIX)


class TestHtmlToMarkdown(unittest.TestCase):
    def test_html_to_markdown_preserves_code_and_list(self):
        html = (
            "<script>alert(1)</script>"
            "<p>Run <code>pip install x</code> with:</p>"
            "<ul><li>First item</li><li>Second <code>arg</code></li></ul>"
            "<img src='x.png' alt='noise'/>"
        )
        md = S._html_to_markdown(html, limit=600)
        self.assertNotIn("alert", md)
        self.assertIn("`pip install x`", md)
        self.assertIn("- First item", md)
        self.assertIn("- Second `arg`", md)

    def test_open_metadata_text_markdown_strips_email(self):
        html = "<p>Use <code>foo()</code>. Contact user@example.com for help.</p>"
        got = S._open_metadata_text(html, markdown=True)
        self.assertIn("`foo()`", got)
        self.assertNotIn("@", got)
        # 既定（markdown=False）の既存互換も維持
        self.assertEqual(S._open_metadata_text("<p>Real &amp; <code>&lt;T&gt;</code></p>"), "Real & <T>")


class TestDebugLogJsonl(unittest.TestCase):
    def test_debug_writes_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "debug.jsonl")
            with mock.patch.object(S, "DEBUG_LOG", log_path):
                S._debug("req", {"id": 1, "method": "tools/list"})
                S._debug("big", {"blob": "x" * 6000})
            with open(log_path, encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            second = json.loads(lines[1])
            self.assertEqual(first["kind"], "req")
            self.assertEqual(first["payload"], {"id": 1, "method": "tools/list"})
            self.assertIn("T", first["ts"])
            self.assertTrue(first["ts"].endswith("Z"))
            self.assertEqual(second["kind"], "big")
            self.assertIsInstance(second["payload"], str)
            self.assertLessEqual(len(second["payload"]), 4000)


class TestConditionalMarkdownRendering(unittest.TestCase):
    def test_models_panel_consult_diagnose_markdown_mode(self):
        models_data = {
            "providers": [{"provider": "nous", "ready": True, "models": 10, "free": 5, "error": ""}],
            "free_candidates": 5, "usable_now": 5, "total_models": 10, "default_model": "nous/a|b",
        }
        panel_data = {
            "participants": {"requested": 2, "answered": 2}, "independent_sources": 2,
            "agreement": 0.9, "consensus": [{"models": ["m1", "m2"], "excerpt": "結論 | 改行\nあり"}],
            "refined": {"model": "m3", "conclusion": "統合結果"},
        }
        consult_data = {
            "session_id": "s1", "mode": "discuss", "rounds_run": 1, "stage": "initial",
            "agreement": 0.8, "confidence_mean": 80,
            "consensus": [{"model": "m1", "confidence": 80, "conclusion": "案A"}],
        }
        diag_data = {
            "window": {"days": 7, "since_utc": "2026-11-17", "until_utc": "2026-11-24"},
            "totals": {"calls": 10, "ok": 9, "failed": 1, "ok_rate": 0.9},
            "latency": {"n": 10, "p50": 1.0, "p90": 2.0, "p95": 2.5, "p99": 3.0},
            "slowest_models": [{"model": "m1", "n": 5, "p50": 1.2, "p95": 2.5}],
        }
        # 既定（plain）では Markdown 見出し・表を出さない
        with mock.patch.dict(os.environ, {"FREEAGENT_CONTENT_FORMAT": "plain"}):
            self.assertNotIn("| --- |", S._render_models(models_data))
            self.assertNotIn("| --- |", S._render_panel(panel_data))
            self.assertNotIn("| --- |", S._render_consult(consult_data))
            self.assertNotIn("| --- |", S._render_diagnose(diag_data))

        # markdown 指定時は見出し・表を出力し、パイプと改行をエスケープする
        with mock.patch.dict(os.environ, {"FREEAGENT_CONTENT_FORMAT": "markdown"}):
            m_md = S._render_models(models_data)
            p_md = S._render_panel(panel_data)
            c_md = S._render_consult(consult_data)
            d_md = S._render_diagnose(diag_data)
            for out in (m_md, p_md, c_md, d_md):
                self.assertIn("### ", out)
                self.assertIn("| --- |", out)
            self.assertIn("結論 \\| 改行 あり", p_md)


if __name__ == "__main__":
    unittest.main()
