"""§6 決定論ツール: admit（申告・配分の判定）

server.py から機械的に切り出した（v0.42.7・references/module-split.md）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from . import server as S  # noqa: E402

ADMIT_MODES = ("off", "observe", "auto")


ADMIT_REPORT_KINDS = ("progress", "waiting", "ready", "blocked", "failed", "dropped")


ADMIT_POLICY_FILE = "admit_policy.json"


ADMIT_LOG_FILE = "admission.jsonl"


ADMIT_LOG_READ_MAX = 5000


ADMIT_MAX_TRIGGERS = 16


ADMIT_DEFAULT_POLICY = {
    "accept_mode": "observe",   # off / observe（記録だけ）/ auto
    "sources": {},              # {source: {"allow": [...], "bots": [...], "admission": {scope: {...}}}}
    "rules": [],                # [{"id": "...", "enabled": true, "text": "..."}]
}


def _admit_policy_path() -> str:
    return os.path.join(S.state_dir(), ADMIT_POLICY_FILE)


def _admit_log_path() -> str:
    return os.path.join(S.state_dir(), ADMIT_LOG_FILE)


def admit_policy() -> dict:
    """受付ポリシーを読む（無ければ既定＝許可リスト空・observe）。

    **版は内容から決める**（`p` + 先頭 8 桁の sha1）。手で上げる版だと、書き忘れた変更が
    同じ版のまま通り、Mandate の「どの規則で認可したか」が辿れなくなる。
    """
    import hashlib

    policy = S._deep_copy(ADMIT_DEFAULT_POLICY)
    path = _admit_policy_path()
    if os.path.exists(path):
        loaded: object = {}
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.loads(fh.read() or "{}")
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            for key in ("accept_mode", "sources", "rules"):
                if key in loaded:
                    policy[key] = loaded[key]
    mode = S.as_str(policy.get("accept_mode"), "observe").strip().lower()
    policy["accept_mode"] = mode if mode in ADMIT_MODES else "observe"
    if not isinstance(policy.get("sources"), dict):
        policy["sources"] = {}
    if not isinstance(policy.get("rules"), list):
        policy["rules"] = []
    canonical = S._dumps(
        {"accept_mode": policy["accept_mode"], "sources": policy["sources"],
         "rules": [r for r in policy["rules"] if isinstance(r, dict)]}, sort_keys=True, separators=(",", ":"))
    policy["version"] = "p" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:8]
    return policy


def _admit_gate(policy: dict, trig: dict) -> dict:
    """1 件の Trigger を裁定する（決定論。**モデルを呼ばない**）。

    規則は dsh-superagent の 002-authorization の写し: bot は推測候補まで、許可者の名指しは明示、
    チケットのあるスレッドへの追記は追記扱い、それ以外は受付表（scope ごとのセル）。
    """
    source = S.as_str(trig.get("source")).strip()
    actor = S.as_str(trig.get("actor")).strip()
    kind = S.as_str(trig.get("kind"), "mention").strip().lower()
    scope = S.as_str(trig.get("scope"), "default").strip()
    is_bot = S.as_flag(trig.get("is_bot"))
    ticket = S.as_str(trig.get("ticket")).strip()
    scope_all = S.as_flag(trig.get("scope_all"))
    addressed = kind in ("mention", "direct", "dm", "addressed")

    src = (policy.get("sources") or {}).get(source) or {}
    allow = {S.as_str(x).strip() for x in (src.get("allow") or []) if S.as_str(x).strip()}
    bots = {S.as_str(x).strip() for x in (src.get("bots") or []) if S.as_str(x).strip()}
    table = src.get("admission") or {}
    row = table.get(scope) if isinstance(table.get(scope), dict) else {}
    cells = {
        "listed_silent": S.as_str(row.get("listed_silent"), "inferred").strip().lower(),
        "unlisted_addressed": S.as_str(row.get("unlisted_addressed"), "ignore").strip().lower(),
        "unlisted_silent": S.as_str(row.get("unlisted_silent"), "ignore").strip().lower(),
    }

    if is_bot:
        if actor in bots and (addressed or scope_all):
            return {"strength": "inferred", "basis": "bot_list"}
        return {"strength": "not_admitted", "basis": "bot_not_listed"}
    if addressed and actor in allow:
        return {"strength": "explicit", "basis": "listed+addressed"}
    if ticket:
        if addressed and cells["unlisted_addressed"] == "explicit":
            return {"strength": "explicit", "basis": "table_unlisted_explicit"}
        if actor in allow:
            return {"strength": "explicit", "basis": "followup+listed"}
        return {"strength": "inferred", "basis": "followup+unlisted"}
    if addressed:
        cell = cells["unlisted_addressed"]
    elif actor in allow:
        cell = cells["listed_silent"]
    else:
        cell = cells["unlisted_silent"]
    if cell == "explicit":
        return {"strength": "explicit", "basis": "table_explicit"}
    if cell == "inferred":
        return {"strength": "inferred", "basis": "table_inferred"}
    return {"strength": "not_admitted", "basis": "table_ignore"}


def _admit_disposition(strength: str, mode: str) -> tuple[str, str]:
    """強度と accept_mode から裁定を決める。`observe` は受け入れるはずを記録だけ。"""
    if strength == "explicit":
        return "accept", "explicit"
    if strength == "inferred":
        if mode == "off":
            return "ignored", "mode_off"
        if mode == "observe":
            return "would_accept", "observe"
        return "accept", "auto"
    return "ignored", "not_admitted"


def _admit_mandate(policy: dict, trig: dict, strength: str) -> dict:
    """accept が与える認可。**不変**（広げるなら新しい裁定を作る）。"""
    rules = [S.as_str(r.get("id")) for r in (policy.get("rules") or [])
             if isinstance(r, dict) and r.get("enabled")]
    assumptions = [{"question": S.truncate(S.as_str(a.get("question")), 200),
                    "default": S.truncate(S.as_str(a.get("default")), 120)}
                   for a in (trig.get("assumptions") or []) if isinstance(a, dict)]
    return {
        "policy_version": policy["version"],
        "strength": strength,
        "rules": [r for r in rules if r],
        "source": S.as_str(trig.get("source")),
        "actor": S.as_str(trig.get("actor")),
        "scope": S.as_str(trig.get("scope"), "default"),
        "card": S.as_str(trig.get("card")),
        "assumptions": assumptions,
    }


def _admit_key(row: dict) -> str:
    """冪等性の手がかり。入力（trigger_id）と記録（trigger）の両方の書き方を受ける。"""
    trigger = S.as_str(row.get("trigger")).strip() or S.as_str(row.get("trigger_id")).strip()
    return "|".join([S.as_str(row.get("source")).strip(), S.as_str(row.get("thread")).strip(), trigger])




def _admit_load(limit: int = ADMIT_LOG_READ_MAX) -> list[dict]:
    """追記ログを読む（新しい方から `limit` 行）。壊れた行は黙って飛ばす。

    **末尾だけをメモリに載せる**（v0.41.0。旧実装は `readlines()` で全行展開した）。
    ローテーション（`.1`）があるときは古い側の末尾もつなげて `limit` 行に絞る。
    """
    path = _admit_log_path()
    if not os.path.exists(path):
        return []
    lines: list[str] = []
    rotated = path + ".1"
    try:
        if os.path.exists(rotated):
            with open(rotated, encoding="utf-8") as fh:
                lines.extend(fh.readlines()[-limit:])
        with open(path, encoding="utf-8") as fh:
            lines.extend(fh.readlines()[-limit:])
    except OSError:
        return []
    lines = lines[-limit:]
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _admit_append(rows: list[dict]) -> None:
    """追記する（プロセスの外とも直列化する。既存の台帳と同じ型）。

    サイズ上限（`FREEAGENT_ADMIT_MAX_BYTES`・既定 5MB）を超えたら `traces.jsonl` と同じ型で
    `.1` へ退避する（traces のローテーションと同じく、追記専用ログを無限に育てない）。
    """
    if not rows:
        return
    path = _admit_log_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with S._file_lock(path):
        try:
            if os.path.getsize(path) > S._ADMIT_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(S._dumps(row) + "\n")


def _admit_report(args: dict) -> dict:
    """実行役の報告を検査する。**中身の真偽は見ない**（見られるのは形だけ）。"""
    report = args.get("report")
    if not isinstance(report, dict):
        report = {"kind": args.get("kind"), "reason": args.get("reason"),
                  "ticket": args.get("ticket"), "evidence": args.get("evidence")}
    kind = S.as_str(report.get("kind")).strip().lower()
    reason = S.as_str(report.get("reason")).strip()
    evidence = report.get("evidence") if isinstance(report.get("evidence"), dict) else {}
    commands = [c for c in (evidence.get("commands") or []) if isinstance(c, dict)]
    commands = [c for c in commands if S.as_str(c.get("cmd")).strip()]
    all_pass = bool(commands) and all(
        S.as_str(c.get("result")).strip().lower() in ("pass", "ok", "success", "成功") for c in commands)

    def _out(accepted: bool, why: str, ask: str = "") -> dict:
        data = {"action": "report", "accepted": accepted, "kind": kind or None,
                "why": why, "evidence_commands": len(commands),
                "ticket": S.as_str(report.get("ticket"))}
        if not accepted:
            data["ask"] = ask
            data["next_action"] = {"kind": "report_rejected", "advice": ask,
                                   "fallback_tools": ["freeagent_consult"]}
        return data

    if kind not in ADMIT_REPORT_KINDS:
        return _out(False, "unknown_kind",
                    f"kind を {', '.join(ADMIT_REPORT_KINDS)} から選んでください")
    if kind == "ready":
        if not commands:
            return _out(False, "no_evidence",
                        "実行した検証コマンドと結果を evidence.commands に入れてください"
                        "（確かめていないなら waiting で報告してください）")
        if not all_pass:
            return _out(False, "evidence_failed",
                        "失敗した検証があります。failed で報告するか、直してから ready にしてください")
        return _out(True, "ready_with_evidence")
    if kind in ("blocked", "failed", "dropped"):
        if not reason:
            return _out(False, "no_reason", "何が止めたか（または何も無かった理由）を reason に書いてください")
        return _out(True, f"{kind}_with_reason")
    return _out(True, kind)


def _admit_recent(args: dict, policy: dict) -> dict:
    """記録の読み戻し（observe の監査用）。**新しい順**。"""
    limit = S.as_int(args.get("limit"), 20, 1, 200)
    only = S.as_str(args.get("disposition")).strip().lower()
    rows = [r for r in _admit_load() if not only or S.as_str(r.get("disposition")) == only]
    picked = list(reversed(rows[-limit:]))
    return {"action": "list_recent", "policy_version": policy["version"],
            "accept_mode": policy["accept_mode"], "count": len(picked),
            "decisions": picked, "state": _admit_log_path(),
            "note": "observe では「受け入れるはずだった」判断が would_accept として残ります"}


def tool_admit(args: dict) -> dict:
    """受付の裁定（evaluate）・報告の検査（report）・記録の読み戻し（list_recent）。"""
    action = S.as_str(args.get("action"), "evaluate").strip().lower()
    policy = admit_policy()
    if action == "report":
        return _admit_report(args)
    if action in ("list_recent", "recent", "list"):
        return _admit_recent(args, policy)
    if action != "evaluate":
        return {"error": f"action は evaluate / report / list_recent のいずれかです（{action}）",
                "policy_version": policy["version"],
                "next_action": {"kind": "invalid_argument",
                                "advice": "action を evaluate / report / list_recent から選んでください",
                                "fallback_tools": []}}

    raw = args.get("triggers")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return {"error": "triggers は配列（または 1 個のオブジェクト）で必須です",
                "policy_version": policy["version"],
                "next_action": {"kind": "invalid_argument",
                                "advice": "source / thread / trigger_id / actor / kind / scope を持つ"
                                          "オブジェクトを triggers に入れていますか",
                                "fallback_tools": []}}
    if len(raw) > ADMIT_MAX_TRIGGERS:
        return {"error": f"triggers が多すぎます（{len(raw)} > {ADMIT_MAX_TRIGGERS}）。"
                         "分割して呼んでください",
                "policy_version": policy["version"],
                "next_action": {"kind": "invalid_argument",
                                "advice": f"{ADMIT_MAX_TRIGGERS} 件以下に分割してください",
                                "fallback_tools": []}}

    known = {_admit_key(r): r for r in _admit_load()}
    decisions: list[dict] = []
    fresh: list[dict] = []
    counts = {"accept": 0, "would_accept": 0, "ignored": 0, "replayed": 0, "errors": 0}
    needs: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            counts["errors"] += 1
            decisions.append({"error": "trigger はオブジェクトである必要があります"})
            continue
        trigger_id = S.as_str(item.get("trigger_id")).strip()
        source = S.as_str(item.get("source")).strip()
        if not trigger_id or not source:
            counts["errors"] += 1
            decisions.append({"trigger_id": trigger_id, "source": source,
                              "error": "source と trigger_id は必須です（冪等性の手がかりになる）"})
            continue
        key = _admit_key(item)
        previous = known.get(key)
        if previous:
            counts["replayed"] += 1
            decisions.append({**{k: previous.get(k) for k in
                                 ("trigger_id", "source", "thread", "actor", "disposition",
                                  "strength", "basis")},
                              "replayed": True, "needs_assessment": False,
                              "mandate": previous.get("mandate")})
            continue
        gate = _admit_gate(policy, item)
        disposition, why = _admit_disposition(gate["strength"], policy["accept_mode"])
        mandate = _admit_mandate(policy, item, gate["strength"])
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "trigger_id": trigger_id,
               "source": source, "thread": S.as_str(item.get("thread")),
               "trigger": S.as_str(item.get("trigger_id")), "actor": S.as_str(item.get("actor")),
               "kind": S.as_str(item.get("kind"), "mention"),
               "disposition": disposition, "strength": gate["strength"], "basis": gate["basis"],
               "why": why, "policy_version": policy["version"], "mandate": mandate}
        counts[disposition] += 1
        if disposition == "would_accept":
            needs.append(trigger_id)
        decisions.append(row)
        fresh.append(row)
    _admit_append(fresh)

    return {
        "action": "evaluate", "policy_version": policy["version"],
        "accept_mode": policy["accept_mode"], "counts": counts,
        "decisions": decisions, "needs_assessment": needs,
        "state": _admit_log_path(),
        "note": "作業の状態（作業中／完了）は KANBAN が持ち主です。ここは受付の裁定だけを返します。",
    }


def _render_admit(data: dict) -> str:
    """`freeagent_admit` の本文（人間向け日本語）。"""
    if "accepted" in data and "decisions" not in data:
        lines = [f"報告の検査: {'受理' if data.get('accepted') else '差し戻し'}"
                 f"（kind={data.get('kind') or '—'} / 証拠 {data.get('evidence_commands', 0)} 件）",
                 f"理由: {data.get('why')}"]
        if data.get("ask"):
            lines.append(f"→ {data['ask']}")
        return "\n".join(lines)
    if data.get("action") == "list_recent":
        lines = [f"記録の読み戻し: {data.get('count', 0)} 件（mode={data.get('accept_mode')} / "
                 f"ポリシー {data.get('policy_version')}）"]
        marks = {"accept": "✓", "would_accept": "◎", "ignored": "・"}
        for row in (data.get("decisions") or [])[:20]:
            when = S.as_str(row.get("at"))[5:16].replace("T", " ")
            card = S.as_str((row.get("mandate") or {}).get("card"))
            lines.append(f"  {marks.get(row.get('disposition'), '?')} {when} "
                         f"{S.as_str(row.get('trigger_id')) or '—'}（{S.as_str(row.get('source'))}）"
                         f" {row.get('disposition')}/{row.get('strength')}"
                         + (f" card={card}" if card else "")
                         + ("（再実行）" if row.get("replayed") else ""))
        if data.get("note"):
            lines.append("ℹ️ " + data["note"])
        return "\n".join(lines)
    counts = data.get("counts") or {}
    lines = [f"受付 {data.get('action', 'evaluate')} / ポリシー {data.get('policy_version')} "
             f"/ mode={data.get('accept_mode')}",
             f"裁定: 受付 {counts.get('accept', 0)} / 記録のみ {counts.get('would_accept', 0)} "
             f"/ 却下 {counts.get('ignored', 0)} / 再実行 {counts.get('replayed', 0)}"
             + (f" / エラー {counts['errors']}" if counts.get("errors") else "")]
    marks = {"accept": "✓", "would_accept": "◎", "ignored": "・"}
    for row in (data.get("decisions") or [])[:10]:
        if row.get("error"):
            lines.append(f"  ✗ {row.get('trigger_id') or '—'}: {row['error']}")
            continue
        lines.append(f"  {marks.get(row.get('disposition'), '?')} {row.get('trigger_id')}: "
                     f"{row.get('disposition')}/{row.get('strength')}（{row.get('basis')}）"
                     + ("（再実行）" if row.get("replayed") else ""))
    if data.get("needs_assessment"):
        lines.append("査読が要る候補: " + ", ".join(data["needs_assessment"][:5]))
    if data.get("note"):
        lines.append("ℹ️ " + data["note"])
    return "\n".join(lines)


EXPORTS = {
    "ADMIT_MODES": ADMIT_MODES,
    "ADMIT_REPORT_KINDS": ADMIT_REPORT_KINDS,
    "ADMIT_POLICY_FILE": ADMIT_POLICY_FILE,
    "ADMIT_LOG_FILE": ADMIT_LOG_FILE,
    "ADMIT_LOG_READ_MAX": ADMIT_LOG_READ_MAX,
    "ADMIT_MAX_TRIGGERS": ADMIT_MAX_TRIGGERS,
    "ADMIT_DEFAULT_POLICY": ADMIT_DEFAULT_POLICY,
    "_admit_policy_path": _admit_policy_path,
    "_admit_log_path": _admit_log_path,
    "admit_policy": admit_policy,
    "_admit_gate": _admit_gate,
    "_admit_disposition": _admit_disposition,
    "_admit_mandate": _admit_mandate,
    "_admit_key": _admit_key,
    "_admit_load": _admit_load,
    "_admit_append": _admit_append,
    "_admit_report": _admit_report,
    "_admit_recent": _admit_recent,
    "tool_admit": tool_admit,
    "_render_admit": _render_admit,
}
