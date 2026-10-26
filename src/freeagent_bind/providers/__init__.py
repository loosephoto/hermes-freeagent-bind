"""プロバイダ定義のレジストリ（1 社 = 1 ファイル）.

`SPECS` の挿入順がそのまま `PROVIDER_ORDER` の既定順（＝一覧・選抜の巡回順）になる。
追加は (1) `<name>.py` に `SPEC` を書く (2) この import 一覧に足す、の 2 手。
`PROVIDER_ORDER` の既定文字列はここから生成するので、手書きの列挙は持たない。
※ `tests/test_freeagent_bind.py` の登録件数テストは件数を固定しているので同時に直す。
"""

import sys as _sys

SPECS: dict = {}


def register(name, spec):
    """1 社ぶんの SPEC を登録する（import 順を保つ）。"""
    if name in SPECS:
        raise RuntimeError(f"プロバイダ名が重複しています: {name}")
    SPECS[name] = spec


from . import nous  # noqa: E402,F401
from . import openrouter  # noqa: E402,F401
from . import nvidia  # noqa: E402,F401
from . import huggingface  # noqa: E402,F401
from . import groq  # noqa: E402,F401
from . import cloudflare  # noqa: E402,F401
from . import gemini  # noqa: E402,F401
from . import vercel  # noqa: E402,F401
from . import ollama  # noqa: E402,F401
from . import zai  # noqa: E402,F401
from . import mistral  # noqa: E402,F401
from . import modelscope  # noqa: E402,F401
from . import cerebras  # noqa: E402,F401
from . import sambanova  # noqa: E402,F401
from . import novita  # noqa: E402,F401
from . import siliconflow  # noqa: E402,F401
from . import longcat  # noqa: E402,F401
from . import opencode  # noqa: E402,F401


for _name, _mod in list(globals().items()):
    if getattr(_mod, "__name__", "") == f"{__name__}.{_name}":
        register(_name, _mod.SPEC)

NAMES = tuple(SPECS)
