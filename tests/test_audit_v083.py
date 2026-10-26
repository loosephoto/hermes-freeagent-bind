#!/usr/bin/env python3
"""v0.8.3 の回帰テスト。

監査（2026-10-20）で実測した 3 件の不都合を退行として固定する。

1. **429 で全滅する**（P3）: crossref の 429 は**レート制限ではなく共有プールのTransientな拒否**で、
   間隔（0.4〜1.0 秒）不管で 10 回中 1〜3 回出る。旧実装は 429 を受けると**ホストを 60 秒
   ブロック**して、以後そのホストのソースが全滅した（実測: 連打で 1 件のみ成功）。
   → 429 は**再試行**し（実測で 10 試行すべて成功）、**再試行しても駄目なときだけ**遮断する。

2. **fan-out で脱落する**（P3）: 同じホストのソースが 2 本以上あると、ローカル予算の
   取り合いで片方が落ちる（実測: 成功 1/4）。かつ GitHub の search 枠は
   **repo/issue で共通**（`X-RateLimit-Resource: search`）なのに、予算キーが別だった。
   → 予算キーは search 全体で 1 枠に揃える。

3. **inflight の待ちに上限が無い**（P6-部分）: 同一キーの 2 番目が無制限に待つと、
   取得に 14 秒かかるソースで**利用者を 13.8 秒待たせる**（`KB_DEADLINE`=8 秒の 1.7 倍）。
   → 待ちに上限を置き、上限に達したら待たずに自分で取得する。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from freeagent_bind import server as S  # noqa: E402


class _Resp:
    """`urllib` の最小スタブ。"""

    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self, amt=None):
        data, self._body = self._body[:amt], self._body[amt:]
        return data

    def read1(self, amt=None):
        return self.read(amt)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Test429Retry(unittest.TestCase):
    """429 は再試行する（遮断は最後）。§5.8。"""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fa-v083-429-")
        self.addCleanup(lambda: shutil.rmtree(self.state, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.state})
        patcher.start()
        self.addCleanup(patcher.stop)
        S._KB_BLOCKED.clear()

    def test_429_is_retried_and_succeeds(self):
        """1 回目 429 → 再試行で 200。"""
        calls = []

        def fake(req, budget):
            calls.append(1)
            if len(calls) == 1:
                import urllib.error
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many Requests", {}, None)
            return _Resp(200, b'{"ok":1}')

        with mock.patch.object(S, "_urlopen", fake):
            status, body = S.kb_http("https://example.test/x")
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 2, "429 は再試行されるべき")

    def test_429_is_not_blocked_while_retrying(self):
        """再試行で成功したときは**ホストを遮断しない**（他のソースまで巻き込まない）。"""
        calls = []

        def fake(req, budget):
            calls.append(1)
            if len(calls) == 1:
                import urllib.error
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many Requests", {}, None)
            return _Resp(200, b'{"ok":1}')

        with mock.patch.object(S, "_urlopen", fake):
            S.kb_http("https://example.test/x")
        self.assertNotIn("example.test", S._KB_BLOCKED,
                         "再試行で成功したのにホストを遮断している")

    def test_429_after_retries_is_blocked(self):
        """再試行しても駄目なときだけ遮断する（隠さない）。"""
        import urllib.error

        def fake(req, budget):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

        with mock.patch.object(S, "_urlopen", fake):
            status, _ = S.kb_http("https://example.test/y")
        self.assertEqual(status, 429)
        self.assertIn("example.test", S._KB_BLOCKED, "再試行 亦だめなら遮断する")

    def test_retry_does_not_break_post_bodies(self):
        """再試行で**要求本文が str 化しない**（変数名の衝突による退行の防止）。"""
        import urllib.error
        seen = []

        def fake(req, budget):
            seen.append(req.data)
            if len(seen) == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many Requests", {}, None)
            return _Resp(200, b'{"ok":1}')

        with mock.patch.object(S, "_urlopen", fake):
            status, _ = S.kb_http("https://example.test/z", body=b'{"q":1}', method="POST")
        self.assertEqual(status, 200)
        self.assertTrue(all(isinstance(d, (bytes, type(None))) for d in seen),
                        f"要求本文が bytes でない: {seen}")


class TestSharedBudgetKey(unittest.TestCase):
    """同じホストのソースは**予算を共有**する（fan-out で脱落させない）。§5.11。"""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fa-v083-key-")
        self.addCleanup(lambda: shutil.rmtree(self.state, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.state})
        patcher.start()
        self.addCleanup(patcher.stop)
        # 他のテストが残した枠予約を持ち越さない（= 待ちの判定が壊れる）。
        S._KB_RATE_NEXT.clear()

    def test_repo_and_issue_share_one_search_quota(self):
        """repo と issue は同じ枠を使う（GitHub の `X-RateLimit-Resource: search`）。"""
        seen = []

        def fake(req, budget):
            seen.append(req.full_url)
            return _Resp(200, b'{"items":[]}')

        with mock.patch.object(S, "_urlopen", fake):
            # 1 本目は枠を取れる（通る）。
            S._kb_new_json("https://api.github.com/search/repositories?q=a", 0.2,
                           budget_key="api.github.com/search")
            # 2 本目は同じ枠なので**待つ**（= 枠を共有している）。
            S._KB_LOCAL.deadline_at = time.monotonic() + 5.0
            try:
                S._kb_new_json("https://api.github.com/search/issues?q=b", 0.2,
                               budget_key="api.github.com/search")
            finally:
                S._KB_LOCAL.deadline_at = None
        self.assertEqual(len(seen), 2, "2 本とも通るはず")

    def test_code_search_keeps_its_own_quota(self):
        """code 検索は枠が違うので別キーのまま。"""
        # ソース実装は v0.15.0 で src/freeagent_bind/sources/ へ分割された（Phase 1）。
        # 主張（別の budget_key を持つ）は分割先のファイルで確認する。
        src_dir = os.path.join(ROOT, "src/freeagent_bind")
        src = open(os.path.join(src_dir, "server.py"), encoding="utf-8").read()
        src += open(os.path.join(src_dir, "sources/github.py"), encoding="utf-8").read()
        self.assertIn("api.github.com/search/code", src)


class TestInflightWaitTimeout(unittest.TestCase):
    """inflight の待ちには**上限**がある。§5.8。"""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fa-v083-infl-")
        self.addCleanup(lambda: shutil.rmtree(self.state, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.state})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_wait_is_capped(self):
        """取得が遅いソースで 2 番目が**上限を超えて待たない**。"""
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(6.0)
            return {"source": "x", "items": [], "citations": [], "error": ""}

        owner = threading.Thread(target=lambda: S._kb_cached("cap-key", slow), daemon=True)
        owner.start()
        started.wait()
        time.sleep(0.2)
        t0 = time.time()
        S._kb_cached("cap-key", slow)
        waited = time.time() - t0
        cap = S._KB_INFLIGHT_WAIT_MAX_S
        self.assertLessEqual(waited, cap + 1.5,
                             f"待ちが上限（{cap} 秒）を超えた: {waited:.1f} 秒")
        owner.join(timeout=20)

    def test_cap_default_follows_deadline(self):
        """既定の上限は締め切り（`KB_DEADLINE`）と同じ。"""
        self.assertEqual(S._KB_INFLIGHT_WAIT_MAX_S, S.KB_DEADLINE)


class TestRateWait(unittest.TestCase):
    """予算の待ちには**上限**があり、締め切り外では待たない。§5.11。"""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fa-v083-wait-")
        self.addCleanup(lambda: shutil.rmtree(self.state, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.state})
        patcher.start()
        self.addCleanup(patcher.stop)
        S._KB_RATE_NEXT.clear()

    def test_no_deadline_means_no_wait(self):
        """締め切りが無い直接呼び出しでは待たない（利用者を待たせない）。"""
        S._KB_RATE_NEXT["api.github.com/search"] = time.monotonic() + 60
        t0 = time.time()
        data, err = S._kb_new_json("https://api.github.com/search/repositories?q=x", 6.0,
                                   budget_key="api.github.com/search")
        self.assertIsNone(data)
        self.assertIn("429", err, f"待たずに 429 を返すべき: {err}")
        self.assertLess(time.time() - t0, 1.0, "待たずに返すべき")

    def test_wait_inside_deadline(self):
        """締め切り内に収まるなら待って叩く。"""
        S._KB_RATE_NEXT["api.github.com/search"] = time.monotonic() + 0.5
        S._KB_LOCAL.deadline_at = time.monotonic() + 5.0
        try:
            called = []

            def fake_urlopen(req, budget):
                called.append(1)
                return _Resp(200, b'{"ok":1}')

            with mock.patch.object(S, "_urlopen", fake_urlopen):
                data, err = S._kb_new_json("https://api.github.com/search/y", 6.0)
        finally:
            S._KB_LOCAL.deadline_at = None
        self.assertEqual(err, "", f"待つべきなのに待たなかった: {err}")
        self.assertIsNotNone(data)
        self.assertEqual(len(called), 1, "待ったあと 1 回だけ叩く")


if __name__ == "__main__":
    unittest.main()
