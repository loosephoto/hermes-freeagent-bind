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
  8. Linux 常駐ユニットの WorkingDirectory= が引用符で囲まれていない（Issue #10）
  9. macOS launchd plist の起動前検査（ログの親ディレクトリ・絶対パス・KeepAlive）
 10. PATH が OS 別（macOS の定数を Linux ユニットへ流用していない）
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import re
import sys

# 日本語 Windows のコンソール（cp932/cp1252）でも出力を落とさない。CI の windows-latest は
# cp1252 で、print() が UnicodeEncodeError になり **ゲートが落ちる**（実測）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

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
    # **分割後のモジュールも連結して読む**（v0.43.x で HTTP まわりを server.py から出したため、
    # server.py だけ見ると「(connect, read) が無い」と誤判定する・実測 1 件）。
    parts = [inspect.getsource(S)]
    pkg_dir = os.path.dirname(os.path.abspath(S.__file__))
    for name in sorted(os.listdir(pkg_dir)):
        if name.endswith(".py") and name != "server.py":
            with open(os.path.join(pkg_dir, name), encoding="utf-8") as fh:
                parts.append(fh.read())
    src = "\n".join(parts)
    check("timeout=" in src, "外部 HTTP に timeout が指定されていません")
    check(re.search(r"\(\s*\d+(\.\d+)?\s*,\s*\d+(\.\d+)?\s*\)", src) is not None,
          "外部 HTTP に (connect, read) 形式のタイムアウトが見当たりません")

    # おまけ: 描画・ファイル書き込みの副作用が無いこと（読み取り専用ツールの担保）
    tree = ast.parse(src)
    check(not any(isinstance(node, ast.Import) and
                  any(alias.name in {"webbrowser", "tkinter"} for alias in node.names)
                  for node in ast.walk(tree)),
          "GUI を開く import が含まれています（stdio サーバーで開いてはいけません）")

    # 8. Linux systemd --user ユニットの WorkingDirectory= 検査（Issue #10: 引用符があると bad-setting）
    ir_path = os.path.join(ROOT, "scripts", "install_resident.py")
    ir_spec = importlib.util.spec_from_file_location("_check_install_resident", ir_path)
    check(ir_spec is not None and ir_spec.loader is not None, "install_resident.py を読み込めません")
    if ir_spec is not None and ir_spec.loader is not None:
        ir_mod = importlib.util.module_from_spec(ir_spec)
        ir_spec.loader.exec_module(ir_mod)
        for sample_home, sample_repo in (
            ("/home/oresama/.hermes", "/home/oresama/hermes-freeagent-bind"),
            ("/home/alice smith/job%2F1/.hermes", "/home/alice smith/job%2F1/hermes-freeagent-bind"),
        ):
            cfg_linux = ir_mod.build_config(
                sample_home, sample_repo, "127.0.0.1", 8790,
                python="/usr/bin/python3", startup_dir="/tmp/fa-check-units",
                platform_name="linux", assume_host=False,
            )
            for unit_label, rendered in (
                ("hermes-freeagent-bind.service", ir_mod.render_linux_unit(cfg_linux)),
                ("hermes-proxy.service", ir_mod.render_linux_proxy_unit(cfg_linux)),
            ):
                wd_lines = [ln for ln in rendered.splitlines() if ln.startswith("WorkingDirectory=")]
                check(len(wd_lines) == 1, f"{unit_label}: WorkingDirectory= 行が 1 本ではありません ({wd_lines})")
                for wd in wd_lines:
                    val = wd.partition("=")[2]
                    check(not val.startswith(('"', "'")),
                          f"{unit_label}: WorkingDirectory= が引用符で始まっています（systemd bad-setting）: {wd}")
                    check(val.startswith("/"),
                          f"{unit_label}: WorkingDirectory= が絶対パスではありません: {wd}")
                    check(" " not in val,
                          f"{unit_label}: WorkingDirectory= に未エスケープの空白があります: {wd}")

        # 9. macOS launchd plist（v0.51.0・SPEC §6.66）。launchd は `StandardOutPath` /
        #    `StandardErrorPath` を exec の**前**に開き、中間ディレクトリは作らない。親が無いと
        #    ジョブは起動せず `last exit status = 78` で黙って終わる（`bootstrap` の rc は 0 のまま）。
        #    ここでは macOS 実機なしで確かめられる形（パスの組み立て・KeepAlive の意味）を固定する。
        import plistlib as _plistlib

        for sample_home, sample_repo in (
            ("/Users/alice/.hermes", "/Users/alice/hermes-freeagent-bind"),
            ("/Users/alice smith/job%2F1/.hermes", "/Users/alice smith/job%2F1/hermes-freeagent-bind"),
        ):
            cfg_mac = ir_mod.build_config(
                sample_home, sample_repo, "127.0.0.1", 8790,
                python="/opt/homebrew/bin/python3",
                startup_dir="/Users/alice/Library/LaunchAgents",
                platform_name="macos", assume_host=False,
            )
            for plist_label, rendered in (
                ("ai.hermes.freeagent-bind.plist", ir_mod.render_macos_plist(cfg_mac)),
                ("ai.hermes.proxy.plist", ir_mod.render_macos_proxy_plist(cfg_mac)),
            ):
                try:
                    plist = _plistlib.loads(rendered.encode("utf-8"))
                except Exception as exc:  # noqa: BLE001 - 解析できないこと自体を失敗にしたい
                    check(False, f"{plist_label}: plist を解析できません: {exc}")
                    continue
                log_path = str(plist.get("StandardOutPath") or "")
                check(os.path.dirname(log_path) == sample_home + "/logs",
                      f"{plist_label}: StandardOutPath が <hermes-home>/logs の下にありません: {log_path}")
                check(plist.get("StandardErrorPath") == log_path,
                      f"{plist_label}: StandardErrorPath が StandardOutPath と一致しません")
                check(str(plist.get("WorkingDirectory") or "").startswith("/"),
                      f"{plist_label}: WorkingDirectory が絶対パスではありません: {plist.get('WorkingDirectory')}")
                check(plist.get("KeepAlive") == {"SuccessfulExit": False},
                      f"{plist_label}: KeepAlive が SuccessfulExit=False ではありません"
                      "（Restart=on-failure と同義でなくなっています）")
                check(plist.get("RunAtLoad") is True, f"{plist_label}: RunAtLoad がありません")
                prog = (plist.get("ProgramArguments") or [""])[0]
                check(str(prog).startswith("/"),
                      f"{plist_label}: ProgramArguments[0] が絶対パスではありません: {prog}")
                path_env = str((plist.get("EnvironmentVariables") or {}).get("PATH") or "")
                check("/opt/homebrew/bin" in path_env,
                      f"{plist_label}: PATH に /opt/homebrew/bin がありません: {path_env}")
                account_home = sample_home.rsplit("/", 1)[0]
                check(account_home + "/.local/bin" in path_env,
                      f"{plist_label}: PATH に ~/.local/bin がありません"
                      f"（hermes CLI を見つけられません）: {path_env}")

        # 10. PATH は OS 別（macOS の `/opt/homebrew/bin` を Linux ユニットへ流用しない）
        cfg_lin = ir_mod.build_config(
            "/home/alice/.hermes", "/home/alice/hermes-freeagent-bind", "127.0.0.1", 8790,
            python="/usr/bin/python3", startup_dir="/home/alice/.config/systemd/user",
            platform_name="linux", assume_host=False,
        )
        sample_home_pref = str(cfg_lin.get("hermes_home") or "").rstrip("/")
        for unit_label2, rendered2 in (
            ("hermes-freeagent-bind.service", ir_mod.render_linux_unit(cfg_lin)),
            ("hermes-proxy.service", ir_mod.render_linux_proxy_unit(cfg_lin)),
        ):
            # サンプルは `assume_host=False` で組むので、ホストに依らず同じ PATH になる（CI の Linux /
            # macOS でも Windows でも同じ判定）。値そのものをメッセージに出す。
            path_line = next((ln for ln in rendered2.splitlines()
                              if ln.startswith("Environment=") and "PATH=" in ln), "")
            path_value = path_line.split("PATH=", 1)[1].rstrip('"').replace("\\\\", "\\")
            entries2 = [e for e in path_value.split(":") if e]
            check(not any("/opt/homebrew" in e for e in entries2),
                  f"{unit_label2}: Linux ユニットの PATH に macOS の /opt/homebrew が入っています: {path_value}")
            check("/home/alice/.local/bin" in entries2,
                  f"{unit_label2}: PATH に ~/.local/bin がありません（hermes CLI を見つけられません）: {path_value}")
            check("/usr/local/bin" in entries2,
                  f"{unit_label2}: PATH に /usr/local/bin がありません: {path_value}")
            check(sample_home_pref + "/bin" in path_value,
                  f"{unit_label2}: PATH に <hermes-home>/bin がありません: {path_value}")

    if FAILURES:
        print("整合性チェック失敗:")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"整合性チェック OK（tools={len(names)} / version={S.SERVER_VERSION}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())