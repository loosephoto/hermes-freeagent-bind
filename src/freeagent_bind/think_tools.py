"""§6.9/§6.10 freeagent_think の実装（計画・改訂・分岐・仮説・検証）

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import re

from . import server as S  # noqa: E402

def _think_step_label(step: dict) -> str:
    tags = []
    if step.get("branch_id"):
        tags.append(f"分岐 {step['branch_id']}")
    if step.get("is_revision"):
        tags.append(f"#{step['revises_thought']} の修正" if step.get("revises_thought") else "修正")
    if step.get("kind") == "hypothesis":
        tags.append(f"仮説・{step.get('hypothesis_status') or 'open'}")
    elif step.get("kind") == "test" and step.get("tests_hypothesis"):
        tags.append(f"仮説 #{step['tests_hypothesis']} の検証")
    elif step.get("kind") == "conclusion":
        tags.append("結論")
    return f"（{', '.join(tags)}）" if tags else ""


def _think_prompt(question: str, steps: list[dict], step: dict, row: dict | None = None) -> str:
    """検証者へ渡す材料。**解析済みの項目だけ**を渡す（原文の CoT をそのまま転送しない）。

    渡すのは**現行の道筋**（改訂済み・棄却分岐を除く）だけ。古い思考を混ぜると、検証者が既に
    撤回された前提を反証して予算を使う。改訂なら改訂前の文、仮説の検証なら対象の仮説を添える。
    """
    parts: list[str] = []
    n = S.as_int(step.get("n"), 0, 0, 9999)
    rev = S.as_int(step.get("revises_thought"), 0, 0, 9999)
    by_n = {S.as_int(s.get("n"), 0, 0, 9999): s for s in steps}
    # 改訂の印（superseded_by）は統合時＝検証の後に付くので、今回の改訂対象もここで外す。
    active = [s for s in _think_active(steps, row) if S.as_int(s.get("n"), 0, 0, 9999) not in (n, rev)]
    if question:
        parts.append(f"【問い】\n{S.truncate(question, 800)}")
    plan = [p for p in ((row or {}).get("plan") or []) if isinstance(p, dict)]
    if plan:
        parts.append("【計画（サブ目標）】\n" + "\n".join(
            f"{p.get('id')}. {S.truncate(p.get('text') or '', 160)}" + ("（済）" if p.get("done_at") else "")
            for p in plan))
    if active:
        block = "\n".join(
            f"- #{s.get('n')}{_think_step_label(s)}: {S.truncate(s.get('text') or '', 400)}"
            for s in active[-6:])
        omitted = len([s for s in steps if S.as_int(s.get("n"), 0, 0, 9999) != n]) - len(active)
        tail = f"\n（改訂・棄却で外した思考 {omitted} 件は省略）" if omitted > 0 else ""
        parts.append(f"【これまでの思考（現行の道筋）】\n{block}{tail}")
    rev_block = rev if rev in by_n else 0
    if rev_block:
        parts.append(f"【改訂前の思考 #{rev}】\n{S.truncate(by_n[rev].get('text') or '', 600)}")
    hyp = S.as_int(step.get("tests_hypothesis"), 0, 0, 9999)
    if hyp and hyp in by_n:
        parts.append(f"【検証対象の仮説 #{hyp}】\n{S.truncate(by_n[hyp].get('text') or '', 600)}")
    parts.append(f"【検証対象の思考 #{step.get('n')}{_think_step_label(step)}】\n"
                 f"{S.truncate(step.get('text') or '', 1200)}")
    parts.append("上の形式（判定/反証/見落とし/確信度）だけで答えてください。")
    return "\n\n".join(parts)


def _think_ledger_view(steps: list[dict], *, keep: int = 8, row: dict | None = None,
                       brief: bool = False) -> dict:
    """台帳の要約（計画・分岐・修正・仮説・最新の思考）。**全文は返さず末尾だけ**を載せる。

    `brief=True` は**現行の道筋だけ**を載せる（改訂済み・棄却分岐の本文を省く。文脈圧縮後の復帰用）。
    省いた件数は `omitted` に残す（黙って隠さない）。
    """
    row = row or {}
    meta = row.get("branch_meta") if isinstance(row.get("branch_meta"), dict) else {}
    branches: dict[str, list[int]] = {bid: [] for bid in meta}
    for step in steps:
        if step.get("branch_id"):
            branches.setdefault(step["branch_id"], []).append(S.as_int(step.get("n"), 0, 0, 9999))
    active = _think_active(steps, row)
    plan = [p for p in (row.get("plan") or []) if isinstance(p, dict)]
    retired = [p for p in (row.get("plan_retired") or []) if isinstance(p, dict)]
    shown = active if brief else steps[-keep:]
    out = {
        "steps_recorded": len(steps),
        "branches": [{"branch_id": bid, "steps": ns,
                      "from": (meta.get(bid) or {}).get("from"),
                      "status": (meta.get(bid) or {}).get("status") or "open",
                      "resolved_at": (meta.get(bid) or {}).get("resolved_at")}
                     for bid, ns in branches.items()],
        "branch_points": sorted({S.as_int(s.get("branch_from_thought"), 0, 0, 9999)
                                 for s in steps if s.get("branch_from_thought")}),
        "revisions": [{"step": S.as_int(s.get("n"), 0, 0, 9999),
                       "revises": s.get("revises_thought")}
                      for s in steps if s.get("is_revision")],
        # 改訂で置き換わった思考（消さずに印だけ付ける）と、現行の道筋（改訂済み・棄却分岐を除く）。
        "superseded": [{"step": S.as_int(s.get("n"), 0, 0, 9999), "by": s.get("superseded_by")}
                       for s in steps if s.get("superseded_by")],
        "active_path": [S.as_int(s.get("n"), 0, 0, 9999) for s in active],
        # `id` は**同一性**（差し替えをまたいで不変）／`position` は今の plan での並び順（1 始まり）。
        # `subgoal` はこの id を指す（§6.10）。
        "plan": [{"id": p.get("id"), "position": i + 1,
                  "text": S.truncate(p.get("text") or "", 200),
                  "done": bool(p.get("done_at")), "done_at": p.get("done_at"),
                  "steps": [S.as_int(s.get("n"), 0, 0, 9999) for s in steps
                            if S.as_int(s.get("subgoal"), 0, 0, 999) == p.get("id")]}
                 for i, p in enumerate(plan)],
        # 計画から抜けた項目（廃止）。**記録を黙って消さない**（§2.7）。
        "plan_retired": [{"id": p.get("id"), "position": None,
                          "text": S.truncate(p.get("text") or "", 200),
                          "done": bool(p.get("done_at")), "done_at": p.get("done_at"),
                          "steps": [S.as_int(s.get("n"), 0, 0, 9999) for s in steps
                                    if S.as_int(s.get("subgoal"), 0, 0, 999) == p.get("id")]}
                         for p in retired],
        "plan_progress": {"done": sum(1 for p in plan if p.get("done_at")), "total": len(plan)},
        # 廃止した達成済みの件数。`plan_progress` は**今の plan の項目だけ**を数えるので、
        # 達成済みの項目を plan から外すと進捗が減って見える（実測: 1/3 → 0/3）。
        # 数を消さずに別枠で出し、描画側でも明示する（§6.16）。
        "plan_retired_done": sum(1 for p in retired if p.get("done_at")),
        "hypotheses": [{"n": S.as_int(s.get("n"), 0, 0, 9999), "text": S.truncate(s.get("text") or "", 200),
                        "status": s.get("hypothesis_status") or "open",
                        "tested_by": s.get("tested_by") or [],
                        "superseded_by": s.get("superseded_by"),
                        "verdicts": ((s.get("verification") or {}).get("verdicts"))}
                       for s in steps if s.get("kind") == "hypothesis"],
        "latest": [{"n": S.as_int(s.get("n"), 0, 0, 9999), "branch_id": s.get("branch_id"),
                    "is_revision": bool(s.get("is_revision")),
                    "kind": s.get("kind") or "step",
                    "superseded_by": s.get("superseded_by"),
                    "hypothesis_status": s.get("hypothesis_status"),
                    "tests_hypothesis": s.get("tests_hypothesis"),
                    "subgoal": s.get("subgoal"),
                    "text": S.truncate(s.get("text") or "", 200 if brief else 300)} for s in shown],
        # 直近に宣言された見積り総数（増減してよい・調整はメインが行う）。台帳に残して履歴化する。
        "total_thoughts": _think_latest_total(steps),
        "total_history": [h for h in (row.get("total_history") or []) if isinstance(h, dict)],
    }
    if brief:
        active_ns = {S.as_int(s.get("n"), 0, 0, 9999) for s in active}
        out["brief"] = True
        # 省いた件数は隠さない（「要約だから無い」と「そもそも無い」を混同させない）。
        out["omitted"] = sum(1 for s in steps if S.as_int(s.get("n"), 0, 0, 9999) not in active_ns)
    return out


def _think_suggestions(data: dict, steps: list[dict], needed: bool) -> list[str]:
    """次の一手を**数値から**生成する。LLM 向けの助言なので structuredContent にだけ置く（規約 3）。"""
    out: list[str] = []
    verify = data.get("verification")
    if verify:
        if verify["verdicts"]["要修正"]:
            out.append(f"判定に「要修正」が {verify['verdicts']['要修正']} 件あります。"
                       "修正ステップ（is_revision=true, revises_thought=#n）を推奨します。")
        if verify["verdicts"]["根拠不足"]:
            out.append(f"判定に「根拠不足」が {verify['verdicts']['根拠不足']} 件あります。"
                       "freeagent_lookup / freeagent_grounded で根拠を取ってから再検証してください。")
        if not verify["answered"]:
            out.append("独立した検証が得られていません（この思考を『検証済み』として扱わないでください）。")
        elif verify["failed"]:
            out.append(f"検証 {verify['failed']} 体が脱落しました（failed_rows に残しています）。")
        if verify["objections"]:
            out.append(f"未解決の反証が {len(verify['objections'])} 件あります。"
                       "結論の前に潰すか、未解決点として残してください。")
    ledger = data["ledger"]
    open_branches = [b["branch_id"] for b in ledger["branches"] if b.get("status", "open") == "open"]
    if len(open_branches) > 1 or (open_branches and not needed):
        out.append(f"未決着の分岐が {len(open_branches)} 本あります（{', '.join(open_branches)}）。"
                   "統合ステップを積むか、resolve_branch と branch_status（adopted/abandoned/merged）で"
                   "決着を記録してください。")
    untested = [h["n"] for h in ledger.get("hypotheses") or []
                if h.get("status") == "open" and not h.get("tested_by") and not h.get("superseded_by")]
    if untested:
        out.append(f"未検証の仮説があります（{', '.join(f'#{x}' for x in untested)}）。"
                   "tests_hypothesis=#n と hypothesis_status で検証結果を記録するか、verify=true で"
                   "独立モデルの反証を取ってください。")
    plan = ledger.get("plan") or []
    est = data.get("total_thoughts")
    # 構造を使っていない台帳への促し（常用の規則を台帳側でも補強する。数値から生成・判断はメイン）。
    if needed and ledger["steps_recorded"] == 1 and not plan:
        out.append("計画がありません。複数段の問題なら plan でステップ（サブ目標）に分解してください"
                   "（1 問 1 答で済むなら不要です）。")
    if (needed and ledger["steps_recorded"] == 3 and not ledger.get("hypotheses")
            and not ledger["branches"] and not ledger.get("revisions")):
        out.append(f"{ledger['steps_recorded']} ステップ積みましたが、仮説・分岐・改訂がありません。"
                   "前提や原因は kind=hypothesis で立てて検証し、別案は branch_from_thought で分岐して"
                   "比べ、考えが変わった前のステップは revises_thought で改訂してください。")
    if plan and est and len(plan) > est:
        out.append(f"計画は {len(plan)} 項目ですが見積り総数は {est} です。total_thoughts の見直しを推奨します。")
    pending = [p["id"] for p in plan if not p.get("done")]
    if pending and not needed:
        out.append(f"計画の未達が {len(pending)} 項目あります（{', '.join(str(x) for x in pending)}）。"
                   "結論にするなら未達の理由を残してください。")
    if needed and len(steps) >= S.THOUGHT_MAX_STEPS:
        out.append(f"思考数が上限（{S.THOUGHT_MAX_STEPS}）です。新しい session_id で台帳を分けてください。")
    if needed and est and ledger["steps_recorded"] >= est:
        out.append(f"見積り総数（{est}）に達しました。続けるなら total_thoughts を増やしてください"
                   "（据え置き・減らすのも可。総数はメインが動的に調整します）。")
    if not needed:
        out.append("next_thought_needed=false。最終結論はメインで確定してください。")
    if ledger.get("steps_recorded", 0) >= 12 and not data.get("brief"):
        out.append(f"台帳が {ledger['steps_recorded']} ステップあります。読み戻すときは view=true に "
                   "brief=true を併用すると、現行の道筋だけを軽く読めます。")
    return out


def _think_verify_block(args: dict, question: str, steps: list, step: dict,
                       preview: dict, used: list, sid_in: str):
    """`verify=true` の検証を実行して (verification, used, failure) を返す。

    `failure` は**環境障害で台帳を汚さずに終わらせる場合の応答**（`None` なら正常）。
    検証は独立モデルによる反証探索であり、合意の収集ではない（§7.2）。
    """
    refs, info = S.select_models(S.as_int(args.get("size"), 2, 1, 4),
                               S.as_str_list(args.get("models")) or None,
                               prefer=S.as_str_list(args.get("prefer")) or None,
                               exclude=S.as_str_list(args.get("exclude")) or None,
                               identity_exclude=used)
    if not refs:
        return None, used, S._no_models()   # 状態を書かない（環境障害で台帳を汚さない）
    results = S.ask_many(refs, _think_prompt(question, steps, step, preview), system=S.THINK_CRITIC_SYSTEM,
                       max_tokens=S.as_int(args.get("max_tokens"), 400, 16, 2000), kind="think",
                       avoid=S.as_str_list(args.get("exclude")) or None, identity_avoid=used)
    rows: list[dict] = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            rows.append({"model": ref, "error": res["error"]})
            continue
        parsed = S.parse_verdict(res["text"])
        rows.append({"model": ref, "served_by": res.get("served_by"),
                     "verdict": parsed["verdict"], "objection": parsed["objection"],
                     "oversight": parsed["oversight"], "confidence": parsed["confidence"],
                     "labels_found": parsed["labels_found"],
                     "text": S.truncate(res["text"], 600), "cot_leak": res.get("cot_leak")})
    answered = [r for r in rows if not r.get("error")]
    failed = [r for r in rows if r.get("error")]
    # 環境障害で 1 体も応答していないなら**書かない**（規約 21）。モデル側の失敗（429 等）は
    # 従来どおり記録し、`answered: 0` として隠さず返す。
    if not answered and failed and all(S.is_env_failure(r.get("error") or "") for r in failed):
        return None, used, {"error": "検証に到達できませんでした（バックエンド不通: "
                          f"{S.truncate(failed[0].get('error') or '', 120)}）。"
                          "この思考は台帳に記録していません。",
                  "session_id": sid_in or None, "verifier_models": refs,
                  "failures": [{"model": r["model"], "error": S.truncate(r.get("error") or "", 160)}
                               for r in failed]}
    confs = [r["confidence"] for r in answered if isinstance(r.get("confidence"), int)]
    verification = {
        "models": refs, "selection": info, "answered": len(answered), "failed": len(failed),
        "verdicts": {v: sum(1 for r in answered if r.get("verdict") == v)
                     for v in ("妥当", "要修正", "根拠不足")},
        "unlabeled": sum(1 for r in answered if not r.get("verdict")),
        "confidence_mean": round(sum(confs) / len(confs), 1) if confs else None,
        "objections": [{"model": r["model"], "objection": r["objection"]}
                       for r in answered if r.get("objection")],
        "oversights": [{"model": r["model"], "oversight": r["oversight"]}
                       for r in answered if r.get("oversight")],
        "answers": rows,
        "failed_rows": [{"model": r["model"], "error": S.truncate(r.get("error") or "", 160)}
                        for r in failed],
        "agreement": S.agreement_of([r.get("verdict") or "" for r in answered if r.get("verdict")]),
        "note": "検証は独立モデルによる反証探索です。合意は正しさの保証ではありません。",
    }
    # `used` には**要求したモデルだけでなく実際に答えたモデル（`served_by`）**も入れる。
    # 要求名だけだと、`longcat` が `laguna-s-2.1` へフォールバックした直後に、同じ呼び出しの
    # 「代替案の提案者」が再び `laguna-s-2.1` に決まり、検証者と提案者が同一モデルになる
    # （実測。規約 26 の「検証者とも別のモデル」が破れる）。
    served: list[str] = []
    for row in rows:
        for name in (row.get("model"), row.get("served_by")):
            if isinstance(name, str) and name and name not in served:
                served.append(name)
    return verification, list(dict.fromkeys(used + served)), None


def tool_think(args: dict) -> dict:
    """メインの思考ステップを台帳に積み、任意で独立モデルに反証させる。

    分解（`plan` / `subgoal`）・改訂（`revises_thought` → 元ステップに `superseded_by`）・
    分岐（`branch_from_thought` / `branch_id` / `resolve_branch`）・仮説（`kind=hypothesis` /
    `tests_hypothesis`）・見積り総数の動的調整（`total_thoughts`）を台帳の**構造**として持つ。
    参照先の番号・分岐・仮説は**サブ呼び出しの前に**検証し、無ければ推測で繋がずエラーで返す。

    `verify=true` を付けたステップだけ、生成者とは別の Free モデルが「判定/反証/見落とし」を返す。
    `propose_alternatives=true` なら、さらに別のモデルが**この道筋とは異なる代替案**を返す（記録は
    メインが選ぶ）。どちらも**同意の収集ではない**。バックエンドへ到達できなかった場合は**台帳に
    書かない**（検証されていない前提の上に次の思考を積まないため。規約 21）。
    """
    if not S.THOUGHTS_ENABLED:
        return {"error": "思考台帳は無効です（FREEAGENT_THOUGHTS=0 で停止中）", "enabled": False}
    sid_in = S.as_str(args.get("session_id"))
    card = S.truncate(S.as_str(args.get("card")).strip(), 64)
    # card だけでも同じ台帳を継続できる（KANBAN のワーカーはカード ID しか持たない。実測: session_id を
    # 省略した 2 手目が新しい台帳になり、積んだ道筋が分断された）。明示の session_id が常に優先。
    resumed_by_card = False
    if not sid_in and card:
        sid_in = S.thought_find_by_card(card)
        resumed_by_card = bool(sid_in)
    stored = S.thought_get(sid_in) if sid_in else None
    if S.as_flag(args.get("view")):
        # カードから読み戻す（KANBAN のワーカーが自分のカードの思考を引く経路）。session_id が優先。
        if not sid_in and card:
            sid_in = S.thought_find_by_card(card)
            if not sid_in:
                return {"error": f"カード {card} に紐づく台帳はありません"
                                 "（同じ card を付けて思考を積むと紐づきます）",
                        "view": True, "card": card}
            stored = S.thought_get(sid_in)
        return _think_view(sid_in, stored, brief=S.as_flag(args.get("brief")))
    thought = S.as_str(args.get("thought"))
    if not thought:
        return {"error": "thought は必須です（空文字は不可。台帳を読むだけなら view=true と session_id）"}

    steps = [s for s in ((stored or {}).get("steps") or []) if isinstance(s, dict)]
    notes: list[str] = []
    if resumed_by_card:
        notes.append(f"card {card} に紐づく台帳を継続しました（session_id={sid_in}）。")
    if S.as_flag(args.get("brief")):
        notes.append("brief は view=true のときだけ効きます（思考の記録には影響しません）。")
    if sid_in and stored is None:
        notes.append(f"セッション {S.truncate(sid_in, 40)} は見つかりません（期限切れ／未知）。"
                     "新しい台帳を開始しました（古い思考は復活させません）。")

    explicit_number = args.get("thought_number") is not None
    n = S.as_int(args.get("thought_number"), len(steps) + 1, 1, 9999)
    existing = {S.as_int(s.get("n"), 0, 0, 9999) for s in steps}
    if n not in existing and len(steps) >= S.THOUGHT_MAX_STEPS:
        return {"error": f"思考数が上限（{S.THOUGHT_MAX_STEPS}）に達しています。新しい session_id で"
                         "台帳を分けるか、FREEAGENT_THOUGHT_MAX_STEPS を上げてください",
                "session_id": sid_in or None, "steps_recorded": len(steps)}

    # 構造の検証は**サブ呼び出しの前**（参照先が無い呼び出しで検証者の予算を使わない）。
    problem, st = _think_structure(args, steps, n, stored or {})
    if problem:
        problem.update({"session_id": sid_in or None, "steps_recorded": len(steps)})
        return problem
    notes.extend(st["notes"])

    # 見積り総数の動的調整: 省略時は台帳の見積りを引き継ぎ、番号が見積りを超えたら引き上げる。
    declared = S.as_int(args.get("total_thoughts"), 0, 0, 999)
    total = declared or _think_latest_total(steps) or 0
    total_auto = False
    if total and n > total:
        notes.append(f"思考 #{n} が見積り総数 {total} を超えたため、見積りを {n} へ引き上げました。")
        total, total_auto = n, True

    step = {
        "n": n, "text": S.truncate(thought, S._THOUGHT_CHARS),
        "kind": st["kind"],
        "branch_id": st["branch_id"],
        "is_revision": st["is_revision"],
        "revises_thought": st["revises"] or None,
        "branch_from_thought": st["branch_from"] or None,
        "tests_hypothesis": st["tests_hypothesis"] or None,
        "result_status": st["hypothesis_status"] or None,
        "subgoal": st["subgoal"] or None,
        "subgoal_done": st["subgoal_done"],
        "total_thoughts": total or None,
        "at": S.now_ts(), "verification": None,
    }
    if st["kind"] == "hypothesis":
        step["hypothesis_status"] = "open"
    used = [m for m in ((stored or {}).get("verifier_models") or []) if isinstance(m, str)]
    previous_used = list(used)
    previous_generation = (stored or {}).get("verifier_generation", 0)
    explicit_question = S.as_str(args.get("question"))
    question = explicit_question or (stored or {}).get("question") or ""
    # 検証者へ渡す台帳には、今回の呼び出しで決まる計画・分岐の決着を先に反映して見せる。
    preview = _think_preview_row(stored or {}, st)

    verification = None
    if S.as_flag(args.get("verify")):
        # 検証ブロックは `_think_verify_block` へ分けた（`tool_think` の巨大化対策。§7.2）。
        verification, used, failure = S._think_verify_block(
            args, question, steps, step, preview, used, sid_in)
        if failure is not None:
            return failure     # 状態を書かない（環境障害で台帳を汚さない。規約 21）
        step["verification"] = verification

    alternatives = None
    if S.as_flag(args.get("propose_alternatives")):
        alternatives = _think_alternatives(args, question, steps, step, preview, exclude=used)
        if alternatives.get("error"):
            alternatives.update({"session_id": sid_in or None})
            return alternatives    # 状態を書かない（規約 21）
        step["alternatives"] = alternatives
        alt_served = [name for row in alternatives.get("answers", []) if isinstance(row, dict)
                      for name in (row.get("model"), row.get("served_by"))
                      if isinstance(name, str) and name]
        used = list(dict.fromkeys(used + list(alternatives.get("models") or []) + alt_served))

    # Merge and rotate against the latest history while holding the ledger lock.
    selected = verification is not None or alternatives is not None
    selected_models = []
    for result in (verification, alternatives):
        if result is not None:
            selected_models.extend(result.get("models") or [])
            selected_models.extend(name for answer in result.get("answers", []) if isinstance(answer, dict)
                                   for name in (answer.get("model"), answer.get("served_by"))
                                   if isinstance(name, str) and name)

    # 台帳への統合は**ロック内**で行う（番号の採番と、他ステップ・メタへの波及も含む。
    # 並列呼び出しで片方が消えるのを防ぐ）。検証プロンプトに載せた `#n` は採番前の見積りなので、
    # 並列時は 1 ずれることがある（返り値は統合後の確定番号を使う）。
    ops = {"revises": st["revises"], "tests_hypothesis": st["tests_hypothesis"],
           "hypothesis_status": st["hypothesis_status"],
           "resolve_branch": st["resolve_branch"], "branch_status": st["branch_status"],
           "plan": st["plan"], "subgoal_done": st["subgoal_done"],
           "total": total, "total_auto": total_auto}
    if st["subgoal"]:
        ops["subgoal_check"] = {"raw": st["subgoal_raw"],
                                "identity": _thought_subgoal_identity(preview, st["subgoal"])}
    merged = S.thought_merge(sid_in, step, question=explicit_question, verifier_models=used if selected else None, card=card,
                           assign_number=not explicit_number, ops=ops,
                           verifier_snapshot=previous_used if selected else None,
                           verifier_additions=selected_models if selected else None,
                           verifier_snapshot_generation=previous_generation if selected else None,
                           require_existing=stored is not None)
    if merged.get("error"):
        return merged
    if merged.get("refused"):
        return {"error": f"思考数が上限（{S.THOUGHT_MAX_STEPS}）に達しています。新しい session_id で"
                         "台帳を分けるか、FREEAGENT_THOUGHT_MAX_STEPS を上げてください",
                "session_id": merged["session_id"], "steps_recorded": merged["steps_recorded"]}
    if merged.get("verifier_omitted"):
        notes.append(f"検証者の古い履歴を{merged['verifier_omitted']}件省略しました。古いモデルは次回以降に再選択され得ます。")
    sid = merged["session_id"]
    step = merged["step"] or step
    n = S.as_int(step.get("n"), n, 1, 9999)
    if merged.get("replaced"):
        notes.append(f"思考 #{n} は既にありました。置き換えました（同じ番号の再送は上書き）。")
    after = S.thought_get(sid) or {}
    steps_out = [s for s in (after.get("steps") or []) if isinstance(s, dict)]
    used = [m for m in (after.get("verifier_models") or used) if isinstance(m, str)]

    # 「続けるか」。**明示された null は「未指定」扱い**（既定 True へ戻す）。
    # `as_flag(None)` は False になるので素朴に書くと、クライアントが null を送っただけで
    # 台帳が暗黙に終端する（実測: null → False / キー無し → True）。
    needed = _think_needed_flag(args)
    data = {
        "session_id": sid, "step": n, "text": step["text"], "kind": step.get("kind") or "step",
        "branch_id": step["branch_id"], "is_revision": step["is_revision"],
        "revises_thought": step["revises_thought"],
        "branch_from_thought": step["branch_from_thought"],
        "tests_hypothesis": step.get("tests_hypothesis"),
        "hypothesis_status": step.get("result_status") or step.get("hypothesis_status"),
        "subgoal": step.get("subgoal"),
        # 解決前（呼び出し側の指定）と解決後（plan 項目の id）の両方を出す。
        "subgoal_ref": st.get("subgoal_raw") or None,
        "total_thoughts": total or None,
        "total_auto_adjusted": total_auto,
        "next_thought_needed": needed,
        "ledger": _think_ledger_view(steps_out, row=after),
        "verification": verification,
        "verified": bool(verification and verification["answered"]),
        "alternatives": alternatives,
        "verifier_models": used,
        "notes": notes,
        # 終端（next_thought_needed=false）のときは**次の思考を誘導しない**。
        # 「続けるか」を False にしておきながら next_call を出すと、呼び出し側が
        # 勝手に次を積んでしまう（実測: 終端なのに「<次の思考>」が出ていた）。
        "privacy_note": "台帳は思考の記録です。判断と責任はメインLLMに残ります。",
    }
    if needed:
        data["next_call"] = {"tool": "freeagent_think",
                             "args": {"session_id": sid, "thought": "<次の思考>",
                                      "thought_number": n + 1, "verify": bool(verification),
                                      **({"total_thoughts": max(total, n + 1)} if total else {}),
                                      **({"card": card} if card else {})}}
    else:
        data["next_call"] = None
    if card:
        data["card"] = card          # KANBAN のカードへの紐づけ（引けたことを呼び出し側に見せる）
    data["suggestions"] = _think_suggestions(data, steps_out, needed)
    return data


THINK_ALT_SYSTEM = (
    "あなたは代替案の提案者です。与えられた思考の道筋とは**異なる**仮説・解法・道筋を挙げてください。"
    "出力は次の形式の行だけです（最大3行）。\n"
    "代替: <1〜2文。どこが今の道筋と違うかが分かるように>\n"
    "今の道筋の言い換えや賛成は書かない。無ければ「代替: なし」の1行だけ。"
    "前置き・思考過程・挨拶・Markdown の見出しは書かない。"
)


_LABEL_ALT = re.compile(
    r"^(?:(?:[-*・•]|\d+[.)．、])\s*)?(?:\*\*)?(?:代替案?|対立仮説|alternative)\s*\d*(?:\*\*)?\s*[:：]\s*(.+)",
    re.I)


_BULLET = re.compile(r"^(?:[-*・•]|\d+[.)．、])\s+(.+)")


def parse_alternatives(text, limit: int = 3, *, truncated: bool = False) -> list[str]:
    """提案者の「代替: …」行を取り出す。ラベルが 1 つも無ければ箇条書きだけを拾う（推測で埋めない）。

    `truncated`（max_tokens で打ち切られた応答）のときは、**本文の最終行にあたる案**を捨てる。
    打ち切りは最終行の途中で起きるので、それを案として渡すと文の途中で切れた提案が
    完全な案として台帳に残る（実測「代替: 親プロセスのコマン」）。
    """
    lines = [ln.strip() for ln in (text if isinstance(text, str) else "").splitlines() if ln.strip()]
    if truncated and lines:
        lines = lines[:-1]
    labeled = [m.group(1) for m in (_LABEL_ALT.match(ln) for ln in lines) if m]
    picked = labeled or [m.group(1) for m in (_BULLET.match(ln) for ln in lines) if m]
    out: list[str] = []
    for value in picked:
        value = value.strip().strip("* ")
        if value and not S._is_no_answer(value):
            out.append(S.truncate(value, 300))
        if len(out) >= limit:
            break
    return out


def _think_latest_total(steps: list[dict]) -> int | None:
    return next((S.as_int(s.get("total_thoughts"), 0, 0, 999) for s in reversed(steps)
                 if S.as_int(s.get("total_thoughts"), 0, 0, 999)), None)


def _think_active(steps: list[dict], row: dict | None) -> list[dict]:
    """現行の道筋: 改訂で置き換わった思考と、棄却（abandoned）された分岐の思考を除いたもの。"""
    meta = (row or {}).get("branch_meta")
    meta = meta if isinstance(meta, dict) else {}
    dropped = {bid for bid, m in meta.items() if isinstance(m, dict) and m.get("status") == "abandoned"}
    return [s for s in steps if not s.get("superseded_by") and s.get("branch_id") not in dropped]


def _think_preview_row(row: dict, st: dict) -> dict:
    """今回の呼び出しで決まる計画・分岐の決着を反映した台帳メタの写し（検証者に見せる用。書かない）。"""
    out = S._deep_copy({k: row.get(k) for k in ("plan", "plan_retired", "branch_meta")})
    if st.get("plan"):
        # 実際のマージと同じ id 割当てにする（検証者に別の plan を見せない）。
        old_plan = [p for p in (out.get("plan") or []) if isinstance(p, dict)]
        old_retired = [p for p in (out.get("plan_retired") or []) if isinstance(p, dict)]
        merged, retired = S._plan_merge(old_plan, old_retired, list(st["plan"]))
        out["plan"] = merged
        out["plan_retired"] = retired
    meta = out.get("branch_meta") if isinstance(out.get("branch_meta"), dict) else {}
    if st.get("resolve_branch") and st["resolve_branch"] in meta:
        meta[st["resolve_branch"]]["status"] = st["branch_status"]
    out["branch_meta"] = meta
    return out


def _thought_subgoal_identity(preview: dict, subgoal: int):
    for key in ("plan", "plan_retired"):
        for item in preview.get(key) or []:
            if isinstance(item, dict) and S.as_int(item.get("id"), 0, 0, 9999) == subgoal:
                return (subgoal, S.as_str(item.get("text")), key)
    return None


def _thought_subgoal_still_matches(row: dict, steps: list[dict], step: dict, ops: dict) -> bool:
    check = ops.get("subgoal_check")
    if not check:
        return True
    problem, current = _think_structure({"plan": ops.get("plan"), "subgoal": check["raw"],
                                         "subgoal_done": ops.get("subgoal_done")},
                                        steps, S.as_int(step.get("n"), 0, 0, 9999), row)
    if problem:
        return False
    identity = _thought_subgoal_identity(_think_preview_row(row, current), current["subgoal"])
    return identity is not None and identity == check["identity"]


def _think_structure(args: dict, steps: list[dict], n: int, row: dict) -> tuple[dict | None, dict]:
    """構造引数を検証・正規化する。問題があれば `({"error": ...}, {})` を返す（台帳は書かない）。"""
    notes: list[str] = []
    by_n = {S.as_int(s.get("n"), 0, 0, 9999): s for s in steps}
    known = sorted(k for k in by_n if k != n)
    meta = row.get("branch_meta") if isinstance(row.get("branch_meta"), dict) else {}

    def fail(msg: str) -> tuple[dict, dict]:
        return {"error": msg, "known_thoughts": known, "known_branches": sorted(meta)}, {}

    kind_raw = S.as_str(args.get("kind")).strip().lower()
    if kind_raw and kind_raw not in S.THOUGHT_KINDS:
        return fail(f"kind は {' / '.join(S.THOUGHT_KINDS)} のいずれかです（受け取った値: {S.truncate(kind_raw, 40)}）")
    tests = S.as_int(args.get("tests_hypothesis"), 0, 0, 9999)
    kind = kind_raw or ("test" if tests else "step")

    # 改訂: 元ステップは消さず、統合時に `superseded_by` を付ける（§2.7）。
    revises = S.as_int(args.get("revises_thought"), 0, 0, 9999)
    is_revision = S.as_flag(args.get("is_revision")) or bool(revises)
    if is_revision and not revises:
        return fail("is_revision=true には revises_thought（改訂する思考の番号）が必要です")
    if revises and revises not in known:
        return fail(f"revises_thought=#{revises} は台帳にありません（自分自身は改訂できません）")
    if revises and by_n[revises].get("superseded_by") not in (None, n):
        notes.append(f"#{revises} は既に #{by_n[revises]['superseded_by']} で改訂済みです（改訂を重ねます）。")

    # 分岐: 新しい分岐には分岐元が要る。既存の分岐は分岐元を引き継ぐ。
    bid = S.as_str(args.get("branch_id")).strip()[:40]
    bfrom = S.as_int(args.get("branch_from_thought"), 0, 0, 9999)
    if bfrom and bfrom not in known:
        return fail(f"branch_from_thought=#{bfrom} は台帳にありません")
    if bid and bid in meta:
        origin = S.as_int((meta[bid] or {}).get("from"), 0, 0, 9999)
        if bfrom and origin and bfrom != origin:
            notes.append(f"分岐 {bid} の分岐元は #{origin} のままです（branch_from_thought=#{bfrom} は使いません）。")
        bfrom = origin or bfrom
        state = (meta[bid] or {}).get("status") or "open"
        if state != "open":
            notes.append(f"分岐 {bid} は {state} で決着済みです（状態は変えずに記録しました。"
                         "再開は resolve_branch と branch_status=open）。")
    elif bid and not bfrom and not any(s.get("branch_id") == bid for s in steps):
        return fail(f"新しい分岐 {bid} には branch_from_thought（分岐元の思考番号）が必要です")
    elif bfrom and not bid:
        k = len(meta) + 1
        while f"b{k}" in meta:
            k += 1
        bid = f"b{k}"
        notes.append(f"branch_id が省略されたため、#{bfrom} からの分岐に {bid} を割り当てました。")

    # 分岐の決着（採用・棄却・統合）。棄却した分岐は「現行の道筋」から外れる。
    resolve = S.as_str(args.get("resolve_branch")).strip()[:40]
    bstatus = S.as_str(args.get("branch_status")).strip().lower()
    if bstatus and bstatus not in S.BRANCH_STATES:
        return fail(f"branch_status は {' / '.join(S.BRANCH_STATES)} のいずれかです")
    if bstatus and not resolve:
        return fail("branch_status には resolve_branch（決着させる分岐の ID）が必要です")
    if resolve and not bstatus:
        return fail("resolve_branch には branch_status（adopted / abandoned / merged / open）が必要です")
    if resolve and resolve not in meta and resolve != bid:
        return fail(f"分岐 {S.truncate(resolve, 40)} は台帳にありません")

    # 仮説の検証: 対象は kind=hypothesis のステップに限る。
    hstatus = S.as_str(args.get("hypothesis_status")).strip().lower()
    if hstatus and hstatus not in S.HYPOTHESIS_STATES:
        return fail(f"hypothesis_status は {' / '.join(S.HYPOTHESIS_STATES)} のいずれかです")
    if hstatus and not tests:
        return fail("hypothesis_status には tests_hypothesis（検証する仮説の番号）が必要です")
    if tests and tests not in known:
        return fail(f"tests_hypothesis=#{tests} は台帳にありません")
    if tests and by_n[tests].get("kind") != "hypothesis":
        return fail(f"#{tests} は仮説（kind=hypothesis）として記録されていません")
    if tests and kind == "hypothesis":
        return fail("仮説の検証ステップは kind=test です（仮説そのものは kind=hypothesis で別に積みます）")

    # 分解: 計画（サブ目標）は再送で改訂できる。上限超過は切り捨てずエラー（規約 63 の例外ではなく
    # 「多すぎる入力をそのまま受け取らない」契約なので、件数を返すのではなく fail で止める）。
    # schema にも maxItems を宣言して、呼び出し側が事前に知れるようにする。
    plan = [S.truncate(p, 200) for p in S.as_str_list(args.get("plan"))]
    if len(plan) > S.THOUGHT_PLAN_MAX:
        return fail(f"plan は {S.THOUGHT_PLAN_MAX} 項目までです（{len(plan)} 項目）。粒度を上げて分けてください")
    old_plan = [p for p in (row.get("plan") or []) if isinstance(p, dict)]
    old_retired = [p for p in (row.get("plan_retired") or []) if isinstance(p, dict)]
    if plan:
        # 差し替え後の plan で id を振り直す（実際のマージと同じ割当て）。
        plan_now, _retired = S._plan_merge(old_plan, old_retired, plan)
    else:
        plan_now = old_plan
    # `subgoal` の解決（§6.10）。**番号の解釈は 1 つだけ**にする:
    #   1) 現在の plan の**id**に一致 → その項目（同一性。差し替えをまたいでも不変）
    #   2) 現在の plan の**1 始まりの位置** → その項目（id が一致しない番号は位置として扱う）
    #   3) 廃止済み項目の id → その項目（記録は残す。達成済みの印は付けない）
    # どれにも当たらなければエラー（推測で繋がない）。
    # 旧実装は id だけで照合していたため、差し替えの後は位置と id がずれて
    # **位置 1 の項目ではなく廃止済み項目を完了扱い**にしていた（実測: 20 回反復で 20 回再現）。
    sub_raw = S.as_int(args.get("subgoal"), 0, 0, 999)
    sub = 0
    sub_kind = ""
    if sub_raw:
        live_ids = [S.as_int(p.get("id"), 0, 0, 9999) for p in plan_now]
        if sub_raw in live_ids:
            sub, sub_kind = sub_raw, "id"
        elif 1 <= sub_raw <= len(plan_now):
            sub = S.as_int(plan_now[sub_raw - 1].get("id"), 0, 0, 9999)
            sub_kind = "position"
        elif any(S.as_int(p.get("id"), 0, 0, 9999) == sub_raw for p in old_retired):
            sub, sub_kind = sub_raw, "retired"
        else:
            return fail(f"subgoal={sub_raw} は計画にありません（計画 {len(plan_now)} 項目。"
                        "先に plan で分解してください）")
        if sub_kind == "position":
            notes.append(f"subgoal={sub_raw} は plan の {sub_raw} 番目（id={sub}）として解決しました。")
        elif sub_kind == "retired":
            notes.append(f"subgoal={sub_raw} は計画から廃止済みの項目（id={sub}）です。"
                         "記録は残しますが、達成済みの印は付けません。")
    sub_done = S.as_flag(args.get("subgoal_done"))
    if sub_done and not sub:
        return fail("subgoal_done には subgoal（達成したサブ目標の番号）が必要です")

    return None, {"kind": kind, "is_revision": is_revision, "revises": revises,
                  "branch_id": bid, "branch_from": bfrom,
                  "resolve_branch": resolve, "branch_status": bstatus,
                  "tests_hypothesis": tests, "hypothesis_status": hstatus,
                  "plan": plan, "subgoal": sub, "subgoal_raw": sub_raw,
                  "subgoal_kind": sub_kind, "subgoal_done": sub_done, "notes": notes}


def _think_needed_flag(args: dict) -> bool:
    """「続けるか」を読む。**明示された null は未指定扱い**（既定 True）。

    `S.as_flag(None)` は False なので、素朴に `S.as_flag(args.get("next_thought_needed", True))` と書くと、
    クライアントが `null` を送っただけで台帳が暗黙に終端する（実測: null → False）。
    MCP クライアントは「引数を省略した」と「null を送った」を区別しないので、
    どちらも「未指定 → True」と解釈するのが安全側。
    `needs_more_thoughts` は `next_thought_needed` の別名（両方あれば前者を優先）。
    """
    for key in ("next_thought_needed", "needs_more_thoughts"):
        if key not in args:
            continue
        value = args.get(key)
        if value is None:
            continue          # null = 未指定 → 次の別名か既定へ
        return S.as_flag(value)
    return True


def _think_view(sid: str, stored: dict | None, brief: bool = False) -> dict:
    """台帳を**書かずに**読む（文脈圧縮・再起動の後に、積んだ思考へ戻るため）。

    `brief=True` は現行の道筋と未解決の項目だけを返す（大きな台帳を軽く読み戻すため）。
    """
    if not sid:
        return {"error": "view=true には session_id が必要です"}
    if stored is None:
        return {"error": f"セッション {S.truncate(sid, 40)} は見つかりません（期限切れ／未知）",
                "session_id": sid}
    steps = [s for s in (stored.get("steps") or []) if isinstance(s, dict)]
    last = max([S.as_int(s.get("n"), 0, 0, 9999) for s in steps] + [0])
    total = _think_latest_total(steps)
    card = S.as_str(stored.get("card"))
    data = {
        "session_id": sid, "view": True, "brief": bool(brief), "step": last or None,
        "question": stored.get("question") or "",
        "total_thoughts": total, "next_thought_needed": True,
        "ledger": _think_ledger_view(steps, keep=S.THOUGHT_MAX_STEPS, row=stored, brief=brief),
        "verification": None, "verified": False, "alternatives": None,
        "verifier_models": [m for m in (stored.get("verifier_models") or []) if isinstance(m, str)],
        "card": card,
        "notes": [],
        "next_call": {"tool": "freeagent_think",
                      "args": {"session_id": sid, "thought": "<次の思考>", "thought_number": last + 1,
                               **({"card": card} if card else {})}},
    }
    data["suggestions"] = _think_suggestions(data, steps, True)
    return data


def _think_alternatives(args: dict, question: str, steps: list[dict], step: dict, row: dict,
                        *, exclude: list[str]) -> dict:
    """生成者・検証者とは別のモデルに**代替案**を出させる。不通なら error を返す（呼び出し側は書かない）。"""
    user_exclude = S.as_str_list(args.get("exclude")) or []
    refs, info = S.select_models(S.as_int(args.get("size"), 2, 1, 4), None,
                               prefer=S.as_str_list(args.get("prefer")) or None,
                               exclude=user_exclude, identity_exclude=exclude)
    if not refs:
        return S._no_models()
    prompt = _think_prompt(question, steps, step, row).rsplit("\n\n", 1)[0] + (
        "\n\n上の道筋とは異なる代替の仮説・解法・道筋を「代替: …」の形式で最大3行だけ挙げてください。")
    # 既定 400 のまま: 予算を増やすと 1 体あたりの待ち時間と利用枠の消費が増える。
    # （proxy 経由の 10 秒接続タイムアウトという旧制約は 2026-10-04 の接続/読み取り分離で解消した。）
    # 打ち切り（truncated）で文の途中で切れた最後の案は parse_alternatives が捨てる（実測「代替: 親プロセスのコマン」）。
    # avoid: フォールバックが同じ呼び出しの**検証者**に落ちると「検証者とも別のモデル」が破れる（実測）。
    results = S.ask_many(refs, prompt, system=THINK_ALT_SYSTEM,
                       max_tokens=S.as_int(args.get("max_tokens"), 400, 16, 2000), kind="think",
                       avoid=user_exclude, identity_avoid=exclude)
    rows: list[dict] = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            rows.append({"model": ref, "error": res["error"]})
            continue
        rows.append({"model": ref, "served_by": res.get("served_by"),
                     "items": parse_alternatives(res.get("text"), truncated=bool(res.get("truncated"))),
                     "truncated": bool(res.get("truncated")),
                     "text": S.truncate(res.get("text") or "", 600)})
    answered = [r for r in rows if not r.get("error")]
    failed = [r for r in rows if r.get("error")]
    if not answered and failed and all(S.is_env_failure(r.get("error") or "") for r in failed):
        return {"error": "代替案の提案者に到達できませんでした（バックエンド不通: "
                         f"{S.truncate(failed[0].get('error') or '', 120)}）。この思考は台帳に記録していません。",
                "failures": [{"model": r["model"], "error": S.truncate(r.get("error") or "", 160)} for r in failed]}
    return {
        "models": refs, "selection": info, "answered": len(answered), "failed": len(failed),
        "items": [{"model": r["model"], "text": item} for r in answered for item in r["items"]],
        "empty": [r["model"] for r in answered if not r["items"]],
        "answers": rows,
        "failed_rows": [{"model": r["model"], "error": S.truncate(r.get("error") or "", 160)} for r in failed],
        "note": "代替案は独立モデルの提案（仮説）です。検証済みではなく、採否はメインが決めます。",
    }


EXPORTS = {
    "_think_step_label": _think_step_label,
    "_think_prompt": _think_prompt,
    "_think_ledger_view": _think_ledger_view,
    "_think_suggestions": _think_suggestions,
    "_think_verify_block": _think_verify_block,
    "tool_think": tool_think,
    "THINK_ALT_SYSTEM": THINK_ALT_SYSTEM,
    "_LABEL_ALT": _LABEL_ALT,
    "_BULLET": _BULLET,
    "parse_alternatives": parse_alternatives,
    "_think_latest_total": _think_latest_total,
    "_think_active": _think_active,
    "_think_preview_row": _think_preview_row,
    "_thought_subgoal_identity": _thought_subgoal_identity,
    "_thought_subgoal_still_matches": _thought_subgoal_still_matches,
    "_think_structure": _think_structure,
    "_think_needed_flag": _think_needed_flag,
    "_think_view": _think_view,
    "_think_alternatives": _think_alternatives,
}
