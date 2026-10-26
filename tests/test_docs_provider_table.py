"""SPEC の「キーと確認変数」表が実装と一致していることを検査する（docs の陳腐化よけ）.

根拠: この表は長らく**一部のプロバイダしか載っておらず**（`doc_drift_check.py` で 15 件の欠落）、
新しい提供元を足すたびに手で並べる前提だった。列挙は実装（`providers/<name>.py` の `SPEC`）から
写し、**足したらテストが落ちる**ようにする。

検査するのは 3 つ:
  1. `PROVIDER_ORDER` の全 18 社の名前が SPEC の表にある
  2. 各社の `key_env`（キー不要の `nous` を除く）が SPEC にある
  3. 各社の `required_env_flags` / `required_env_values` が SPEC にある
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import freeagent_bind.server as S  # noqa: E402

SPEC = open(os.path.join(ROOT, "SPEC.md"), encoding="utf-8").read()
SECTION = SPEC[SPEC.index("### 4.1.2"):SPEC.index("### 4.2")] if "### 4.1.2" in SPEC else ""


class TestKeyTable(unittest.TestCase):
    def test_section_exists(self):
        self.assertTrue(SECTION, "SPEC に §4.1.2（キーと確認変数）が無い")

    def test_every_provider_is_listed(self):
        missing = [name for name in S.PROVIDER_ORDER if f"`{name}`" not in SECTION]
        self.assertEqual(missing, [], f"SPEC §4.1.2 に無いプロバイダ: {missing}")

    def test_every_key_env_is_listed(self):
        missing = []
        for name in S.PROVIDER_ORDER:
            key_env = (S.PROVIDER_SPECS.get(name) or {}).get("key_env")
            if key_env and key_env not in SECTION:
                missing.append(f"{name}:{key_env}")
        self.assertEqual(missing, [], f"SPEC §4.1.2 に無いキー: {missing}")

    def test_every_required_flag_is_listed(self):
        missing = []
        for name in S.PROVIDER_ORDER:
            spec = S.PROVIDER_SPECS.get(name) or {}
            for flag in list(spec.get("required_env_flags") or ()) + \
                    list(spec.get("required_env_values") or ()):
                if flag not in SECTION:
                    missing.append(f"{name}:{flag}")
        self.assertEqual(missing, [], f"SPEC §4.1.2 に無い確認変数: {missing}")

    def test_table_row_count_matches_the_registry(self):
        rows = [ln for ln in SECTION.splitlines()
                if re.match(r"^\|\s*`[a-z0-9]+`\s*\|", ln)]
        self.assertEqual(len(rows), len(S.PROVIDER_ORDER),
                         f"表の行 {len(rows)} 件 / 実装 {len(S.PROVIDER_ORDER)} 社")


if __name__ == "__main__":
    unittest.main()
