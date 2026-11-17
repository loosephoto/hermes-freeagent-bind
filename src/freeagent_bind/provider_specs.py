"""§3 プロバイダの仕様とモデル参照の正規化

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import http
import json
import os
import ssl
import subprocess
import threading
import urllib
import urllib.request

from . import server as S  # noqa: E402

def provider_ready(name: str) -> bool:
    return not S._provider_missing_settings(S.PROVIDER_SPECS.get(name) or {})


def make_ref(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def split_ref(ref: str) -> tuple[str, str]:
    """'openrouter/qwen/qwen3.8-27b:free' → ('openrouter', 'qwen/qwen3.8-27b:free')。"""
    ref = S.as_str(ref)
    head, _, rest = ref.partition("/")
    if head in S.PROVIDER_SPECS and rest:
        return head, rest
    return "", ref


class _ShortConnectMixin:
    """接続の間だけ接続上限を使い、接続後は urllib が渡した読み取り上限へ戻す。

    実測（Python 3.11〜3.14）: `urlopen(timeout=…)` の上限は socket に残るため、応答ヘッダを
    受信するまで読み取り上限へ切り替わらない。生成完了までヘッダを返さないローカルプロキシでは、
    生成が `S.CONNECT_TIMEOUT`（既定 10 秒）を超えると `S.READ_TIMEOUT`（既定 180 秒）が使われずに
    `TimeoutError` になった（実測: 1200 トークンの要求が 10.0 秒で失敗）。接続だけを短い上限に
    絞ることで、**不通ホストへの fail fast は保ったまま**長時間の生成を待てる。
    """

    def connect(self):
        read_timeout = self.timeout
        self.timeout = S.CONNECT_TIMEOUT
        try:
            super().connect()  # 接続と TLS ハンドシェイクだけを接続上限で打ち切る
        finally:
            self.timeout = read_timeout
        self.sock.settimeout(read_timeout)


class _ShortConnectHTTPConnection(_ShortConnectMixin, http.client.HTTPConnection):
    pass


class _ShortConnectHTTPSConnection(_ShortConnectMixin, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_ShortConnectHTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        # 版差: 3.13 以前の HTTPSHandler は check_hostname を保持して do_open へ渡すが、
        # 3.14 以降は context だけを渡す（HTTPSConnection も check_hostname を受け付けない）。
        # 実測: 3.14 で `self._check_hostname` を参照すると AttributeError になった。
        kwargs = {"context": self._context}
        check_hostname = getattr(self, "_check_hostname", None)
        if check_hostname is not None:
            kwargs["check_hostname"] = check_hostname
        return self.do_open(_ShortConnectHTTPSConnection, req, **kwargs)


_OPENER = urllib.request.build_opener(_HTTPHandler, _HTTPSHandler)


_SSL_REFRESH_LOCK = threading.Lock()


def _refresh_ssl_context(url: str) -> None:
    """Windows で未訪問のルート CA（例: Starfield Root G2 → export.arxiv.org）がストアに無く
    `CERTIFICATE_VERIFY_FAILED` になったとき、Schannel 経由の HEAD で Windows CryptoAPI の
    自動ルート証明書取得を促し、共有 `S._OPENER` の `SSLContext` を作り直す（検証は無効化しない）。
    """
    with _SSL_REFRESH_LOCK:
        if os.name == "nt" and isinstance(url, str) and url.startswith("https://"):
            curl_bin = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                                    "System32", "curl.exe")
            if os.path.exists(curl_bin):
                try:
                    _nw = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                    subprocess.run([curl_bin, "-sI", "--max-time", "5", url],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=6, check=False, creationflags=_nw)
                except Exception:
                    pass
        ctx = ssl.create_default_context()
        for handler in getattr(S._OPENER, "handlers", ()):
            if isinstance(handler, _HTTPSHandler):
                handler._context = ctx


def _urlopen(req: urllib.request.Request, timeout: float):
    """接続は短い上限で、接続後のヘッダ待ちと本文読取は `timeout` で打ち切る。"""
    try:
        return S._OPENER.open(req, timeout=timeout)
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            S._refresh_ssl_context(getattr(req, "full_url", ""))
            return S._OPENER.open(req, timeout=timeout)
        raise


def provider_http(path: str, provider: str = "nous", payload: dict | None = None,
                  timeout: float | None = None) -> dict:
    spec = S.PROVIDER_SPECS[provider]
    if payload is not None and not S.provider_ready(provider):
        missing = S._provider_missing_settings(spec)
        raise RuntimeError(f"{provider} は未設定/未確認です。必要なenv設定: {', '.join(missing)}")
    url = f"{spec['base_url'].rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {spec['key']}", "Accept": "application/json"}
    data = None
    if payload is not None:
        data = S._dumps(payload).encode("utf-8")     # 孤立サロゲート・NaN を除いた UTF-8（上流が 400 にしない）
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with S._urlopen(req, timeout or S.READ_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as exc:
        # HTML のエラーページ等を 200 で返す上流がある。素の「Expecting value」では原因が分からない。
        raise ValueError(f"上流の応答が JSON ではありません（{type(exc).__name__}）: {S.truncate(body, 80)}") from exc


def _is_hf(provider: str) -> bool:
    """HF（Inference Providers router）か。料金と文脈長が**提供元ごと**という違いを 1 か所で判定する。

    2026-11-16 に HF のルータの中身が OpenRouter になったため分類名を `hf_providers` から
    `hf_credits` へ変えた。**分類名に依存させず** `hf_router` の印を第一に見る（旧名も受ける）。
    """
    spec = S.PROVIDER_SPECS.get(provider) or {}
    if spec.get("hf_router"):
        return True
    return spec.get("free_kind") in ("hf_providers", "hf_credits")


def _free_providers(row: dict) -> list[str]:
    """HF の一覧で**無料で使える提供元**を列挙する（`is_free` か、価格が全部 0 のもの）。

    実測: HF の `/v1/models` は各モデルに `providers[]` を持ち、要素は
    `{provider, status, context_length, pricing:{input,output}, is_free, ...}`。停止中（status が
    live 以外）の提供元は数えない（数えると「無料で使える」と嘘をつくことになる）。
    """
    out: list[str] = []
    for p in (row.get("providers") or []):
        if not isinstance(p, dict) or p.get("status") != "live":
            continue
        name = S.as_str(p.get("provider"))
        if not name:
            continue
        if p.get("is_free") is True:
            out.append(name)
            continue
        pricing = p.get("pricing") or {}
        vals = [float(pricing[k]) for k in ("input", "output")
                if isinstance(pricing.get(k), (int, float))]
        if vals and all(v == 0.0 for v in vals):
            out.append(name)
    return out


_HF_ACCOUNT: dict = {}          # `/api/whoami-v2` の結果（プロセス内キャッシュ）
_HF_ACCOUNT_LOCK = threading.Lock()
_HF_WHOAMI_TTL_S = S._env_float("FREEAGENT_HF_WHOAMI_TTL", 900.0)
_HF_WHOAMI_URL = os.environ.get("FREEAGENT_HF_WHOAMI_URL", "https://huggingface.co/api/whoami-v2")


def hf_credits_enabled() -> bool:
    """`FREEAGENT_HF_CREDITS=1` の申告（利用者がクレジット消費を了承した印）。

    既定は無効。実測（2026-11-17）では HF の一覧に Free の提供元が 1 件も無く、推論は全件 402
    だったので、**申告が無い限り HF を Free 在庫に数えない**（誤って有料を呼ばない）。
    """
    return S._env_on("FREEAGENT_HF_CREDITS", False)


def hf_account_state(refresh: bool = False) -> dict:
    """`/api/whoami-v2` で HF アカウントの課金状態を読む（キャッシュ・失敗しても例外にしない）。

    実測（2026-11-17）: `{"isPro": false, "canPay": false, "billingMode": "prepaid", "periodEnd": …}`。
    クレジット残高を返す API は無い（`/v1/credits` / `/v1/usage` / `/v1/key` はいずれも 404）ので、
    **加入（isPro）と支払い手段（canPay）の有無だけ**が分かる。取得は best-effort（短い期限・無音）。
    """
    spec = S.PROVIDER_SPECS.get("huggingface") or {}
    key = S.as_str(spec.get("key"))
    with _HF_ACCOUNT_LOCK:
        hit = dict(_HF_ACCOUNT) if _HF_ACCOUNT else {}
    if hit and not refresh and S.now_ts() - S.as_float(hit.get("at"), 0.0) < _HF_WHOAMI_TTL_S:
        return hit
    if not key:
        return {"checked": False, "at": S.now_ts(), "error": "HF_TOKEN 未設定"}
    state: dict = {"checked": False, "at": S.now_ts()}
    try:
        req = urllib.request.Request(_HF_WHOAMI_URL, headers={
            "Authorization": f"Bearer {key}", "Accept": "application/json"})
        with S._urlopen(req, timeout=min(8.0, S.READ_TIMEOUT)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(data, dict):
            state.update({
                "checked": True,
                "is_pro": data.get("isPro") is True,
                "can_pay": data.get("canPay") is True,
                "plan": S.as_str(data.get("plan")) or ("pro" if data.get("isPro") is True else ""),
                "billing_mode": S.as_str(data.get("billingMode")),
                "period_end": data.get("periodEnd"),
            })
    except Exception as exc:              # 規約 21: 環境障害で設定を無効化しない（申告だけを信じる）
        state["error"] = f"{type(exc).__name__}: {S.truncate(str(exc), 120)}"
    with _HF_ACCOUNT_LOCK:
        _HF_ACCOUNT.clear()
        _HF_ACCOUNT.update(state)
    return dict(state)


def hf_credits_active() -> bool:
    """HF をクレジット枠として在庫に数えてよいか（申告 + 課金手段の確認）。

    (a) `FREEAGENT_HF_CREDITS=1` が前提。(b) whoami が取れて **isPro も canPay も false** なら
    課金手段が無く 402 にしかならないので数えない（実測: このホストのアカウント）。whoami が
    取れないときは申告だけを信じる（規約 21: 不通で利用者の設定を無効化しない）。
    """
    if not hf_credits_enabled():
        return False
    spec = S.PROVIDER_SPECS.get("huggingface") or {}
    if not S.as_str(spec.get("key")):
        return False                       # トークンが無ければ何も呼べない（在庫にも数えない）
    state = S.hf_account_state()
    if state.get("checked") and not (state.get("is_pro") or state.get("can_pay")):
        return False
    return True


def _is_free(provider: str, row: dict) -> bool:
    """Free 判定。NIM は「無料クレジット枠」なので全モデルが対象、HF は提供元単位、他は pricing が 0。"""
    kind = (S.PROVIDER_SPECS.get(provider) or {}).get("free_kind")
    if kind == "credit":
        return True
    if kind == "allowlist":
        return S.provider_model_allowed(provider, S.as_str(row.get("id")))
    if _is_hf(provider):
        # 2026-11 以降 `is_free` は全件 false（HF の課金は OpenRouter 経由に一本化）。提供元の Free が
        # 戻れば従来どおり拾い、無いときはクレジット枠の申告があるときだけ Free と数える（§4.1）。
        return bool(_free_providers(row)) or S.hf_credits_active()
    if str(row.get("id") or "").endswith(":free"):
        return True
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        return False
    vals = []
    for key in ("prompt", "completion", "input", "output", "request"):
        if key not in pricing or pricing.get(key) is None:
            continue
        try:
            vals.append(float(pricing[key]))
        except (TypeError, ValueError):
            return False
    return bool(vals) and all(v == 0.0 for v in vals)


def _context_length(provider: str, raw: dict):
    """文脈長。HF は**トップレベルに無く提供元ごと**にあるので最大値を採る（実測）。"""
    if _is_hf(provider):
        lens = [p.get("context_length") for p in (raw.get("providers") or [])
                if isinstance(p, dict) and isinstance(p.get("context_length"), (int, float))]
        return int(max(lens)) if lens else None
    return raw.get("context_length") or raw.get("context_window")


EXPORTS = {
    "provider_ready": provider_ready,
    "make_ref": make_ref,
    "split_ref": split_ref,
    "_ShortConnectMixin": _ShortConnectMixin,
    "_ShortConnectHTTPConnection": _ShortConnectHTTPConnection,
    "_ShortConnectHTTPSConnection": _ShortConnectHTTPSConnection,
    "_HTTPHandler": _HTTPHandler,
    "_HTTPSHandler": _HTTPSHandler,
    "_OPENER": _OPENER,
    "_SSL_REFRESH_LOCK": _SSL_REFRESH_LOCK,
    "_refresh_ssl_context": _refresh_ssl_context,
    "_urlopen": _urlopen,
    "provider_http": provider_http,
    "_is_hf": _is_hf,
    "_free_providers": _free_providers,
    "_HF_ACCOUNT": _HF_ACCOUNT,
    "_HF_ACCOUNT_LOCK": _HF_ACCOUNT_LOCK,
    "_HF_WHOAMI_TTL_S": _HF_WHOAMI_TTL_S,
    "_HF_WHOAMI_URL": _HF_WHOAMI_URL,
    "hf_credits_enabled": hf_credits_enabled,
    "hf_account_state": hf_account_state,
    "hf_credits_active": hf_credits_active,
    "_is_free": _is_free,
    "_context_length": _context_length,
}
