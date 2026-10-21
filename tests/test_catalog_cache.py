"""§3.3 モデル一覧の取得（並列・締め切り・プロセス間キャッシュ）の回帰テスト（オフライン）。

監査で実測した値（直列 0.842 秒・冷起動の選抜 0.33 秒）をそのままアサーションにする:
同じ問いを新しいプロセスで聞き直したときに **HTTP を出さない**こと、1 ホストが固まっても
**他が直列で待たない**こと、締め切りを超えた取得は**前回の成功値**で埋めること。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S  # noqa: E402


def _rows(provider: str, n: int = 2) -> list[dict]:
    return [{"id": f"{provider}-model-{i}", "pricing": {"prompt": "0", "completion": "0"}}
            for i in range(n)]


FAKE_SPEC = {
    "base_url": "http://127.0.0.1:1/v1",
    "key": "test-key",
    "key_env": None,
    "free_kind": "pricing",
    "always_ready": True,
    "models_path": "/models",
    "note": "offline test provider",
}


class _CacheCase(unittest.TestCase):
    """状態ディレクトリを一時領域へ向け、モジュールのキャッシュを毎回消す。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-catalog-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._old_state = os.environ.get("FREEAGENT_STATE_DIR")
        os.environ["FREEAGENT_STATE_DIR"] = self.tmp
        self.addCleanup(self._restore_state)
        self.addCleanup(self._reset)
        self._reset()

    def _restore_state(self):
        if self._old_state is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._old_state

    def _reset(self):
        with S._MODELS_LOCK:
            S._MODELS_CACHE.clear()
        with S._MODELS_INFLIGHT_LOCK:
            S._MODELS_INFLIGHT.clear()
        with S._CATALOG_DISK_LOCK:
            S._CATALOG_DISK.clear()
            S._CATALOG_DISK_MTIME = None

    def _specs(self, *names):
        return mock.patch.dict(S.PROVIDER_SPECS, {name: FAKE_SPEC for name in names})

    def _store_stale(self, provider: str, rows: list[dict]) -> None:
        """前回の成功値（TTL を過ぎたもの）をディスクに置く＝「締め切りで取れなかった」状況を作る。"""
        S._catalog_disk_store(provider, rows)
        with S._CATALOG_DISK_LOCK:
            S._CATALOG_DISK[provider]["at"] = S.now_ts() - 3600.0
            stale = {name: dict(row) for name, row in S._CATALOG_DISK.items()}
        S._atomic_write(S.catalog_path(), json.dumps({"version": 1, "providers": stale},
                                                     ensure_ascii=False))
        with S._CATALOG_DISK_LOCK:
            S._CATALOG_DISK_MTIME = None


