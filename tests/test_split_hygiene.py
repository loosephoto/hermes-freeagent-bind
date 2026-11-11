"""切り出しの衛生チェック（v0.47.0）.

機械分割で実際に混入した 2 種類の欠陥を、再発しないようテストで固定する:

1. **文字列リテラルの中まで `S.` 前置**して、辞書のキーが `"S.confidence_warning"` になった
   （値を読む側は `confidence_warning` を探すので黙って欠落する。テストは通ってしまう）。
2. **モジュール直下で自分の名前を `S.` 経由**にして、読み込み時に `AttributeError`。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "src", "freeagent_bind")


def modules():
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py"):
            with open(os.path.join(PKG, name), encoding="utf-8") as fh:
                yield name, fh.read()


class SplitHygiene(unittest.TestCase):
    def test_no_prefixed_dict_keys(self):
        bad = []
        for name, body in modules():
            for m in re.finditer(r'"(S\.[a-z_][a-z_0-9]*)"', body):
                bad.append(f"{name}: {m.group(1)}")
        self.assertEqual(bad, [], "文字列リテラルの中に S. 前置が残っています")

    def test_no_prefixed_own_names_at_module_level(self):
        bad = []
        for name, body in modules():
            own = set(re.findall(r'^\s{4}"([A-Za-z_][A-Za-z_0-9]*)": \1,$', body, re.M))
            for line in body.splitlines():
                if not line or line[0].isspace() or line.startswith("#"):
                    continue
                for real in own:
                    if f"S.{real}" in line:
                        bad.append(f"{name}: {line.strip()[:60]}")
        self.assertEqual(bad, [], "モジュール直下で自分の名前を S. 経由にしています")

    def test_exports_names_resolve_on_the_module(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "src"))
        import importlib
        missing = []
        for name, body in modules():
            if not re.search(r"^EXPORTS = ", body, re.M):
                continue
            mod = importlib.import_module(f"freeagent_bind.{name[:-3]}")
            missing += [f"{name}:{k}" for k in getattr(mod, "EXPORTS", {}) if not hasattr(mod, k)]
        self.assertEqual(missing, [], "EXPORTS の名前がモジュールに存在しません")


if __name__ == "__main__":
    unittest.main()
