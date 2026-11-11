"""§6 決定論ツール: diagnose（記録の集計）

server.py から機械的に切り出した（v0.42.7・references/module-split.md）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import datetime
import json
import os
import re

from . import server as S  # noqa: E402

DIAG_MAX_LINES = S._env_int("FREEAGENT_DIAG_MAX_LINES", 20000)


DIAG_SLOW_MIN_CALLS = 3


DIAG_PROD_MIN = S._env_int("FREEAGENT_DIAG_PROD_MIN", 10)


_BUDGET_OVER_RATIO = 1.2


BUDGET_VIOLATION_MIN = 2


def _is_over_budget(result: dict) -> bool:
    """この 1 回が `max_tokens` を大きく超えて返したか（v0.42.4・issue #8）。

    **比べるのは「実効の予算」**（空応答リトライで 64 → 512 に引き上げた後の値）。
    実測: traces 2,288 行で `max_tokens >= 512` の行が 63 行あり、それらは引き上げ後の値。
    実効予算で比べると超過は 0 件で、実験2の「64 指定で 931 字」は**サーバー自身の引き上げ**が
    原因だった（モデルが予算を無視したわけではない）。`FREEAGENT_RANK_BUDGET` を既定 off に
    しているのは、この実測で降格の根拠がまだ無いため。
    """
    if not isinstance(result, dict):
        return False
    budget = S.as_int(result.get("max_tokens"), 0, 0, 10 ** 9)
    used = S.as_int(((result.get("tokens") or {}).get("completion")), 0, 0, 10 ** 9)
    return bool(budget and used and used > budget * _BUDGET_OVER_RATIO)


def _diag_budget(rows: list[dict]) -> dict:
    """`max_tokens` を大きく超えて返したモデルを数える（v0.42.3・issue #8）。"""
    over: dict[str, list[float]] = {}
    checked = 0
    for row in rows or []:
        budget = S.as_int(row.get("max_tokens"), 0, 0, 10 ** 9)
        used = S.as_int(((row.get("tokens") or {}).get("completion")), 0, 0, 10 ** 9)
        if not budget or not used:
            continue
        checked += 1
        ratio = used / budget
        if ratio > _BUDGET_OVER_RATIO:
            ref = S.as_str(row.get("served_by")) or S.as_str(row.get("ref"))
            if ref:
                over.setdefault(ref, []).append(ratio)
    models = [{"model": m, "n": len(vals), "max_ratio": round(max(vals), 2),
               "mean_ratio": round(sum(vals) / len(vals), 2)}
              for m, vals in over.items() if len(vals) >= BUDGET_VIOLATION_MIN]
    models.sort(key=lambda r: (-r["max_ratio"], r["model"]))
    return {"checked": checked, "threshold": _BUDGET_OVER_RATIO,
            "min_rows": BUDGET_VIOLATION_MIN, "models": models[:8]}


def _diag_pct(values: list[float], p: float) -> float | None:
    """ソート済み配列の分位（補間しない。件数が少ないときに小数を捏造しない）。"""
    if not values:
        return None
    idx = max(0, min(len(values) - 1, int(round((len(values) - 1) * p))))
    return round(values[idx], 3)


def _diag_norm(message) -> str:
    """同じ失敗をまとめるための正規化（ID・数値を伏せる）。"""
    text = S.as_str(message)
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", text)
    text = re.sub(r"\d+(?:\.\d+)?", "N", text)
    return S.truncate(text, 120)