class TestCatalogDiskCache(_CacheCase):
    def test_second_process_reads_disk_instead_of_http(self):
        """実測の主眼: 新しいプロセス（メモリが空）でもディスクから温まり、HTTP を出さない。"""
        calls = []

        def http(path, provider=None, timeout=None):
            calls.append(provider)
            return {"data": _rows(provider)}

        with self._specs("fakep"), mock.patch.object(S, "provider_http", http):
            first = S.fetch_provider_models("fakep")
            self.assertEqual(len(first), 2)
            self.assertEqual(calls, ["fakep"])
            # プロセスが入れ替わった状況（メモリを落とす）を作る
            with S._MODELS_LOCK:
                S._MODELS_CACHE.clear()
            with S._CATALOG_DISK_LOCK:
                S._CATALOG_DISK.clear()
                S._CATALOG_DISK_MTIME = None
            second = S.fetch_provider_models("fakep")
        self.assertEqual([r["id"] for r in second], [r["id"] for r in first])
        self.assertEqual(calls, ["fakep"], "2 プロセス目は HTTP を出さない")

    def test_disk_cache_file_is_written_only_on_success(self):
        """規約 21 / check_offline: 環境障害では状態を書かない。"""
        def boom(path, provider=None, timeout=None):
            raise OSError("network down")

        with self._specs("fakep"), mock.patch.object(S, "provider_http", boom):
            self.assertEqual(S.fetch_provider_models("fakep"), [])
        self.assertFalse(os.path.exists(S.catalog_path()),
                         "取得に失敗したら状態ディレクトリへ書かない")
        self.assertEqual(sorted(os.listdir(self.tmp)), [])

    def test_ttl_expiry_refetches_from_http(self):
        def http(path, provider=None, timeout=None):
            return {"data": _rows(provider)}

        spy = mock.Mock(side_effect=http)
        with self._specs("fakep"), mock.patch.object(S, "provider_http", spy):
            S.fetch_provider_models("fakep")
            S.fetch_provider_models("fakep", ttl=0)
        self.assertEqual(spy.call_count, 2, "ttl=0 はディスク／メモリを信じない")

    def test_stale_disk_entry_is_not_fresh(self):
        """TTL を過ぎたディスクは「前回値」であって鮮度ではない（HTTP を出す）。"""
        with self._specs("fakep"):
            self._store_stale("fakep", _rows("fakep"))
            rows, fresh = S._catalog_disk_rows("fakep", S.CATALOG_TTL)
            self.assertEqual(len(rows), 2)
            self.assertFalse(fresh)
            rows, fresh = S._catalog_disk_rows("fakep", 0.0)
            self.assertFalse(fresh, "ttl=0 は常に鮮度なし")

    def test_broken_disk_file_is_ignored(self):
        S._atomic_write(S.catalog_path(), "{not json")
        with self._specs("fakep"), mock.patch.object(S, "provider_http",
                                                     lambda *a, **k: {"data": _rows("fakep")}):
            rows = S.fetch_provider_models("fakep")
        self.assertEqual(len(rows), 2)

    def test_cache_can_be_disabled_by_env(self):
        with self._specs("fakep"), \
             mock.patch.dict(os.environ, {"FREEAGENT_CATALOG_CACHE": "0"}), \
             mock.patch.object(S, "provider_http", lambda *a, **k: {"data": _rows("fakep")}):
            S.fetch_provider_models("fakep")
        self.assertFalse(os.path.exists(S.catalog_path()), "無効化したら書かない")


