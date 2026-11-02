"""テスト用の状態ディレクトリ（import 時に作る分をプロセス終了時に消す）。

各テストは `FREEAGENT_STATE_DIR` を一時ディレクトリへ向けて本番ストアを汚さないが、
**モジュール読み込み時に `tempfile.mkdtemp(...)` を直接呼ぶと、`addCleanup` を付けられない**
ため空ディレクトリが `%TEMP%` に残る（実測: 1 回の全実行で 7 個・累積 537 個 / 22.4MB）。
ここでは `atexit` で消す。**既に設定済みならディレクトリを作らない**（他のテストが用意した
隔離を壊さない＝従来の `os.environ.setdefault` と同じ意味）。
"""

import atexit
import os
import shutil
import sys
import tempfile
import time

ENV = "FREEAGENT_STATE_DIR"

# このプロセスが作った状態ディレクトリ。**終了時にもう一度消す**（テストの後片付けのあとに
# 遅れて走る書き手が、消したディレクトリを作り直すことがある＝実測: 13 テストで 1 個）。
_REGISTERED = []


def register_state_dir(path: str) -> str:
    if path and path not in _REGISTERED:
        _REGISTERED.append(path)
    return path


@atexit.register
def _cleanup_registered() -> None:
    for path in list(_REGISTERED):
        remove_tree(path)


def remove_tree(path: str, attempts: int = 8, delay: float = 0.1) -> None:
    """テストの後片付けで消す。**1 回で諦めない**。

    Windows では状態ファイルの `.lock` を別スレッドがまだ握っていると `rmtree` が失敗し、
    `ignore_errors=True` では**黙って残る**（実測: `test_audit_v0231` が 1 実行 1 個残した）。
    短い待ちを挟んで数回試す（それでも消えなければ諦める＝テストを落とさない）。
    """
    for _ in range(max(1, attempts)):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(delay)
    # 黙って残さない（何が残ったか分からないと次の実行で迷う）。
    left = sorted(os.listdir(path)) if os.path.isdir(path) else []
    print(f"[tmpstate] 消せませんでした: {path} ({left})", file=sys.stderr)


def isolated_state(prefix: str) -> str:
    """`FREEAGENT_STATE_DIR` が未設定なら一時ディレクトリを作り、終了時に消す。

    戻り値は実際に使うパス（既に設定済みならその値）。
    """
    current = os.environ.get(ENV)
    if current:
        return current
    path = tempfile.mkdtemp(prefix=prefix)
    os.environ[ENV] = path
    register_state_dir(path)
    return path
