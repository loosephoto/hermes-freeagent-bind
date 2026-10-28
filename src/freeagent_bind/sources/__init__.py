"""知識ソースのレジストリ（1 ソース = 1 ファイル）.

`server.py` は共有ヘルパーと横断検索だけを持ち、ソース実装はここから読む。
追加手順: (1) `sources/<name>.py` を作る (2) このファイルの import 一覧に足す
(3) 既定には入れない（規約 28。利用者が `sources=[...]` で明示指定する）。

`register()` は import 順に登録するので、この import 一覧の順がそのまま
`SOURCES` の順（＝横断検索の並び）になる。
"""

REGISTRY: dict = {}
EXPORTS: dict = {}
FUNCTIONS: dict = {}
BACKENDS: dict = {}


def register(name, fn, backend):
    """ソースを登録する。"""
    if name in REGISTRY:
        raise RuntimeError(f"ソース名が重複しています: {name}")
    REGISTRY[name] = {"fn": fn, "backend": backend}
    FUNCTIONS[fn.__name__] = fn
    BACKENDS[name] = backend
    # 分割前の `server.<name>`（定数・専用ヘルパーを含む）を保つ。テストが直接参照する。
    import sys as _sys
    EXPORTS.update(getattr(_sys.modules.get(fn.__module__), "EXPORTS", {}))


from . import wikipedia  # noqa: E402,F401
from . import wikidata  # noqa: E402,F401
from . import arxiv  # noqa: E402,F401
from . import crossref  # noqa: E402,F401
from . import openalex  # noqa: E402,F401
from . import github  # noqa: E402,F401
from . import datacite  # noqa: E402,F401
from . import openaire  # noqa: E402,F401
from . import zenodo  # noqa: E402,F401
from . import ror  # noqa: E402,F401
from . import doaj  # noqa: E402,F401
from . import npm  # noqa: E402,F401
from . import crates  # noqa: E402,F401
from . import cinii  # noqa: E402,F401
from . import osv  # noqa: E402,F401
from . import ietf  # noqa: E402,F401
from . import inspirehep  # noqa: E402,F401
from . import oeis  # noqa: E402,F401
from . import hfhub  # noqa: E402,F401
from . import hn  # noqa: E402,F401
from . import swh  # noqa: E402,F401
from . import librariesio  # noqa: E402,F401
from . import ia  # noqa: E402,F401
from . import qiita  # noqa: E402,F401
from . import openreview  # noqa: E402,F401
from . import mdn  # noqa: E402,F401
from . import devdocs  # noqa: E402,F401

NAMES = tuple(REGISTRY)
