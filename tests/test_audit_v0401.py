"""v0.40.1: `freeagent_lookup` の本文列挙の追従（ネット不要）。

監査（v0.40.0）で見つかった退行を固定する:

`freeagent_lookup` の本文（`description`）の追加ソース列挙が 17/24 で止まり、
cinii / openreview / mdn / devdocs / nvd / terraform / zbmath の 7 名が本文に無かった。
`sources` 引数の説明は 30/30 だったのでモデルの発見性は担保されていたが、
本文の列挙も在庫を全部名指しする（規約 5・20 と同じ型。v0.36.1 の `sources` 説明の
追従テストの本文版）。

同じ監査で見つかったテストの資源閉鎖（ResourceWarning 3 件）は各テストファイルの
修正本体（`test_docs_provider_table.py` の with 化と `test_concurrency_regressions.py`
の cleanup）で担保し、このファイルでは扱わない。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from freeagent_bind import server as S  # noqa: E402


class LookupBodyEnumerationTest(unittest.TestCase):
    """`sources` の説明だけではなく本文の列挙も在庫を全部名指しする。"""

    def test_every_backend_is_named_in_body(self):
        tool = [t for t in S.TOOLS if t["name"] == "freeagent_lookup"][0]
        body = tool["description"].lower()
        missing = [n for n in S.KB_BACKENDS if n not in body]
        self.assertEqual([], missing, f"freeagent_lookup の本文に無いソース: {missing}")


if __name__ == "__main__":
    unittest.main()