class TestCatalogParallelDeadline(_CacheCase):
    def test_slow_provider_does_not_serialise_the_others(self):
        """1 ホストが固まっても、他プロバイダの結果で先に進める（並列 + 締め切り）。"""
        def http(path, provider=None, timeout=None):
            if provider == "slowp":
                time.sleep(1.5)
            return {"data": _rows(provider)}

        with self._specs("fastp", "slowp"), \
             mock.patch.object(S, "provider_http", http), \
             mock.patch.object(S, "_CATALOG_DEADLINE_S", 0.3):
            start = time.monotonic()
            S._catalog_prefetch(["fastp", "slowp"])
            elapsed = time.monotonic() - start
            fast = S._models_memory("fastp")
            slow = S._models_memory("slowp")
        self.assertLess(elapsed, 1.0, f"締め切りで打ち切る（実測 {elapsed:.2f}s）")
        self.assertEqual(len(fast), 2)
        self.assertIsNone(slow, "間に合っていないものは鮮度なしとして扱う")

    def test_deadline_miss_is_filled_with_the_previous_value(self):
        """締め切り超過は**失敗ではない**ので、前回の成功値で埋める（空にしない）。"""
        def http(path, provider=None, timeout=None):
            if provider == "slowp":
                time.sleep(1.5)
            return {"data": _rows(provider, 3)}

        with self._specs("fastp", "slowp"):
            self._store_stale("slowp", _rows("slowp", 2))     # 前回の成功値
            with mock.patch.object(S, "provider_http", http), \
                 mock.patch.object(S, "PROVIDER_ORDER", ["fastp", "slowp"]), \
                 mock.patch.object(S, "_CATALOG_DEADLINE_S", 0.3):
                start = time.monotonic()
                rows = S.all_models()
                elapsed = time.monotonic() - start
        ids = [r["id"] for r in rows]
        self.assertLess(elapsed, 1.0, f"直列なら 1.5 秒以上かかる（実測 {elapsed:.2f}s）")
        self.assertIn("fastp-model-0", ids)
        self.assertIn("slowp-model-1", ids, "締め切りを超えた分は前回の成功値で埋まる")

    def test_failure_is_not_filled_with_the_previous_value(self):
        """取得の**失敗**は前回値で隠さない（規約 8: 失敗を content / error で出す）。"""
        def boom(path, provider=None, timeout=None):
            raise OSError("network down")

        with self._specs("fakep"):
            self._store_stale("fakep", _rows("fakep"))
            with mock.patch.object(S, "provider_http", boom):
                self.assertEqual(S.fetch_provider_models("fakep"), [])
            with S._MODELS_LOCK:
                self.assertTrue((S._MODELS_CACHE.get("fakep") or {}).get("error"))

    def test_single_flight_shares_one_http_call(self):
        calls = []

        def http(path, provider=None, timeout=None):
            calls.append(provider)
            time.sleep(0.3)
            return {"data": _rows(provider)}

        got = []

        def run():
            got.append(S.fetch_provider_models("fakep"))

        with self._specs("fakep"), mock.patch.object(S, "provider_http", http):
            threads = [threading.Thread(target=run) for _ in range(3)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(5)
        self.assertEqual(len(calls), 1, "同じプロバイダを二重に取りに行かない")
        self.assertTrue(all(len(rows) == 2 for rows in got))
        self.assertEqual(len(got), 3)

    def test_prefetch_skips_providers_that_are_already_fresh(self):
        def http(path, provider=None, timeout=None):
            return {"data": _rows(provider)}

        spy = mock.Mock(side_effect=http)
        with self._specs("fakep"), mock.patch.object(S, "provider_http", spy):
            S._catalog_prefetch(["fakep"])
            S._catalog_prefetch(["fakep"])
        self.assertEqual(spy.call_count, 1)


class TestGroundedSelectionContract(_CacheCase):
    def test_no_selection_when_evidence_is_missing(self):
        """早期返却（根拠 0 件）では**選抜も走らせない**（提供元へ無駄な一覧要求を出さない）。

        選抜を知識取得と重ねる案は実測で採らなかった（§3.3 の注記・`_select_or_error` の呼び出し位置）。
        """
        calls = []
        with mock.patch.object(S, "knowledge_lookup",
                               lambda *a, **k: {"citations": [], "sources": ["wikipedia"],
                                                "errors": {}}), \
             mock.patch.object(S, "_select_or_error",
                               side_effect=lambda *a, **k: calls.append(a)):
            data = S.tool_grounded({"question": "検証"})
        self.assertIn("error", data)
        self.assertEqual(calls, [], "根拠が無いときは選抜を走らせない")

    def test_catalog_is_shared_with_the_next_call(self):
        """早期返却のあと、次の呼び出しはディスク／メモリの一覧で即座に選抜できる（§3.3）。"""
        def http(path, provider=None, timeout=None):
            return {"data": _rows(provider)}

        with self._specs("nous"), mock.patch.object(S, "provider_http", http):
            with mock.patch.object(S, "PROVIDER_ORDER", ["nous"]):
                first = S.free_model_refs(chat_only=True)
                self.assertTrue(first)
                with S._MODELS_LOCK:
                    S._MODELS_CACHE.clear()      # プロセスが入れ替わった状況
                with S._CATALOG_DISK_LOCK:
                    S._CATALOG_DISK.clear()
                    S._CATALOG_DISK_MTIME = None
                spy = mock.Mock(side_effect=http)
                with mock.patch.object(S, "provider_http", spy):
                    second = S.free_model_refs(chat_only=True)
        self.assertEqual(second, first)
        self.assertEqual(spy.call_count, 0, "2 プロセス目は一覧を取りに行かない")


if __name__ == "__main__":
    unittest.main()
