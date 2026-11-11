"""§4 サブLLM呼び出し（秘密の伏せ字・キャッシュ・進捗・合流・予算）

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import contextlib
import copy
import os
import re
import threading
import time
import urllib

from . import server as S  # noqa: E402

REDACT_SECRETS = S.as_flag(os.environ.get("FREEAGENT_REDACT_SECRETS", "1"))


_SECRET_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[REDACTED:sk]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "[REDACTED:ghp]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED:github_pat]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:slack]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[REDACTED:google]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
     "[REDACTED:jwt]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED:private-key]"),
]


def scrub_secrets(text: str, enabled: bool | None = None) -> tuple[str, int]:
    """秘密を伏せた本文と、伏せた件数を返す。既定は有効（`FREEAGENT_REDACT_SECRETS=0` で無効）。

    上流は外部の無料モデルなので、鍵が混ざったまま送ると取り消しが効かない。
    """
    if enabled is None:
        enabled = REDACT_SECRETS
    if not enabled or not text:
        return text, 0
    total = 0
    for pattern, replacement in _SECRET_PATTERNS:
        text, count = pattern.subn(replacement, text)
        total += count
    return text, total


def _call_once(provider: str, model: str, prompt: str, system: str,
               max_tokens: int, temperature: float | None,
               timeout: float | None = None) -> dict:
    prompt, prompt_redactions = scrub_secrets(prompt)
    system, system_redactions = scrub_secrets(system)
    redactions = prompt_redactions + system_redactions
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        payload["temperature"] = temperature
    started = S.now_ts()
    try:
        chat_path = (S.PROVIDER_SPECS.get(provider) or {}).get("chat_path") or "/chat/completions"
        data = S.provider_http(chat_path, provider=provider, payload=payload, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = ""
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        finally:
            exc.close()
        raise S.HttpStatusError(int(exc.code), body, retry_after)
    latency = S.now_ts() - started
    if not isinstance(data, dict):
        raise ValueError(f"上流の応答が JSON オブジェクトではありません（{type(data).__name__}）")
    text = S._extract_text(data)
    finish = ""
    try:
        finish = (data.get("choices") or [{}])[0].get("finish_reason") or ""
    except Exception:
        finish = ""
    if not isinstance(finish, str):
        finish = ""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return {"text": text, "latency_s": round(latency, 3), "finish_reason": finish,
            "truncated": finish == "length", "redactions": redactions,
            "max_tokens": max_tokens,
            "tokens": {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens")}}


def _candidates(ref: str, free_only: bool = True) -> list[str]:
    """指定先が冷却中でも、冷却外かつ認証可能な Free 代替モデルを並べる。

    予備の並びは**選抜と同じ規則**（`S.diverse_order(S._order_for_selection(...))`）にする。
    以前はカタログ順だったため、実績のあるモデルより「まだ観測の無いカタログ前半のモデル」を
    先に試し、外れるたびに 1 回ぶんの失敗待ちを払っていた（実測: スコア 0.95 の実績モデルが
    0.2 の未検証モデルの後ろに並んだ）。
    """
    cooling = S.cooling_refs()
    out = [ref] if ref and ref not in cooling else []
    if len(out) >= S._MAX_ATTEMPTS:
        return out[:S._MAX_ATTEMPTS]
    blocked = {name for name in S.PROVIDER_ORDER if S.provider_auth_blocked(name)}
    ex_set = set(S.env_excluded_models())
    pool = [other for other in S.free_model_refs(free_only=free_only, chat_only=True)
            if other != ref and other not in cooling and other not in out
            and not S._is_model_excluded(other, ex_set)
            and other.split("/", 1)[0] not in blocked]
    for other in S.diverse_order(S._order_for_selection(pool)):
        out.append(other)
        if len(out) >= S._MAX_ATTEMPTS:
            break
    return out


def _is_auth_error(status: int, body: str) -> bool:
    """401/403 が**認証の失敗**なのかを署名で判定する。

    実測: 403 は認証だけでは起きない。HF は権限不足（`does not have sufficient permissions to call
    Inference Providers`）だが、同じ HF の `prism-ml/…:together` は **Cloudflare の 403** を返し、
    OpenRouter の `:free` は「このモデルは使えない」の 403 を返す。これらを認証失敗として扱うと
    **プロバイダ全体を 15 分ブロックしてしまう**（生きている他モデルまで選抜から消える）。
    """
    if status == 401:
        return True          # 401 は定義上ずっと認証（未認証・無効トークン）
    low = (body or "").lower()
    return any(sign in low for sign in (
        "insufficient permissions", "authentication method", "unauthorized",
        "authorization failed", "authorization missing", "no cookie auth",
        "invalid api key", "invalid_api_key", "incorrect api key", "not authorized",
    ))


def _auth_hint(provider: str, status: int, body: str) -> str:
    """認証エラーは**原因と直し方**を返す（「すべての候補で失敗しました」だけでは直しようがない）。

    実測: HF の既存トークンは有効でも `403 This authentication method does not have sufficient
    permissions to call Inference Providers` を返す（fine-grained トークンに推論権限が無い）。
    """
    tail = S.truncate((body or "").replace("\n", " "), 160)
    if status == 402:
        if provider == "cloudflare":
            return ("HTTP 402（cloudflare）: Workers AIのFree割当/アカウント制限の可能性があります。"
                    "Workers Freeでは日次枠超過後に処理が停止し、Workers Paidでは超過分が課金されます。"
                    f"契約状態を確認してください / 応答: {tail}")
        if provider == "groq":
            return ("HTTP 402（groq）: Free tierの状態またはアカウント利用制限を確認してください。"
                    "Developer tierは従量課金です。"
                    f" / 応答: {tail}")
        if provider == "gemini":
            return ("HTTP 402（gemini）: Free tierのプロジェクト割当またはBilling設定を確認してください。"
                    "Billing有効時は課金が発生する場合があります。"
                    f" / 応答: {tail}")
        # クレジット枯渇。キーや権限の問題ではないので取り違えさせない（実測: HF の月次無料枠は
        # 生存確認やパネルを繰り返すと尽き、全モデルが 402 になる）。翌月に回復するため
        # **プロバイダ記憶（15 分）には入れない**。
        return (f"HTTP 402（{provider}）: クレジット枯渇（キーや権限の問題ではありません）。"
                f"別のプロバイダへ回します / 応答: {tail}")
    if provider == "huggingface":
        if "inference providers" in (body or "").lower():
            return ("HTTP 403（huggingface）: トークンに Inference Providers の権限がありません。"
                    "https://huggingface.co/settings/tokens で「Make calls to Inference Providers」を"
                    "有効にしたトークンを作り、env の HF_TOKEN に設定してください"
                    f" / 応答: {tail}")
        if status == 401:
            return ("HTTP 401（huggingface）: HF_TOKEN が未設定か無効です。"
                    f"Inference Providers の権限があるトークンを設定してください / 応答: {tail}")
        # 403 でも権限の文言が無い = 認証ではなく**提供元/CDN 側**の拒否（実測: Together 経由が
        # Cloudflare Error 1010 "Access denied" を返す）。トークンを疑わせない。
        return ("HTTP 403（huggingface）: トークンの権限ではなく**提供元側**で拒否されました"
                "（Cloudflare のブロックや提供元の障害）。`model:提供元` で別の提供元を試してください"
                f" / 応答: {tail}")
    hints = {
        "groq": ("GROQ_API_KEY が未設定か無効です" if status == 401 else
                 "Free対象モデル一覧とアカウントのモデル権限を確認してください"),
        "cloudflare": ("CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID と Workers AI Read 権限を確認してください" if status == 401 else
                       "Freeプラン・モデル利用権限を確認してください。Workers Paidへのアップグレードで超過利用が課金されます"),
        "gemini": ("GEMINI_API_KEY（または GOOGLE_API_KEY）とFree tierプロジェクトを確認してください" if status == 401 else
                   "Freeモデルの利用可否・プロジェクト割当・地域制限を確認してください"),
        "openrouter": ("OPENROUTER_API_KEY が未設定か無効です" if status == 401 else
                       "キーは有効ですが、このモデルを使う権限・プランがありません"
                       "（`:free` でも提供元側の制限で 403 になるものがあります）"),
        "nvidia": ("NVIDIA_API_KEY が未設定か無効です" if status == 401 else
                   "このモデルはアカウントで有効化されていません（一覧に出ても呼べないものがあります）"),
    }
    hint = hints.get(provider, "キーまたは権限を確認してください")
    return f"HTTP {status}（{provider}）: {hint} / 応答: {tail}"


def _call_model_uncached(ref: str, prompt: str, *, system: str = "", max_tokens: int = 800,
                         temperature: float | None = None, kind: str = "ask",
                         allow_fallback: bool = True, free_only: bool = True,
                         timeout: float | None = None, claims: "_ModelClaims | None" = None) -> dict:
    """1 つのサブLLM呼び出し。**例外を外へ漏らさず**、失敗も dict で返す。

    `claims` を渡すと、フォールバック先に**同じ呼び出しの他の枠が使っている／除外されたモデル**を
    選ばない（`ask_many` が渡す）。渡さないと 2 枠が同じモデルで埋まり「独立 2 体」の表示が
    実質 1 体になる（実測: 代替案の提案者の枠を、同じ呼び出しの検証者と同じモデルが埋めた）。
    """
    provider, model = S.resolve_ref(ref, free_only=free_only)
    if not model:
        return {"error": "モデルが解決できませんでした（Free モデルが 0 件の可能性）",
                "ref": ref, "kind": kind}
    spec = S.PROVIDER_SPECS.get(provider) or {}
    if spec.get("free_model_ids") is not None and not S.provider_model_allowed(provider, model):
        return {"error": f"{provider}/{model} はこのFree-tier設定で許可されていないモデルです",
                "ref": ref, "kind": kind}
    if free_only and S._is_known_paid_model(provider, model):
        return {"error": f"{provider}/{model} は有料モデルのため呼び出しを拒否しました（Free モデルのみ許可）",
                "ref": ref, "kind": kind}
    if spec.get("catalog_requires_ready") and not S.provider_ready(provider):
        missing = S._provider_missing_settings(spec)
        return {"error": f"{provider} は未設定/未確認です。必要なenv設定: {', '.join(missing)}",
                "ref": ref, "kind": kind}
    ref = S.make_ref(provider, model)
    cooling = S.cooling_refs()
    attempts = S._candidates(ref, free_only=free_only) if allow_fallback else (
        [ref] if ref not in cooling else [])
    if not attempts:
        cool_reason = S.as_str((cooling.get(ref) or {}).get("reason"))
        return {"error": f"全候補がクールダウン中です（{ref}）", "ref": ref, "kind": kind,
                "cooling_reason": cool_reason or None,
                "skipped_cooling": [ref], "rate_limited": bool(ref in cooling)}

    last_error = ""
    last_error_ref = ref
    last_observed_error = ""
    quota_warnings: list[str] = []
    skipped: list[str] = []
    avoided: list[str] = []
    # 提供元の枠を待つ予算は **1 回の `call_model` で共有する**。候補ごとに満額待つと、枠が
    # 全部埋まっているときに「候補の数 × GATE_WAIT_S」待ってしまう（規約 27 を推論側にも適用）。
    wait_left = S.GATE_WAIT_S
    gate_wait = 0.0

    def note_failure(cand: str, message: str, result: dict | None = None) -> None:
        """**フォールバックで落ちた候補**を統計へ残す（成功した候補だけ記録してはいけない）。

        成功時だけ `S.observe_call` を呼ぶと、毎回 5xx / 404 で落ちるモデルは観測 0 件のまま
        ＝`_rank_score` の探索ボーナス（`0.25·sd`）が最大で残り、**毎回いちばん最初に選ばれて
        1 回ぶんの失敗待ちを発生させ続ける**（実測: `nous/meituan/longcat-2.0:free` が
        `unobserved` のまま第 1 候補に居座り、毎回 `laguna-s-2.1` へフォールバックした）。
        環境障害（`S.is_env_failure`）はモデルの成績ではないので `S.observe_call` が no-op にする。
        """
        nonlocal last_observed_error
        if not message:
            return
        row = {"ref": cand, "error": message}
        if isinstance(result, dict):
            # 空応答でも finish_reason と消費 tokens は記録する（length 枯渇とそれ以外を区別する）。
            row.update({"truncated": bool(result.get("truncated")),
                        "tokens": result.get("tokens") or {},
                        "finish_reason": S.as_str(result.get("finish_reason")),
                        "max_tokens": S.as_int(result.get("max_tokens"), 0, 0, 10 ** 9) or None,
                        "retries": S.as_int(result.get("retries"), 0, 0, 100)})
        S.observe_call(row, kind, "")
        last_observed_error = message

    for idx, cand in enumerate(attempts):
        if claims is not None and ((cand == ref and claims.is_excluded(cand))
                                   or (cand != ref and not claims.take(cand))):
            avoided.append(cand)   # 他の枠が使用中／除外済み。独立性を守るため取らない
            continue
        c_provider, c_model = S.resolve_ref(cand, free_only=free_only)
        if not c_model:
            continue
        last_error_ref = cand
        try:
            result, waited = S._gated_call_once(c_provider, c_model, prompt, system, max_tokens,
                                              temperature, timeout=timeout, wait_budget=wait_left)
            wait_left = max(0.0, wait_left - waited)
            gate_wait += waited
        except S.ProviderBusyError as exc:
            # **429 を踏む代わりに待って諦めた**ケース。隠さず脱落として返し、以後は待たない。
            last_error = str(exc)
            wait_left = 0.0
            skipped.append(cand)
            S._debug("provider_busy", {"ref": cand})
            continue
        except S.QuotaExhaustedError as exc:
            # 宣言された上限に達した（§3.5）。**待たない**が、別の提供元の候補は試す。
            last_error = str(exc)
            skipped.append(cand)
            S._debug("quota_exhausted", {"ref": cand})
            continue
        except S.HttpStatusError as exc:
            last_error = f"HTTP {exc.status}: {exc.body[:200]}"
            if exc.status == 429:
                until = S.note_rate_limited(cand, exc.retry_after)
                was, now = S.PROVIDER_GATE.note_rate_limited(c_provider)
                if now < was:
                    S._debug("gate_reduced", {"provider": c_provider, "from": was, "to": now})
                skipped.append(cand)
                S._debug("rate_limited", {"ref": cand, "until": until})
            elif exc.status in (404, 410):
                S.note_unavailable(cand, exc.status)
                skipped.append(cand)
            elif exc.status == 402:
                # クレジット枯渇はプロバイダ全体の認証障害ではないが、同一モデルへの
                # 連続要求を避けるため短時間だけモデル単位で休ませる。
                S.note_cooldown(cand, S._COOLDOWN_DEFAULT_S, "HTTP 402 (credit depleted)")
                skipped.append(cand)
                last_error = _auth_hint(c_provider, exc.status, exc.body)
                S._debug("credit_exhausted", {"ref": cand})
            elif exc.status in (401, 403):
                # 原因を残し、**認証の署名があるときだけ**プロバイダ単位で覚える。
                # 提供元都合の 403（モデル単位の制限・CDN のエラー）でプロバイダ全体を止めないため。
                last_error = _auth_hint(c_provider, exc.status, exc.body)
                if _is_auth_error(exc.status, exc.body):
                    S.note_provider_auth(c_provider, exc.status, exc.body or last_error)
                S._debug("auth_error", {"ref": cand, "status": exc.status})
            # 落ちた候補を成績に残す（429 は提供元の一時制限なので除く）。
            if exc.status in S._MODEL_FAULT_STATUS:
                note_failure(cand, last_error)
            if exc.status not in S._FALLBACK_STATUS:
                break
            continue
        except Exception as exc:  # 接続不可・タイムアウト・JSON 壊れ
            last_error = f"{type(exc).__name__}: {exc}"
            if S.is_env_failure(last_error):
                break  # 環境障害では別モデルも同じ経路で失敗するため再試行を打ち切る
            note_failure(cand, last_error)   # 環境障害ではない（JSON 壊れ等）＝モデル側の失敗
            continue

        if result.get("quota_accounting"):
            quota_warnings.append(result["quota_accounting"])
        text = result["text"]
        if not text.strip():
            # 空応答。**成功として返さない**（メインLLMが「無回答」を回答と誤解する）。思考トークンで
            # 予算を使い切った可能性が高いので、予算を上げて 1 回だけ同じ候補で引き上げる。
            retry_note = ""
            bumped = max_tokens
            # **引き上げ再試行は「length 枯渇（または不明）」のときだけ**（v0.41.0）。
            # finish_reason が stop / content_filter 等の確定値で本文が空のとき、予算を増やしても
            # 空のままだった（本番 traces 実測: 失敗の大半がこの型で、再試行 1 回ぶんの待ちと
            # 枠を無駄にしていた）。枯渇が疑えるとき（truncated / length / 不明）だけ上げる。
            _finish = S.as_str(result.get("finish_reason")) if isinstance(result, dict) else ""
            _length_starved = bool(result.get("truncated")) or _finish in ("", "length")
            if max_tokens < S._EMPTY_RETRY_CAP and _length_starved:
                bumped = min(max(max_tokens * 3, S._EMPTY_TOKEN_FLOOR), S._EMPTY_RETRY_CAP)
                try:
                    retry, retry_wait = S._gated_call_once(c_provider, c_model, prompt, system,
                                                         bumped, temperature, timeout=timeout,
                                                         wait_budget=wait_left)
                    wait_left = max(0.0, wait_left - retry_wait)
                    gate_wait += retry_wait
                    if retry.get("quota_accounting"):
                        quota_warnings.append(retry["quota_accounting"])
                    if (retry.get("text") or "").strip():
                        result, text = retry, retry["text"]
                except Exception as exc:
                    # **リトライの失敗を「空応答」に潰さない**（v0.40.3）。429・環境障害は
                    # モデルの成績ではないので observe_call に流さない（規約 21）。
                    if isinstance(exc, S.HttpStatusError):
                        retry_note = f"HTTP {exc.status}: {(exc.body or '')[:120]}"
                        if exc.status == 429:
                            S.note_rate_limited(cand, exc.retry_after)
                    else:
                        retry_note = f"{type(exc).__name__}: {exc}"
            if not text.strip():
                if retry_note:
                    if S.is_env_failure(retry_note):
                        # 環境障害: 別候補も同じ経路で失敗するので打ち切る（モデルの成績に数えない）。
                        last_error = f"再試行（max_tokens={bumped}）でも環境障害: {retry_note}"
                        break
                    # **「空応答」で始めない**: classify_error は語を先に見るので、429 を含む文を
                    # 空応答（no_output）に誤分類させる（v0.40.3）。
                    last_error = f"再試行（max_tokens={bumped}）でも失敗: {retry_note}"
                    if "429" not in retry_note:
                        note_failure(cand, last_error)
                    skipped.append(cand)
                    continue
                finish = S.as_str(result.get("finish_reason")) if isinstance(result, dict) else ""
                why = "（finish_reason=length: 思考トークンで予算を使い切った）" if finish == "length" else ""
                tried = f"・再試行 {bumped} も空" if max_tokens < S._EMPTY_RETRY_CAP else ""
                last_error = (f"空応答（{cand}）。max_tokens={max_tokens}{tried} を思考トークンで"
                              f"使い切った可能性があります{why}。max_tokens を増やすか、"
                              "FREEAGENT_MODEL_EXCLUDE で外してください")
                # 失敗行にも **どの max_tokens で・何回目で** 空だったかを残す（v0.42.1）。
                _fail_meta = dict(result) if isinstance(result, dict) else {}
                _fail_meta.update({"max_tokens": bumped,
                                   "retries": 1 if max_tokens < S._EMPTY_RETRY_CAP else 0})
                note_failure(cand, last_error, _fail_meta)
                skipped.append(cand)
                continue

        # `ref` は**呼び出し側が要求したモデル**、`served_by` が実際に答えたモデル。
        # `ref` を cand で上書きすると `tool_ask` の「要求 → 代替へ回った」の表示が消える（実測）。
        out = {"ref": ref, "served_by": cand, "model": c_model, "provider": c_provider,
               "text": text, "latency_s": result["latency_s"],
               "truncated": result["truncated"], "tokens": result["tokens"],
               "max_tokens": result.get("max_tokens"),
               "redactions": result.get("redactions", 0),
               "cot_leak": S._cot_leak(text), "kind": kind,
               "gate_wait_s": round(gate_wait, 3) if gate_wait else None,
               "fallback": cand != ref, "skipped_cooling": skipped or None}
        if quota_warnings:
            out["quota_accounting"] = "; ".join(quota_warnings)
        S.PROVIDER_GATE.note_success(c_provider)   # 枠が縮んでいたら 1 本ずつ戻す
        S.clear_provider_auth(c_provider)   # 通ったら「認証で駄目」の記憶を消す
        # 統計・トレースは**答えたモデル**に付ける（要求元に付けると、代替で答えた事実が消える）。
        S.observe_call(dict(out, ref=cand), kind, text)
        return out

    fail = {"error": last_error or "すべての候補で失敗しました", "ref": ref, "kind": kind,
            "skipped_cooling": skipped or None,
            "rate_limited": any(r in S.cooling_refs() for r in attempts)}
    if quota_warnings:
        fail["quota_accounting"] = "; ".join(quota_warnings)
    if avoided and not last_error:
        fail["error"] = ("フォールバック先がすべて同じ呼び出しの他の枠で使用中か除外済みでした"
                         "（独立性を守るため同じモデルで枠を埋めていません）")
    if avoided:
        fail["avoided_duplicates"] = len(avoided)
    if fail["error"] != last_observed_error:
        S.observe_call(dict(fail, ref=last_error_ref), kind, "")
    return fail


class _ModelClaims:
    """1 回の並列呼び出し（`ask_many`）の中で**使用中のモデル**を記録する（フォールバックの重複防止）。

    各枠の本来のモデルと除外モデルを最初から「使用中」にしておき、フォールバックで新たに取る
    モデルはロック内で 1 回だけ確保できる（2 枠が同時に同じ予備モデルへ落ちるのを防ぐ）。
    """

    def __init__(self, refs, avoid=(), identity_avoid=()):
        self._lock = threading.Lock()
        self._excluded = {r for r in (avoid or ()) if isinstance(r, str) and r}
        self._identity_excluded = {S._model_identity(r) for r in (identity_avoid or ())
                                   if isinstance(r, str) and r}
        self._taken = {S._model_identity(r) for r in list(refs) + list(self._excluded) + list(identity_avoid or ())
                       if isinstance(r, str) and r}

    def is_excluded(self, ref: str) -> bool:
        return (S._model_identity(ref) in self._identity_excluded
                or S._is_model_excluded(ref, self._excluded))

    def take(self, ref: str) -> bool:
        with self._lock:
            identity = S._model_identity(ref)
            if identity in self._taken or self.is_excluded(ref):
                return False
            self._taken.add(identity)
            return True


PROGRESS_ENABLED = S.as_flag(os.environ.get("FREEAGENT_PROGRESS", "1"))


_PROGRESS_LOCAL = threading.local()


def _progress_token_of(params) -> "str | int | None":
    """`tools/call` の params から `_meta.progressToken` を読む（str / int だけ。bool は除く）。"""
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    token = meta.get("progressToken")
    if isinstance(token, bool) or not isinstance(token, (str, int)):
        return None
    return token


class _ProgressSink:
    """1 回のツール呼び出しぶんの進捗。`emit` は `run_parallel` の回収ループから呼ばれる。"""

    def __init__(self, token, sender):
        self.token = token
        self._send = sender
        self._lock = threading.Lock()
        self.sent = 0

    def emit(self, progress, total=None, message=None) -> None:
        note = {"jsonrpc": "2.0", "method": "notifications/progress",
                "params": {"progressToken": self.token, "progress": float(progress)}}
        if total is not None:
            note["params"]["total"] = float(total)
        if message:
            note["params"]["message"] = str(message)[:120]
        with self._lock:               # 1 行 / 1 フレームを割らない
            self._send(note)
            self.sent += 1


@contextlib.contextmanager
def bound_progress(maker):
    """このスレッドの「送り手の作り」を差し替える（transport が要求 1 件ぶんだけ張る）。"""
    prev = getattr(_PROGRESS_LOCAL, "maker", None)
    _PROGRESS_LOCAL.maker = maker
    try:
        yield
    finally:
        _PROGRESS_LOCAL.maker = prev


@contextlib.contextmanager
def progress_for(params):
    """`tools/call` の params に応じて送り手を張る（トークンが無ければ**何もしない**）。"""
    prev = getattr(_PROGRESS_LOCAL, "sink", None)
    maker = getattr(_PROGRESS_LOCAL, "maker", None)
    token = _progress_token_of(params) if S.PROGRESS_ENABLED else None
    sink = None
    if maker is not None and token is not None:
        try:
            sink = _ProgressSink(token, maker(token))
        except Exception as exc:            # 送り先の用意に失敗しても本体は動かす
            S._debug("progress_error", f"{type(exc).__name__}: {exc}")
    try:
        _PROGRESS_LOCAL.sink = sink         # 前の要求のぶんを引き継がない
        yield sink
    finally:
        _PROGRESS_LOCAL.sink = prev


def progress_emit(progress, total=None, message=None) -> None:
    """送り手があれば 1 件送る（無ければ何もしない＝仕様どおり）。進捗で本体を壊さない。"""
    sink = getattr(_PROGRESS_LOCAL, "sink", None)
    if sink is None:
        return
    try:
        sink.emit(progress, total, message)
    except Exception as exc:
        S._debug("progress_error", f"{type(exc).__name__}: {exc}")


def progress_label(job) -> str:
    """進捗の 1 行に添える短いラベル（モデル名・ソース名など）。長い本文は切る。"""
    if isinstance(job, str):
        text = job
    elif isinstance(job, (tuple, list)) and job and isinstance(job[0], str):
        text = job[0]
    else:
        return ""
    return text.strip().replace("\n", " ")[:40]


COALESCE_ENABLED = S.as_flag(os.environ.get("FREEAGENT_COALESCE", "1"))


COALESCE_WAIT_S = max(0.0, S._env_float("FREEAGENT_COALESCE_WAIT", S.CALL_BUDGET_S))


_COALESCE_LOCAL = threading.local()


def _call_coalescer():
    """このスレッドの合流器（ツール呼び出しの外では None＝畳まない）。"""
    return getattr(_COALESCE_LOCAL, "coalescer", None)


@contextlib.contextmanager
def bound_coalescer():
    """ツール呼び出し 1 回ぶんの合流器を張る（入れ子は元に戻す）。"""
    prev = getattr(_COALESCE_LOCAL, "coalescer", None)
    _COALESCE_LOCAL.coalescer = _CallCoalescer(S.COALESCE_WAIT_S) if S.COALESCE_ENABLED else None
    try:
        yield
    finally:
        _COALESCE_LOCAL.coalescer = prev


def _run_worker(worker, run: str, tool: str):
    """ワーカーのスレッドに呼び出し ID を張ってから本体を呼ぶ（`run_parallel` の内側でのみ使う）。"""
    def wrapped(job):
        with S.run_scope(run, tool):
            return worker(job)
    return wrapped


def _coalescer_worker(worker, coalescer):
    """ワーカーを包んで、合流器を**そのスレッド**にも見せる。

    合流器は `bound_coalescer()` を張ったスレッド（= ツール呼び出しを実行しているスレッド）の
    スレッドローカルにある。`run_parallel` はワーカーを**別スレッド**で走らせるので、包まないと
    ワーカーから `_call_coalescer()` が見えず、**並列経路では重複が 1 件も畳まれない**
    （進捗通知は「回収ループが呼び出し側スレッドで回る」ので包み不要——ここが非対称な点）。
    """

    def run(job):
        prev = getattr(_COALESCE_LOCAL, "coalescer", None)
        _COALESCE_LOCAL.coalescer = coalescer
        try:
            return worker(job)
        finally:
            _COALESCE_LOCAL.coalescer = prev

    return run


class _CallCoalescer:
    """同じキーの呼び出しを 1 本だけ上流へ流し、結果を全員に配る。

    待つ側には**上限**を置く（規約 53 と同じ型）。超えたら自分で取りに行く＝上流 1 本を無駄にする
    代わりに固まらない。`run_parallel` は先頭のジョブから順にワーカーへ渡すので、待つ側が走り出して
    いる時点で同じキーの先頭は**既に走っている**（だから待ち合いが循環しない）。
    """

    def __init__(self, wait_max: float = 0.0):
        self._lock = threading.Lock()
        self._slots: dict = {}
        self._wait_max = max(0.0, float(wait_max))
        self.saved = 0          # 上流を呼ばずに済んだ件数

    def run(self, key, thunk):
        with self._lock:
            slot = self._slots.get(key)
            first = slot is None
            if first:
                slot = {"event": threading.Event(), "value": None, "error": None}
                self._slots[key] = slot
        if first:
            try:
                value = thunk()
            except BaseException as exc:        # 先頭が落ちたら、待っている側にはエラーを配る
                slot["error"] = {"error": f"{type(exc).__name__}: {exc}"}
                raise
            else:
                slot["value"] = value           # `finally` より先に置く（待つ側が読む前に）
                return value
            finally:
                slot["event"].set()
        if slot["event"].wait(self._wait_max or None):
            if slot["error"] is not None:
                return dict(slot["error"], coalesced=True)
            value = copy.deepcopy(slot["value"])
            if isinstance(value, dict):
                value["coalesced"] = True       # 上流を呼んでいないことを**隠さない**（規約 8）
            with self._lock:
                self.saved += 1
            return value
        return thunk()                          # 待ち切れなかったので自分で取りに行く


class Deadline:
    """並列実行の壁時計予算。`run_parallel` が**残り時間で回収を打ち切る**ために使う。

    Python のスレッドは強制終了できない（anyio の文書も「shutdown にはコード側の完全な協力が要る」と
    明記している）。したがって打ち切りは「**回収をやめる**」であり、投入済みの呼び出しは裏で完了する
    （結果は捨てるが、キャッシュ・統計・クォータには正しく残る）。打ち切った分は `dropped: true` として
    呼び出し側に返す（規約 8・63「黙って捨てない」）。
    """

    def __init__(self, seconds: float):
        self.total = max(0.0, float(seconds))
        # All cutoff and worker-completion readings use the same high-resolution
        # clock; monotonic() is coarser on Windows Python 3.11/3.12.
        self._until = S.time.perf_counter() + self.total
        self._cancel_signal = S.Future()
        self._cancel_lock = threading.Lock()

    def remaining(self) -> float:
        return max(0.0, self._until - S.time.perf_counter())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def cancel(self) -> None:
        """打ち切る（回収は即座に終わり、未開始のジョブは投入されない）。"""
        with self._cancel_lock:
            self._until = min(self._until, S.time.perf_counter())
        # Future callbacks execute synchronously and may block or re-enter.
        # Every canceller can signal even if another is paused before set_result.
        try:
            self._cancel_signal.set_result(None)
        except S.InvalidStateError:
            pass  # another canceller has already signalled

    def __repr__(self) -> str:
        return f"Deadline(total={self.total:.1f}s, remaining={self.remaining():.1f}s)"


_BUDGET_LOCAL = threading.local()


@contextlib.contextmanager
def budget_scope(seconds: float | None = None):
    """**ツール呼び出し 1 回ぶん**の予算をこのスレッドに張る（入れ子は元に戻す）。

    以前は `call_budget()` が呼ばれるたびに新しい `Deadline` を作っていたため、
    (a) `tool_map` / `tool_agent` は予算を渡しておらず打ち切りが効かない、
    (b) `consult(debate_depth=deep)` はラウンドごとに満額（240 秒 × 2）を作る、
    (c) `panel(refine)` / `map(reduce)` の後段は残り時間を見ない、
    という 3 つの穴があった（実測: `S.CALL_BUDGET_S=0.1` で `tool_fanout` は 0.11 秒で打ち切るのに
    `tool_map` は 0.50 秒完走、`consult(deep)` は 0.15 秒予算で 0.24 秒走った）。
    1 回の呼び出しに 1 つの `Deadline` を共有すれば、MCP クライアントの 300 秒より手前で必ず返る。
    """
    previous = getattr(_BUDGET_LOCAL, "deadline", None)
    limit = S.CALL_BUDGET_S if seconds is None else seconds
    _BUDGET_LOCAL.deadline = (Deadline(limit) if limit > 0 else
                              Deadline(float("inf")) if getattr(S._REQ_LOCAL, "id", None) is not None else None)
    try:
        yield _BUDGET_LOCAL.deadline
    finally:
        _BUDGET_LOCAL.deadline = previous


def call_budget() -> "Deadline | None":
    """このツール呼び出しの予算（`FREEAGENT_CALL_BUDGET` が 0 なら None＝無制限）。

    呼び出し側スレッドに `budget_scope()` が張られていれば**その残り**を共有する（後段の
    refine / reduce / 討論の 2 ラウンド目が満額を新しく作らない）。
    """
    shared = getattr(_BUDGET_LOCAL, "deadline", None)
    if shared is not None:
        return shared
    return Deadline(S.CALL_BUDGET_S) if S.CALL_BUDGET_S > 0 else None


def budget_expired() -> bool:
    """このツール呼び出しの予算を使い切ったか（後段を始めてよいかの判定）。"""
    shared = getattr(_BUDGET_LOCAL, "deadline", None)
    return bool(shared is not None and shared.expired)


EXPORTS = {
    "REDACT_SECRETS": REDACT_SECRETS,
    "_SECRET_PATTERNS": _SECRET_PATTERNS,
    "scrub_secrets": scrub_secrets,
    "_call_once": _call_once,
    "_candidates": _candidates,
    "_is_auth_error": _is_auth_error,
    "_auth_hint": _auth_hint,
    "_call_model_uncached": _call_model_uncached,
    "_ModelClaims": _ModelClaims,
    "PROGRESS_ENABLED": PROGRESS_ENABLED,
    "_PROGRESS_LOCAL": _PROGRESS_LOCAL,
    "_progress_token_of": _progress_token_of,
    "_ProgressSink": _ProgressSink,
    "bound_progress": bound_progress,
    "progress_for": progress_for,
    "progress_emit": progress_emit,
    "progress_label": progress_label,
    "COALESCE_ENABLED": COALESCE_ENABLED,
    "COALESCE_WAIT_S": COALESCE_WAIT_S,
    "_COALESCE_LOCAL": _COALESCE_LOCAL,
    "_call_coalescer": _call_coalescer,
    "bound_coalescer": bound_coalescer,
    "_run_worker": _run_worker,
    "_coalescer_worker": _coalescer_worker,
    "_CallCoalescer": _CallCoalescer,
    "Deadline": Deadline,
    "_BUDGET_LOCAL": _BUDGET_LOCAL,
    "budget_scope": budget_scope,
    "call_budget": call_budget,
    "budget_expired": budget_expired,
}
