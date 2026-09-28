"""静的な整合性ゲート（ネットワーク不要）。

CI と手元の両方で同じ判定にするため、テストスイートとは別に**単体で走る**検査を置く。
失敗は exit 1。検査内容:
  1. モジュールが import できる（構文・起動時 import の破綻を検出）
  2. TOOLS と HANDLERS が一致し、名前が重複しない
  3. 全ツールの description に【使う条件】があり、十分な長さがある
  4. inputSchema が object で、required が properties に存在する
  5. pyproject.toml と SERVER_VERSION が一致する
  6. content に LLM 向け指示文が混ざっていない（人間が読むチャネル）
  7. 外部 HTTP に (connect, read) タイムアウトが指定されている
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from freeagent_bind import server as S  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def main() -> int:
    names = [t.get("name") for t in S.TOOLS]

    # 1-2. レジストリの一致
    check(len(names) == len(set(names)), f"ツール名が重複しています: {names}")
    check(set(names) == set(S.HANDLERS),
          f"TOOLS と HANDLERS が不一致: 差分 {set(names) ^ set(S.HANDLERS)}")
    check(all(name.startswith("freeagent_") for name in names),
          "ツール名は freeagent_ で始まる必要があります（名前空間の衝突回避）")

    # 3-4. スキーマと説明
    for tool in S.TOOLS:
        name = tool.get("name")
        desc = tool.get("description") or ""
        check("【使う条件】" in desc, f"{name}: description に【使う条件】がありません")
        check(len(desc) >= 60, f"{name}: description が短すぎます（{len(desc)} 文字）")
        schema = tool.get("inputSchema") or {}
        check(schema.get("type") == "object", f"{name}: inputSchema.type が object ではありません")
        props = schema.get("properties") or {}
        check(isinstance(props, dict), f"{name}: inputSchema.properties が辞書ではありません")
        for key in schema.get("required") or []:
            check(key in props, f"{name}: required の {key} が properties にありません")

    # 5. バージョン一致
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        pyproject = fh.read()
    check(f'version = "{S.SERVER_VERSION}"' in pyproject,
          f"pyproject.toml の version が SERVER_VERSION={S.SERVER_VERSION} と一致しません")

    # 6. content は人間向け（指示文を混ぜない）
    rendered = [S.render(name, {}) for name in S.HANDLERS]
    rendered.append(S.render("freeagent_panel", {"error": "x"}))
    for text in rendered:
        for banned in ("要約せず", "引用してください", "回答時は", "LLMへ", "モデルに対して"):
            check(banned not in text, f"content に指示文「{banned}」が混ざっています")

    # 7. HTTP タイムアウトの指定
    src = inspect.getsource(S)
    check("timeout=" in src, "外部 HTTP に timeout が指定されていません")
    check(re.search(r"\(\s*\d+(\.\d+)?\s*,\s*\d+(\.\d+)?\s*\)", src) is not None,
          "外部 HTTP に (connect, read) 形式のタイムアウトが見当たりません")

    # おまけ: 描画・ファイル書き込みの副作用が無いこと（読み取り専用ツールの担保）
    tree = ast.parse(src)
    check(not any(isinstance(node, ast.Import) and
                  any(alias.name in {"webbrowser", "tkinter"} for alias in node.names)
                  for node in ast.walk(tree)),
          "GUI を開く import が含まれています（stdio サーバーで開いてはいけません）")

    if FAILURES:
        print("整合性チェック失敗:")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"整合性チェック OK（tools={len(names)} / version={S.SERVER_VERSION}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())