def _diag_read_file(path: str, since: float, info: dict) -> list[dict]:
    """traces 1 ファイルぶんを読む（壊れた行は数えて飛ばす）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = [line for line in fh if line.strip()]
    except OSError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return []
    info["lines"] = info.get("lines", 0) + len(raw)
    rows: list[dict] = []
    for line in raw:
        try:
            row = json.loads(line)
        except Exception:
            info["unreadable"] += 1
            continue
        if not isinstance(row, dict):
            info["unreadable"] += 1
            continue
        ts = S.as_float(row.get("ts"), 0.0)
        if ts and ts < since:
            continue
        rows.append(row)
    return rows


def _diag_rows(since: float) -> tuple[list[dict], dict]:
    """traces を読む。壊れた行は数えて飛ばす（全体を失わない）。

    **ローテーション直後も窓を覆う**（v0.41.0）: `traces.jsonl` は 5MB 超で `.1` に退避するが、
    旧実装は main しか読まず、退避の直後に `freeagent_diagnose(days=7)` の窓が突然空になった。
    `.1` も読んで（こちらが古い）、main とつなげてから上限で末尾に絞る。
    """
    path = S.trace_path()
    info = {"path": path, "exists": os.path.exists(path), "lines": 0,
            "capped": 0, "unreadable": 0, "error": ""}
    if not info["exists"]:
        return [], info
    rows = _diag_read_file(path, since, info)
    rotated = path + ".1"
    if os.path.exists(rotated):
        info["rotated_file"] = rotated
        old_rows = _diag_read_file(rotated, since, info)
        rows = old_rows + rows
    if len(rows) > DIAG_MAX_LINES:
        info["capped"] = len(rows) - DIAG_MAX_LINES
        rows = rows[-DIAG_MAX_LINES:]
    return rows, info


def _diag_advice(totals: dict, by_class: dict, ref_fail: dict, fails: list[dict]) -> dict:
    """失敗の内訳から次の一手を決める（決定論。モデルに聞かない）。"""
    calls = S.as_int(totals.get("calls"), 0, 0, 10**9)
    failed = S.as_int(totals.get("failed"), 0, 0, 10**9)
    if not calls:
        return {"kind": "no_data", "check": ["FREEAGENT_TRACE_PATH"],
                "advice": "この窓に traces がありません。days を広げるか、実際に 1 回ツールを呼んでから"
                          "再度実行してください（記録が無いことを成功とは読みません）。"}
    if not failed:
        return {"kind": "ok", "check": [],
                "advice": "窓の中に失敗はありません（全て成功）。この調子なら選抜の設定を触る必要はありません。"}
    share = {k: v / failed for k, v in by_class.items()}
    if share.get("rate_limited", 0.0) >= 0.4:
        return {"kind": "rate_limited", "check": ["FREEAGENT_QUOTA_LIMITS", "FREEAGENT_PROVIDER_ORDER",
                                                  "FREEAGENT_QUOTA"],
                "advice": f"失敗 {failed} 件のうち {by_class['rate_limited']} 件が枠（429・クォータ・クールダウン）です。"
                          "FREEAGENT_QUOTA_LIMITS の宣言値が実際の枠より小さいか、1 つの提供元に寄りすぎています"
                          "（宣言が無ければ何も制限していないので、この分類は提供元側の 429 です）。"
                          "size を減らすか、PROVIDER_ORDER で別の提供元を先に並べてください。"}
    if share.get("no_output", 0.0) >= 0.3:
        ranked_fail = [r for r, _ in sorted(ref_fail.items(), key=lambda kv: (-kv[1], kv[0]))]
        worst = ", ".join(ranked_fail[:3]) or "特定できず"
        return {"kind": "empty_output", "check": ["FREEAGENT_MODEL_EXCLUDE"],
                "advice": f"失敗 {failed} 件のうち {by_class['no_output']} 件が空応答です（思考モデルが max_tokens を"
                          f"使い切る型。サーバーは既定で {S._EMPTY_TOKEN_FLOOR}→{S._EMPTY_RETRY_CAP} まで引き上げて"
                          "再試行するので、それでも空になるモデルは FREEAGENT_MODEL_EXCLUDE で外すか、"
                          f"呼び出しの max_tokens をさらに上げてください（上位: {worst}）。"}
    if share.get("timeout", 0.0) >= 0.2:
        return {"kind": "timeout", "check": ["FREEAGENT_CALL_BUDGET", "FREEAGENT_MAX_WORKERS"],
                "advice": f"失敗 {failed} 件のうち {by_class['timeout']} 件が時間切れです。size と FREEAGENT_MAX_WORKERS を"
                          f"下げるか、FREEAGENT_CALL_BUDGET を延ばしてください（既定は {S.CALL_BUDGET_S:g} 秒）。"}
    if ref_fail:
        ref, n = max(ref_fail.items(), key=lambda kv: kv[1])
        if n >= 4 and n / failed >= 0.6:
            return {"kind": "one_model", "check": ["FREEAGENT_MODEL_EXCLUDE"],
                    "advice": f"失敗の {n}/{failed} 件が {ref} に集中しています。"
                              "そのモデルを FREEAGENT_MODEL_EXCLUDE で外すか、クールダウンが明けるまで"
                              "別のモデルを使ってください（全体の設定を触る前に 1 体だけ外すのが安い）。"}
    kinds = sorted(share.items(), key=lambda kv: -kv[1])
    detail = ", ".join(f"{k} {v:.0%}" for k, v in kinds[:3])
    return {"kind": "mixed", "check": ["FREEAGENT_DEBUG_LOG"],
            "advice": f"失敗 {failed} 件の内訳は {detail} です（単一の原因に寄っていません）。"
                      "recent_failures の実メッセージを見て、直近の 1 件から切り分けてください"
                      "（推測でまとめない）。"}


def tool_diagnose(args: dict) -> dict:
    """記録（traces）から、失敗の内訳・遅さ・次の一手を決定論で返す（モデル呼び出し 0 回）。"""
    days = S.as_int(args.get("days"), 7, 1, 90)
    limit = S.as_int(args.get("limit"), 5, 1, 20)
    kind_filter = S.as_str(args.get("kind"))
    model_filter = S.as_str(args.get("model"))
    run_filter = S.as_str(args.get("run"))
    now = S.now_ts()
    since = now - days * 86400
    rows, info = _diag_rows(since)
    if kind_filter:
        rows = [r for r in rows if S.as_str(r.get("kind")) == kind_filter]
    if model_filter:
        rows = [r for r in rows
                if model_filter in (S.as_str(r.get("ref")), S.as_str(r.get("served_by")))]
    if run_filter:
        rows = [r for r in rows if S.as_str(r.get("run")) == run_filter]
    ok_rows = [r for r in rows if r.get("ok")]
    fail_rows = [r for r in rows if not r.get("ok")]
    totals = {"calls": len(rows), "ok": len(ok_rows), "failed": len(fail_rows),
              "ok_rate": round(len(ok_rows) / len(rows), 3) if rows else None}
    def _row_error_class(row: dict) -> str:
        raw_cls = S.as_str(row.get("error_class"))
        err_text = S.as_str(row.get("error"))
        if raw_cls and raw_cls != "other":
            return raw_cls
        if err_text:
            reclassified = S.classify_error(err_text)
            if reclassified != "other":
                return reclassified
        return raw_cls or "unknown"

    by_class: dict[str, int] = {}
    for row in fail_rows:
        key = _row_error_class(row)
        by_class[key] = by_class.get(key, 0) + 1
    # **チャネル分離**（v0.42.1）: `tool` / `run` を持つ行＝本番（MCP ツール経由）、
    # 持たない行＝ベンチの直呼び（`scripts/measure_quality.py` 等の eval/probe）。
    # 実測: 30 日の窓 1,780 行のうち本番は 39 行で、失敗率を混ぜると意味が変わった。
    def _channel(row: dict) -> str:
        return "tool" if (S.as_str(row.get("tool")) or S.as_str(row.get("run"))) else "bench"

    by_channel: dict[str, dict] = {}
    for row in rows:
        slot = by_channel.setdefault(_channel(row), {"calls": 0, "ok": 0, "failed": 0,
                                                     "by_error_class": {}})
        slot["calls"] += 1
        if row.get("ok"):
            slot["ok"] += 1
        else:
            slot["failed"] += 1
            key = _row_error_class(row)
            slot["by_error_class"][key] = slot["by_error_class"].get(key, 0) + 1
    for slot in by_channel.values():
        slot["ok_rate"] = round(slot["ok"] / slot["calls"], 3) if slot["calls"] else None
        slot["by_error_class"] = dict(sorted(slot["by_error_class"].items(),
                                             key=lambda kv: (-kv[1], kv[0])))
    # `finish_reason` の内訳（v0.42.1 から記録。古い行は数えない）。
    by_finish: dict[str, int] = {}
    for row in rows:
        fr = S.as_str(row.get("finish_reason"))
        if fr:
            by_finish[fr] = by_finish.get(fr, 0) + 1
    by_kind: dict[str, dict] = {}
    for row in rows:
        key = S.as_str(row.get("kind")) or "unknown"
        slot = by_kind.setdefault(key, {"calls": 0, "ok": 0})
        slot["calls"] += 1
        slot["ok"] += 1 if row.get("ok") else 0
    errors: dict[str, int] = {}
    for row in fail_rows:
        key = _diag_norm(row.get("error"))
        errors[key] = errors.get(key, 0) + 1
    top_errors = [{"count": n, "error": text}
                  for text, n in sorted(errors.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
    latencies = sorted(S.as_float(r.get("latency_s"), 0.0) for r in ok_rows
                       if S.as_float(r.get("latency_s"), 0.0) > 0)
    latency = {"n": len(latencies), "p50": _diag_pct(latencies, 0.5),
               "p90": _diag_pct(latencies, 0.9), "p95": _diag_pct(latencies, 0.95),
               "p99": _diag_pct(latencies, 0.99),
               "max": round(latencies[-1], 3) if latencies else None}
    per_ref: dict[str, list[float]] = {}
    ref_fail: dict[str, int] = {}
    for row in rows:
        ref = S.as_str(row.get("served_by")) or S.as_str(row.get("ref"))
        if not ref:
            continue
        if row.get("ok"):
            if S.as_float(row.get("latency_s"), 0.0) > 0:
                per_ref.setdefault(ref, []).append(S.as_float(row.get("latency_s"), 0.0))
        else:
            ref_fail[ref] = ref_fail.get(ref, 0) + 1
    slowest = []
    for ref, values in per_ref.items():
        if len(values) < DIAG_SLOW_MIN_CALLS:
            continue
        values.sort()
        slowest.append({"model": ref, "n": len(values),
                        "p50": _diag_pct(values, 0.5), "p95": _diag_pct(values, 0.95)})
    slowest.sort(key=lambda row: (-row["p95"], -row["n"], row["model"]))
    # 呼び出し単位（run）の内訳。**同じ呼び出しの中の失敗**はここでしか見えない。
    per_run: dict[str, dict] = {}
    unattributed = 0
    for row in rows:
        rid = S.as_str(row.get("run"))
        if not rid:
            unattributed += 1
            continue
        slot = per_run.setdefault(rid, {"run": rid, "tool": S.as_str(row.get("tool")) or "-",
                                       "calls": 0, "ok": 0, "failed": 0,
                                       "first_ts": 0.0, "error_classes": {}})
        slot["calls"] += 1
        if row.get("ok"):
            slot["ok"] += 1
        else:
            slot["failed"] += 1
            key = _row_error_class(row)
            slot["error_classes"][key] = slot["error_classes"].get(key, 0) + 1
        ts = S.as_float(row.get("ts"), 0.0)
        if ts and (not slot["first_ts"] or ts < slot["first_ts"]):
            slot["first_ts"] = ts
    run_rows = []
    for slot in per_run.values():
        run_rows.append({"run": slot["run"], "tool": slot["tool"], "calls": slot["calls"],
                         "ok": slot["ok"], "failed": slot["failed"],
                         "ok_rate": round(slot["ok"] / slot["calls"], 3) if slot["calls"] else None,
                         "at": datetime.datetime.fromtimestamp(slot["first_ts"]).strftime("%Y-%m-%d %H:%M:%S")
                         if slot["first_ts"] else "",
                         "error_classes": dict(sorted(slot["error_classes"].items(),
                                                      key=lambda kv: (-kv[1], kv[0])))})
    run_rows.sort(key=lambda row: (-row["failed"], -row["calls"], row["run"]))
    recent = []
    for row in fail_rows[-limit:]:
        recent.append({"ts": round(S.as_float(row.get("ts"), 0.0), 3),
                       "at": datetime.datetime.fromtimestamp(S.as_float(row.get("ts"), 0.0)).strftime("%Y-%m-%d %H:%M:%S")
                       if S.as_float(row.get("ts"), 0.0) else "",
                       "kind": S.as_str(row.get("kind")),
                       "model": S.as_str(row.get("ref")),
                       "error_class": _row_error_class(row),
                       "error": S.truncate(S.as_str(row.get("error")), 200)})
    # advice の根拠にする行: 本番が `DIAG_PROD_MIN` 件以上あれば本番だけ
    # （ベンチの連打で「本番の次の一手」が歪まないように）。足りなければ全体。
    tool_rows = [row for row in rows if _channel(row) == "tool"]
    advice_channel = "tool" if len(tool_rows) >= DIAG_PROD_MIN else "all"
    advice_rows = tool_rows if advice_channel == "tool" else rows
    a_ok = [row for row in advice_rows if row.get("ok")]
    a_fail = [row for row in advice_rows if not row.get("ok")]
    a_totals = {"calls": len(advice_rows), "ok": len(a_ok), "failed": len(a_fail),
                "ok_rate": round(len(a_ok) / len(advice_rows), 3) if advice_rows else None}
    a_class: dict[str, int] = {}
    a_ref_fail: dict[str, int] = {}
    for row in a_fail:
        key = _row_error_class(row)
        a_class[key] = a_class.get(key, 0) + 1
        ref = S.as_str(row.get("served_by")) or S.as_str(row.get("ref"))
        if ref:
            a_ref_fail[ref] = a_ref_fail.get(ref, 0) + 1
    advice = _diag_advice(a_totals, a_class, a_ref_fail, a_fail)
    budget = _diag_budget(rows)
    if budget["models"] and advice.get("kind") == "empty_output":
        top = budget["models"][0]
        advice["advice"] += (f" さらに {top['model']} が max_tokens の {top['max_ratio']} 倍まで返しており"
                            "（予算での出力長制御が効かない提供元）、FREEAGENT_RANK_BUDGET=1 で選抜から"
                            "降格できます。")
        advice.setdefault("check", []).append("FREEAGENT_RANK_BUDGET")

    notes = []
    bench_calls = S.as_int((by_channel.get("bench") or {}).get("calls"), 0, 0, 10 ** 9)
    if bench_calls:
        tail = "（次の一手は本番だけを根拠にしています）" if advice_channel == "tool" else ""
        notes.append(f"ベンチ直呼び（tool / run を持たない行）が {bench_calls} 件あります。"
                     f"内訳は totals_by_channel で分けています{tail}")
    if budget["checked"] and not budget["models"]:
        notes.append(f"max_tokens の比較は実効の予算（空応答の引き上げ後）で行いました"
                     f"（{budget['checked']} 行を検査・超過 0 件）")
    for item in budget["models"]:
        notes.append(f"{item['model']} が max_tokens の {item['max_ratio']} 倍まで返しています"
                     f"（{item['n']} 件）。予算での出力長制御が効かない提供元です")
    length_rows = S.as_int(by_finish.get("length"), 0, 0, 10 ** 9)
    if length_rows:
        notes.append(f"finish_reason=length が {length_rows} 件（思考トークンで予算を使い切った型）")
    if info["capped"]:
        notes.append(f"古い {info['capped']} 行は読みませんでした（FREEAGENT_DIAG_MAX_LINES={DIAG_MAX_LINES}）")
    if info["unreadable"]:
        notes.append(f"壊れた行 {info['unreadable']} 行を飛ばしました")
    if info["error"]:
        notes.append(f"traces を読めませんでした: {info['error']}")
    if not info["exists"]:
        notes.append("traces がまだありません（FREEAGENT_TRACE=0 で切っている可能性）")
    unattributed_tool = sum(1 for row in rows
                            if S.as_str(row.get("tool")) and not S.as_str(row.get("run")))
    if unattributed_tool:
        notes.append(f"run を持たない本番の行が {unattributed_tool} 件あります"
                     "（v0.38.0 より前の記録）")
    if kind_filter or model_filter or run_filter:
        notes.append("kind / model / run で絞った結果です（全体は days だけを指定して見る）")
    return {
        "window": {"days": days, "since": round(since, 3), "until": round(now, 3),
                   "since_utc": datetime.datetime.fromtimestamp(since, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "until_utc": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "filters": {"kind": kind_filter, "model": model_filter, "run": run_filter}},
        "totals": totals,
        "by_error_class": dict(sorted(by_class.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_kind": dict(sorted(by_kind.items())),
        "totals_by_channel": by_channel,
        "by_finish_reason": dict(sorted(by_finish.items(), key=lambda kv: (-kv[1], kv[0]))),
        "advice_channel": advice_channel,
        "runs": run_rows[:limit],
        "runs_total": len(run_rows),
        "unattributed_calls": unattributed,
        "top_errors": top_errors,
        "latency": latency,
        "slowest_models": slowest[:limit],
        "recent_failures": recent,
        "advice": advice,
        "budget": budget,
        "trace": info,
        "notes": notes,
        "model_calls": 0,
    }


EXPORTS = {
    "DIAG_MAX_LINES": DIAG_MAX_LINES,
    "DIAG_SLOW_MIN_CALLS": DIAG_SLOW_MIN_CALLS,
    "DIAG_PROD_MIN": DIAG_PROD_MIN,
    "_BUDGET_OVER_RATIO": _BUDGET_OVER_RATIO,
    "BUDGET_VIOLATION_MIN": BUDGET_VIOLATION_MIN,
    "_is_over_budget": _is_over_budget,
    "_diag_budget": _diag_budget,
    "_diag_pct": _diag_pct,
    "_diag_norm": _diag_norm,
    "_diag_read_file": _diag_read_file,
    "_diag_rows": _diag_rows,
    "_diag_advice": _diag_advice,
    "tool_diagnose": tool_diagnose,
}
