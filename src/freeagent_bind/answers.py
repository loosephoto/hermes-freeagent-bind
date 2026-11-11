"""§6.11 回答の組み立て（grounded / map / consult / agent / delegate）

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess

from . import server as S  # noqa: E402

def _evidence_window(citations: list[dict], *, item_chars: int | None = None,
                     total_chars: int | None = None, numbers: list[int] | None = None) -> dict:
    """本文予算に入った番号を返す。本文を渡せない項目は番号付き見出しも作らない。"""
    per_item = S.as_int(S._EVIDENCE_ITEM_CHARS if item_chars is None else item_chars, S._EVIDENCE_ITEM_CHARS, 0, 2000)
    budget = S.as_int(S._EVIDENCE_TOTAL_CHARS if total_chars is None else total_chars, S._EVIDENCE_TOTAL_CHARS, 0, 20000)
    labels = numbers if numbers and len(numbers) == len(citations) else list(range(1, len(citations) + 1))
    lines, injected = [], []
    for i, c in zip(labels, citations):
        if not S._kb_has_evidence(c):
            continue
        body = " ".join(S.as_str(c.get("summary")).split())
        take = min(per_item, budget, len(body))
        if take <= 0:
            continue
        bits = [f"[{i}] {c.get('title') or '(無題)'}"]
        if c.get("year"):
            bits.append(f"({c['year']})")
        if c.get("publication_types"):
            bits.append("種別: " + ", ".join(S.as_str_list(c["publication_types"])))
        if c.get("resource_type"):
            bits.append("種別: " + S.as_str(c["resource_type"]))
        bits.append(c.get("url") or "")
        credits = (c.get("attributions") or []) + ([c["attribution"]] if c.get("attribution") else [])
        bits.extend(dict.fromkeys(credits))
        lines.append(" ".join(str(b) for b in bits if b))
        lines.append("    " + S.truncate(body, take))
        budget -= take
        injected.append(i)
    return {"text": "\n".join(lines), "numbers": injected,
            "omitted": [i for i in labels if i not in injected]}


def _evidence_block(citations: list[dict], *, item_chars: int | None = None,
                    total_chars: int | None = None, numbers: list[int] | None = None) -> str:
    """従来の文字列インターフェース。番号の認定は_evidence_windowのnumbersを使う。"""
    return _evidence_window(citations, item_chars=item_chars, total_chars=total_chars, numbers=numbers)["text"]


def tool_grounded(args: dict) -> dict:
    """根拠を先に取り、それを注入してから複数モデルに答えさせる（幻覚の抑止）。"""
    question = S.as_str(args.get("question") or args.get("prompt"))
    if not question:
        return {"error": "question は必須です"}
    sources = S.as_str_list(args.get("sources")) or None
    # **検索語と質問文を分けられる**（v0.26.0）。arXiv / Crossref / Internet Archive は英語の語で
    # 引く必要があり、日本語の質問をそのまま投げると 0 件（arXiv は CJK を早期拒否）になる。
    # `search_query` を省略したときは従来どおり question をそのまま検索語にする。
    search_query = S.as_str(args.get("search_query")) or question
    kb = S.knowledge_lookup(search_query, sources, limit=S.as_int(args.get("limit"), 3, 1, 10),
                          lang=S.as_str(args.get("lang"), ""),
                          kind=S.as_str(args.get("github_kind"), "repo"),
                          datacite_kind=S.as_str(args.get("datacite_kind"), "all"), fallback=args.get("fallback") is True,
                          doc=S.as_str(args.get("doc")),
                          # **本文根拠が要る場面**（§6.18）。crossref は `has-abstract:true` を、
                          # hn はコメントも検索する（根拠の充填率を上げる）。
                          need_evidence=True)
    citations = [c for c in (kb.get("citations") or []) if S._kb_has_evidence(c)]
    if not citations:
        return {"error": "根拠が 0 件でした。query を変えるか sources を広げてください",
                "lookup": {"sources": kb.get("sources"), "errors": kb.get("errors"),
                           "unknown_sources": kb.get("unknown_sources") or []}}
    window = _evidence_window(citations)
    if not window["numbers"]:
        return {"error": "本文根拠を注入できませんでした。根拠の取得・本文予算を確認してください"}
    # **根拠 0 件・本文なしで早期に返す経路では選抜を走らせない**（提供元へ無駄な一覧要求を出さない）。
    # モデル選抜を知識取得と重ねる案は実測で採らなかった: 一覧がディスクキャッシュ（§3.3）で温まる
    # ため、重ねて縮むのは初回だけの 0.3 秒で、早期返却の契約を壊すほうが損。
    refs, info = S._select_or_error(args, default_size=2, kind="grounded")
    if not refs:
        return S._no_models()
    prompt = (
        "次の【根拠】だけを情報源として質問に答えてください。\n"
        "根拠に無い事実は書かない。書けない場合は「根拠に無い」と明示する。\n"
        "本文中で根拠を示すときは [番号] を付ける。\n\n"
        f"【根拠】\n{window['text']}\n\n【質問】\n{question}"
    )
    results = S.ask_many(refs, prompt, max_tokens=S.as_int(args.get("max_tokens"), 700, 16, 4000),
                       kind="grounded", avoid=S.as_str_list(args.get("exclude")) or None)
    answers = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            answers.append({"model": ref, "error": res["error"],
                            **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
            continue
        cited, unsupported = _cited_numbers(res["text"] or "", len(citations), allowed=window["numbers"])
        answers.append({"model": ref, "served_by": res.get("served_by"),
                        "answer": S.truncate(res["text"], 2000),
                        "cited": cited, "cited_ok": bool(cited), "unsupported_citations": unsupported,
                        "truncated": res.get("truncated"), "cot_leak": res.get("cot_leak"),
                        **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
    good = [a for a in answers if not a.get("error")]
    _grounded_agree = S.agreement_detail([a.get("answer") or "" for a in good])
    return {
        "question": question, "search_query": search_query,
        "citations": citations, "citation_count": len(citations),
        "sources_used": kb.get("sources"), "source_errors": kb.get("errors"),
        "injected_citations": window["numbers"], "not_injected_citations": window["omitted"],
        "evidence_citation_count": len(window["numbers"]),
        "unsupported_citations": sorted({n for row in good for n in row.get("unsupported_citations", [])}),
        "models": refs, "selection": info, "answers": answers,
        "concurrency": S.PROVIDER_GATE.report(refs),
        **S._quota_report_after_answers(refs),
        "agreement": _grounded_agree["value"], "agreement_source": _grounded_agree["source"],
        "agreement_votes": _grounded_agree["votes"],
        "participants": S.participants_of(refs, answers),
        "substitutions": S.substitutions_of(answers),
        "answers_with_citations": sum(1 for a in good if a.get("cited_ok")),
        "answered": len(good), "failed": len(answers) - len(good),
        **S._rate_limit_report(results),
    }


def tool_map(args: dict) -> dict:
    """多数の要素へ同一指示を並列適用し、必要なら reduce で統合する。"""
    items = S.as_str_list(args.get("items") or args.get("prompts"))
    instruction = S.as_str(args.get("instruction") or args.get("system"))
    if not items:
        return {"error": "items は必須です（文字列の配列）"}
    if not instruction:
        return {"error": "instruction は必須です（各要素へ適用する指示）"}
    # **黙って捨てない**（規約 63）: 上限を超えた分は実行せず、件数を返す。
    items_dropped = max(0, len(items) - S.MAX_MAP_ITEMS)
    items = items[:S.MAX_MAP_ITEMS]
    model = S.as_str(args.get("model"))
    selection = {"requested": True, "models": [model]} if model else {}
    if not model:
        selected, selection = S.select_models(1, kind="map")
        model = selected[0] if selected else ""
    if not model:
        return S._no_models()
    max_tokens = S.as_int(args.get("max_tokens"), 500, 16, 4000)
    prompts = [f"{instruction}\n\n---\n{S.truncate(item, 4000)}" for item in items]
    # 進捗のラベルに使うので **(要素, プロンプト) の組**で流す（プロンプト全体をラベルにしない）
    pairs = list(zip(items, prompts))
    # **予算を渡す**（渡さないと 240 秒の打ち切りが効かず、遅い要素 1 件で 300 秒の
    # クライアント待ちに当たって**取れていた結果まで失われる**。実測: 0.1 秒予算でも完走した）。
    map_started = S.now_ts()
    results = S.run_parallel(pairs, lambda pair: S.call_model(model, pair[1], max_tokens=max_tokens, kind="map"),
                           max_workers=S.MAX_WORKERS, deadline=S.call_budget(), fair_keys=prompts,
                           fair_providers=[S.split_ref(model)[0]] * len(pairs))
    wall = round(S.now_ts() - map_started, 3)
    sequential = round(sum(S.as_float(res.get("latency_s"), 0.0) for res in results), 3)
    rows = []
    for item, res in zip(items, results):
        row = {"item": S.truncate(item, 200),
               "output": S.truncate(res.get("text") or "", 1500) if not res.get("error") else "",
               "error": res.get("error") or ""}
        if res.get("quota_accounting"):
            row["quota_accounting"] = res["quota_accounting"]
        if res.get("coalesced"):
            row["coalesced"] = True      # 同一実行内の重複を畳んだ（上流は 1 回だけ）
        rows.append(row)
    ok = sum(1 for r in rows if not r["error"])
    coalesced_calls = sum(1 for r in rows if r.get("coalesced"))
    data = {"model": model, "count": len(rows), "ok": ok, "failed": len(rows) - ok, "results": rows,
            "items_dropped": items_dropped, "coalesced_calls": coalesced_calls, "selection": selection,
            # 所要時間は **fanout と同じ形**で返す（v0.42.2・issue #9）。サーバーが返さないと
            # 「並列が速い」ことを content でも言えない（実測: 12 件で 6.3s / 逐次 24.1s・3.8 倍）。
            "wall_s": wall, "sequential_estimate_s": sequential,
            "speedup": round(sequential / wall, 2) if wall > 0 and sequential else None}
    if S.as_flag(args.get("reduce")) and S.budget_expired():
        # 後段の統合は 1 回ぶんの推論を足す。予算切れで始めると 300 秒の待ちに当たるだけ。
        data["reduce_error"] = ("呼び出しの予算を使い切ったため reduce を実行しませんでした"
                                "（各要素の出力は results に残しています）")
    elif S.as_flag(args.get("reduce")):
        joined = "\n".join(f"- {r['output']}" for r in rows if r["output"])
        if joined.strip():
            reduce_prompt = (f"次の {len(rows)} 件の出力を統合してください。"
                            f"重複をまとめ、対立点は残し、出典番号や行番号を落とさない。\n\n{S.truncate(joined, 12000)}")
            reduce_started = S.now_ts()
            red = S.call_model(S.as_str(args.get("reduce_model")) or model, reduce_prompt,
                             max_tokens=S.as_int(args.get("reduce_max_tokens"), 900, 16, 4000),
                             kind="map_reduce")
            data["reduce_s"] = round(S.now_ts() - reduce_started, 3)
            data["reduced"] = red.get("text") or ""
            if red.get("quota_accounting"):
                data["reduce_quota_accounting"] = red["quota_accounting"]
            if red.get("error"):
                data["reduce_error"] = red["error"]
    return data


def _consult_prompt(question: str, main_reply: str, round_no: int, peers: list[dict] | None = None,
                    mode: str = "discuss", draft: str = "", basis: str = "",
                    stage: str = "quality") -> str:
    parts = [f"【問い】\n{question}"]
    if mode == "review" and draft:
        parts.append(f"【査読対象の下書き】\n{S.truncate(draft, 4000)}")
    if basis:
        parts.append(f"【仕様・計画（これに適合するかを見る）】\n{S.truncate(basis, 3000)}")
    if main_reply:
        parts.append(f"【メインからの回答（権威ある前提として扱う）】\n{S.truncate(main_reply, 3000)}")
    if peers:
        block = "\n".join(f"- {p['model']}: {S.truncate(p.get('conclusion') or p.get('text') or '', 400)}"
                          for p in peers)
        parts.append(f"【他の参加者の回答（第{round_no}ラウンド）】\n{block}")
    if stage == "compliance":
        parts.append("上の形式（結論/判定/指摘/メインに確認したい点）だけで答えてください。"
                     "**品質の良し悪しは書かず**、仕様・計画の各要件を満たすかどうかだけを見てください。")
    elif mode == "review" and draft:
        parts.append("上の形式（結論/重要度/指摘/メインに確認したい点）だけで答えてください。"
                     "5 軸（correctness / readability / architecture / security / performance）を"
                     "一通り見てから、最も重い重要度を 1 つ名乗ってください。")
    else:
        parts.append("上の形式（結論/確信度/メインに確認したい点）だけで答えてください。"
                     "確認したい点は最も重要な 1 つだけにし、推奨の既定値を「既定: …」で添えてください。")
    return "\n\n".join(parts)


def _previous_questions(rounds: list[dict]) -> list[str]:
    """直前ラウンドで出ていた質問（「解消した」を数えるため）。無ければ空。"""
    if len(rounds) < 2:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in rounds[-2].get("answers") or []:
        question = S.as_str(row.get("question")).strip()
        if question and S.norm_text(question) not in seen:
            seen.add(S.norm_text(question))
            out.append(question)
    return out


def tool_consult(args: dict) -> dict:
    """メイン↔サブの双方向相談。`debate_depth="deep"` で独立回答→反論→統合の 3 段討論。"""
    sid = S.as_str(args.get("session_id"))
    main_reply = S.as_str(args.get("main_reply"))
    draft = S.as_str(args.get("draft"))
    basis = S.as_str(args.get("basis"))
    mode = S.as_str(args.get("mode"), "discuss")
    depth = S.as_str(args.get("debate_depth"), "normal")
    stored = S.session_get(sid) if sid else None
    if stored:
        # 再開では mode / draft / basis を引き継ぐ（渡されなければ保存値）。
        # 2 段査読は basis が無いと成立しないので、ここを落とすと再開で片肺になる（実測で踏んだ）。
        mode = S.as_str(args.get("mode")) or S.as_str(stored.get("mode"), mode)
        draft = draft or S.as_str(stored.get("draft"))
        basis = basis or S.as_str(stored.get("basis"))
    question = S.as_str(args.get("question") or (stored or {}).get("question"))

    if stored and not args.get("question"):
        question = stored.get("question") or ""
        if not main_reply:
            return {"error": f"セッション {sid} を再開するには main_reply（または question）が必要です",
                    "session_id": sid, "stage": stored.get("stage")}
    if not question:
        return {"error": "question は必須です（session_id を付けると前回の問いを引き継ぎます）"}

    if stored and stored.get("models"):
        # A compliance review retains its original slots, including cooling models.
        saved_refs = [m for m in stored["models"] if m][:S.MAX_MODELS_PER_CALL]
        excluded = set(S.as_str_list(args.get("exclude")) + S.env_excluded_models())
        blocked_refs = [ref for ref in saved_refs if S._is_model_excluded(ref, excluded)]
        if blocked_refs:
            return {"error": "保存済みセッションに除外対象のモデルがあります。除外設定を確認するか、"
                             "別のモデルで新しいセッションを開始してください: " + ", ".join(blocked_refs),
                    "session_id": sid, "excluded_models": blocked_refs}
        seen_slots = {}
        for ref in saved_refs:
            identity = S._model_identity(ref)
            if identity in seen_slots:
                return {"error": f"Duplicate model identity in saved session: {seen_slots[identity]}, {ref}. Start a new session with distinct models.",
                        "session_id": sid}
            seen_slots[identity] = ref
        refs = saved_refs if mode == "review" and draft and basis else [m for m in saved_refs if not S.is_cooling(m)]
        info = {"requested": True, "resumed_from": sid, "models": refs}
        if not refs:
            refs, info = S._select_or_error(args, default_size=3, kind="consult")
    else:
        refs, info = S._select_or_error(args, default_size=3, kind="consult")
    if not refs:
        return S._no_models()

    rounds: list[dict] = list((stored or {}).get("rounds") or [])
    # 査読は 4 行（結論/重要度/指摘/確認点）を要求するので既定を上げる（実測: 500 で指摘が切れた）。
    review_mode = mode == "review" and bool(draft)
    max_tokens = S.as_int(args.get("max_tokens"), 800 if review_mode else 600, 16, 4000)
    replies = list((stored or {}).get("main_replies") or [])
    if main_reply:
        replies.append(main_reply)

    # 第1ラウンド（または再開時の再検討）
    round_no = len(rounds) + 1
    # `basis` を渡したときだけ、先に**仕様適合**の段を 1 回走らせる（2 段目が品質）。
    # 再開時は保存済みの適合結果を使い回す（毎ラウンド 2 倍の呼び出しを払わない）。
    compliance_rows: list[dict] = []
    compliance_failures: list[dict] = []
    compliance_summary = None
    if review_mode and basis:
        import hashlib
        fingerprint = hashlib.sha256(S._dumps([question, draft, basis, main_reply]).encode("utf-8")).hexdigest()
        stored_compliance = next((r for r in reversed(rounds) if r.get("kind") == "compliance"), None)
        # Legacy rounds with no fingerprint (or an older fingerprint) are re-reviewed conservatively.
        if stored_compliance and stored_compliance.get("fingerprint") != fingerprint:
            stored_compliance = None
        prior = {row.get("model"): row for row in (stored_compliance or {}).get("answers", [])
                 if isinstance(row, dict) and not row.get("error")}
        excluded_now = set(S.as_str_list(args.get("exclude")) + S.env_excluded_models())
        blocked_prior = [S.as_str(row.get("served_by")) for row in prior.values()
                         if S.as_str(row.get("served_by"))
                         and S._is_model_excluded(S.as_str(row.get("served_by")), excluded_now)]
        if blocked_prior:
            return {"error": "保存済み査読の回答モデルが除外対象です。除外設定を確認するか、"
                             "別のモデルで新しいセッションを開始してください: " + ", ".join(blocked_prior),
                    "session_id": sid, "excluded_models": blocked_prior}
        missing = [ref for ref in refs if ref not in prior]
        crows = [prior[ref] for ref in refs if ref in prior]
        seen_reviewers = {}
        for row in crows:
            reviewer = row.get("served_by") or row["model"]
            identity = S._model_identity(reviewer)
            if identity in seen_reviewers:
                return {"error": f"Duplicate reviewer identity in saved session: {seen_reviewers[identity]}, {reviewer}. Start a new session with distinct models.",
                        "session_id": sid}
            seen_reviewers[identity] = reviewer
        cooling_missing = [ref for ref in missing if stored and S.is_cooling(ref)]
        crows.extend({"model": ref, "error": "適合査読はクールダウン中のため未実施です"}
                     for ref in cooling_missing)
        pending = [ref for ref in missing if ref not in cooling_missing]
        if pending:
            cprompt = _consult_prompt(question, main_reply, round_no, mode=mode, draft=draft,
                                      basis=basis, stage="compliance")
            used_reviewers = [name for row in crows if not row.get("error")
                              for name in (row.get("model"), row.get("served_by"))
                              if isinstance(name, str) and name]
            cres = S.ask_many(pending, cprompt, system=S.COMPLIANCE_SYSTEM, max_tokens=max_tokens,
                            kind="consult", avoid=S.as_str_list(args.get("exclude")),
                            identity_avoid=used_reviewers + cooling_missing)
            for index, ref in enumerate(pending):
                res = cres[index] if index < len(cres) else {"error": "適合査読の応答がありません"}
                if res.get("error"):
                    crows.append({"model": ref, "error": res["error"],
                                  **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
                    continue
                row = {"model": ref, "served_by": res.get("served_by"),
                       "conclusion": S.truncate(res["text"], 400),
                       "text": S.truncate(res["text"], 1500),
                       "truncated": bool(res.get("truncated")),
                       "redactions": S.as_int(res.get("redactions"), 0, 0, 999),
                       **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})}
                row.update(S.parse_compliance(res["text"]))
                crows.append(row)
        if missing:
            rounds.append({"round": round_no, "kind": "compliance", "answers": crows,
                           "fingerprint": fingerprint})
            round_no += 1
        compliance_rows = [row for row in crows if not row.get("error")]
        compliance_failures = [row for row in crows if row.get("error")]
        compliance_summary = S.summarize_compliance(crows)
    prompt = _consult_prompt(question, main_reply, round_no, mode=mode, draft=draft)
    results = S.ask_many(refs, prompt, system=S.REVIEW_SYSTEM if review_mode else S.CONSULT_SYSTEM,
                       max_tokens=max_tokens, kind="consult",
                       avoid=S.as_str_list(args.get("exclude")) or None)
    round_rows = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            round_rows.append({"model": ref, "error": res["error"],
                               **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
            continue
        parsed = S.parse_labeled(res["text"])
        row = {"model": ref, "served_by": res.get("served_by"),
               "conclusion": parsed["conclusion"] or S.truncate(res["text"], 400),
               "confidence": parsed["confidence"], "question": parsed["question"],
               "text": S.truncate(res["text"], 1500), "cot_leak": res.get("cot_leak"),
               "truncated": bool(res.get("truncated")),
               "redactions": S.as_int(res.get("redactions"), 0, 0, 999),
               **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})}
        if review_mode:
            row.update(S.parse_review(res["text"]))
        round_rows.append(row)
    quality_rounds = [r for r in rounds if r.get("kind") in ("initial", "reconsider")]
    rounds.append({"round": round_no,
                   "kind": "reconsider" if quality_rounds else "initial",
                   "answers": round_rows})

    debate_summary = None
    _conf_by_model: dict = {}
    do_debate = (depth == "deep") and not S.budget_expired()
    if depth == "deep" and not do_debate:
        dropped_rows = [S._dropped_result(S.call_budget(), "未実行") for _ in refs]
        debate_rows = [{"model": ref, "error": r.get("error")} for ref, r in zip(refs, dropped_rows)]
        rounds.append({"round": round_no + 1, "kind": "debate", "answers": debate_rows})
        dropped = [{"round": r["round"], "kind": r["kind"], "model": a.get("model"),
                    "error": S.truncate(a.get("error") or "", 160)}
                   for r in rounds for a in r["answers"] if a.get("error")]
        debate_summary = {"dropped": dropped,
                          "error": "呼び出しの予算を使い切ったため討論を実行しませんでした（初回回答は有効です）"}
    if do_debate:
        peers = [row for row in round_rows if not row.get("error")]
        rebuttal_prompt = _consult_prompt(question, main_reply, round_no, peers=peers)
        rebuttal = S.ask_many(refs, rebuttal_prompt, system=S.DEBATE_SYSTEM,
                            max_tokens=max_tokens, kind="debate",
                            avoid=S.as_str_list(args.get("exclude")) or None)
        _conf_by_model = {r["model"]: r.get("confidence") for r in round_rows
                          if isinstance(r.get("confidence"), int)}
        debate_rows = []
        for ref, res in zip(refs, rebuttal):
            if res.get("error"):
                debate_rows.append({"model": ref, "error": res["error"],
                                    **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
                continue
            labels = {}
            for part in (res["text"] or "").splitlines():
                if ":" in part or "：" in part:
                    key, value = re.split(r"[:：]", part, maxsplit=1)
                    labels[key.strip()] = value.strip()
            debate_rows.append({"model": ref, "served_by": res.get("served_by"),
                                "text": S.truncate(res["text"], 1500),
                                "position": S.truncate(labels.get("立場") or "", 300),
                                "strongest_objection": S.truncate(labels.get("最強の反論") or "", 400),
                                "response": S.truncate(labels.get("応答") or "", 400),
                                "unresolved": S.truncate(labels.get("未解決") or "", 400),
                                **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
        rounds.append({"round": round_no + 1, "kind": "debate", "answers": debate_rows})
        initial = {row["model"]: row.get("conclusion") for row in round_rows if not row.get("error")}
        # ラウンド途中で脱落した参加者を**明示的に残す**（実測: deep 討論の 2 ラウンド目で 1 体が落ち、
        # content には 1 体しか出ず「元から 1 体だった」ように見えた。失敗は隠さない）。
        dropped = [{"round": r["round"], "kind": r["kind"], "model": a.get("model"),
                    "error": S.truncate(a.get("error") or "", 160)}
                   for r in rounds for a in r["answers"] if a.get("error")]
        debate_summary = {
            "dropped": dropped,
            "agreement_by_round": [S.agreement_of([a.get("conclusion") or a.get("text") or ""
                                                 for a in r["answers"] if not a.get("error")])
                                   for r in rounds],
            "participants": [{
                "model": row["model"],
                "initial_position": initial.get(row["model"], ""),
                "final_position": row.get("position") or row.get("text", "")[:200],
                "changed": bool(initial.get(row["model"])
                                and row.get("position")
                                and S.similarity(initial[row["model"]], row["position"]) < 0.5),
                "strongest_objection": row.get("strongest_objection"),
                "unresolved": row.get("unresolved"),
            } for row in debate_rows if not row.get("error")],
            "unresolved_dissent": any(
                (row.get("unresolved") or "").lower().rstrip(" .。!?！？;；").strip() not in S._NO_ANSWER
                and bool(row.get("unresolved")) for row in debate_rows if not row.get("error")),
        }
        # 確信度の較正（v0.42.6）: **討論で立場を変えた**を誤答の代理ラベルとして数える。
        # 真の正誤ラベルではないので割合だけを出す（SPEC §6.52）。
        for part in debate_summary["participants"]:
            conf = _conf_by_model.get(part.get("model"))
            if isinstance(conf, int):
                S.note_confidence(part["model"], conf, flipped=bool(part.get("changed")))

    if do_debate:
        debate_by_model = {row.get("model"): row for row in debate_rows}
        last = []
        for initial in round_rows:
            if initial.get("error"):
                last.append(initial)
                continue
            debated = debate_by_model.get(initial.get("model"), {})
            if debated.get("error"):
                last.append(initial)
                continue
            last.append({
                **initial, "model": initial["model"], "served_by": debated.get("served_by") or initial.get("served_by"),
                "conclusion": debated.get("position") or initial.get("conclusion") or "",
                "confidence": initial.get("confidence"), "question": initial.get("question") or "",
                "answer": debated.get("text") or initial.get("text") or "",
                "text": debated.get("text") or initial.get("text") or "",
            })
    else:
        last = [row for row in round_rows if not row.get("error")]
    question_rows = last
    last = [row for row in last if not row.get("error")]
    open_questions = []
    seen_questions = set()
    for row in question_rows:
        question_text = (row.get("question") or "").strip()
        normalized = S.norm_text(question_text)
        if question_text and normalized not in seen_questions:
            seen_questions.add(normalized)
            open_questions.append({"model": row["model"], "question": question_text})
    # 上限までをメインへ返し、超えた分は deferred に回す（黙って捨てない。規約 8）。
    reviewed = [{"model": q["model"], "question": q["question"],
                 "default": S.parse_question_default(q["question"])[1]} for q in open_questions]
    kept, deferred = reviewed[:S.QUESTION_CAP], reviewed[S.QUESTION_CAP:]
    open_questions_for_main = [q["question"] for q in kept]
    prev_questions = _previous_questions(rounds)
    current_norm = {S.norm_text(q["question"]) for q in kept}
    resolved = [q for q in prev_questions if S.norm_text(q) not in current_norm]
    coverage = {"status": "clear" if not kept else "outstanding",
                "resolved": resolved, "outstanding": len(kept), "deferred": len(deferred),
                "note": "resolved は質問文の表層一致で判定します（言い換えは解消と誤判定しえます）"}
    findings_summary = S.summarize_review(last) if review_mode else None
    if findings_summary is not None:
        findings_summary["truncated"] = sum(1 for row in last if row.get("truncated"))
        if compliance_summary:
            # 仕様に適合していない・判定を名乗らない、は品質の重さに関係なく差し戻し。
            findings_summary["compliance_blocking"] = compliance_summary["blocking"]
            findings_summary["blocking"] = (findings_summary["blocking"]
                                            or compliance_summary["blocking"])
    review_rows = ([{"model": row.get("model"), "severity": row.get("severity") or "",
                     "findings": row.get("findings") or [], "axes": row.get("axes") or [],
                     "truncated": bool(row.get("truncated"))}
                    for row in last] if review_mode else [])
    sid = sid or S.new_session_id()
    S.session_put(sid, {
        "question": question, "models": refs, "rounds": rounds,
        "main_replies": replies, "mode": mode, "depth": depth, "draft": draft,
        "basis": basis,
        "stage": "awaiting_main" if open_questions else "complete",
        "created_at": (stored or {}).get("created_at") or S.now_ts(),
    })
    conf = [row["confidence"] for row in last if isinstance(row.get("confidence"), int)]
    quota_accounting = [{"round": r.get("round"), "kind": r.get("kind"), "model": a.get("model"),
                         "warning": a["quota_accounting"]}
                        for r in rounds for a in r.get("answers", []) if a.get("quota_accounting")]
    _consult_agree = S.agreement_detail([row.get("conclusion") or "" for row in last])
    return {
        "session_id": sid,
        "mode": mode,
        "stage": "awaiting_main" if open_questions else "complete",
        "rounds_run": len(rounds),
        "question": S.truncate(question, 400),
        "models": refs, "selection": info,
        "concurrency": S.PROVIDER_GATE.report(refs),
        **S._quota_report_after_answers(refs),
        "quota_accounting": quota_accounting,
        "agreement": _consult_agree["value"], "agreement_source": _consult_agree["source"],
        "agreement_votes": _consult_agree["votes"],
        "participants": S.participants_of(refs, last),
        "substitutions": S.substitutions_of(last),
        "agreement_note": S._agreement_note(_consult_agree["source"]),
        "confidence_mean": round(sum(conf) / len(conf), 1) if conf else None,
        "confidence_source": "self_report",
        "S.confidence_warning": S.confidence_warning(
            round(sum(conf) / len(conf), 1) if conf else None, _consult_agree["value"]),
        "S.confidence_calibration": S.confidence_calibration([r.get("model") for r in round_rows]),
        "consensus": last,
        "failed": [{"model": row.get("model"), "error": S.truncate(row.get("error") or "", 160)}
                   for row in compliance_failures + rounds[-1]["answers"] if row.get("error")],
        "open_questions_for_main": open_questions_for_main,
        "open_questions": kept,
        "deferred_questions": deferred,
        "coverage": coverage,
        "question_budget": {"cap": S.QUESTION_CAP, "open": len(kept), "deferred": len(deferred)},
        "findings_summary": findings_summary,
        "review": review_rows,
        "redactions": sum(S.as_int((row or {}).get("redactions"), 0, 0, 999) for row in last),
        "stages": (["compliance", "quality"] if compliance_summary else ["quality"])
                  if review_mode else ["discuss"],
        "compliance_summary": compliance_summary,
        "compliance": compliance_rows,
        "debate_summary": debate_summary,
        "next_call": ({"tool": "freeagent_consult",
                       "args": {"session_id": sid, "main_reply": "<メインの回答>",
                                "max_tokens": max_tokens}}
                      if open_questions else None),
        "privacy_note": "決断はメインLLMが行ってください。サブの出力は仮説です。",
    }


def _agent_tool_call(parsed: dict) -> dict:
    """サブエージェントが要求した読み取り専用ツールを実行する（書き込み系は無い）。"""
    tool = S.as_str(parsed.get("tool"))
    query = S.as_str(parsed.get("query") or parsed.get("q"))
    if tool == "lookup" or tool in S.KB_BACKENDS:
        sources = S.as_str_list(parsed.get("sources")) or ([tool] if tool in S.KB_BACKENDS else None)
        out = S.knowledge_lookup(query, sources, limit=S.as_int(parsed.get("limit"), 2, 1, 5),
                               kind=S.as_str(parsed.get("kind"), "repo"),
                               datacite_kind=S.as_str(parsed.get("datacite_kind"), "all"), fallback=parsed.get("fallback") is True)
        all_cites = out.get("citations") or []
        cites = [c for c in all_cites if S._kb_has_evidence(c)]
        bibliography = [c for c in all_cites if not S._kb_has_evidence(c)]
        brief = []
        for src, res in (out.get("results") or {}).items():
            for item in (res.get("items") or [])[:2]:
                label = "書誌のみ（本文根拠なし）" if item.get("metadata_only") else src
                brief.append(f"{label}: {item.get('title') or item.get('label') or ''} "
                             f"— {S.truncate(item.get('summary') or item.get('description') or '', 300)}")
        return {"hits": len(cites), "brief": brief, "citations": cites[:6], "bibliography": bibliography,
                "errors": out.get("errors") or {}}
    return {"hits": 0, "brief": [f"未知のツール: {tool}（使えるのは lookup / {', '.join(S.KB_BACKENDS)}）"],
            "citations": []}


def _parse_agent_reply(text: str) -> dict:
    """JSON だけを取り出す（コードフェンスや前置き・末尾の補足に `{}` が混ざっても最初の dict を読む）。"""
    return S._jsonutil.extract_object(text)


def _cited_numbers(answer: str, total: int, allowed: list[int] | None = None) -> tuple[list[int], list[int]]:
    """回答中の `[n]` を根拠の番号と突き合わせる。返り値は (根拠にある番号, 無い番号)。

    引用を検査しないと、サブエージェントが根拠を読まずに記憶で答えて `[1]` を飾りで付けた場合に"
    気づけない（実測: citations は常に全件返っていて、回答がそれを参照したかを見ていなかった）。
    `[0]` は「根拠を使っていない」の明示として許し、引用には数えない。
    """
    cited: set[int] = set()
    unsupported: set[int] = set()
    accepted = set(range(1, total + 1)) if allowed is None else set(allowed) & set(range(1, total + 1))
    for raw in re.findall(r"\[(\d{1,2})\]", answer or ""):
        number = S.as_int(raw, -1, 0, 99)
        if number <= 0:
            continue
        if number in accepted:
            cited.add(number)
        else:
            # 根拠が 0 件でも [1] と書けば「存在しない出典」なので unsupported に入れる。
            unsupported.add(number)
    return sorted(cited), sorted(unsupported)


def tool_agent(args: dict) -> dict:
    """サブLLMが自分で知識ツールを呼んで調査するループ（読み取り専用・並列）。"""
    task = S.as_str(args.get("task") or args.get("prompt"))
    if not task:
        return {"error": "task は必須です"}
    refs, info = S._select_or_error(args, default_size=2, kind="agent")
    if not refs:
        return S._no_models()
    max_steps = S.as_int(args.get("max_steps"), 2, 1, 4)
    main_reply = S.as_str(args.get("main_reply"))
    max_tokens = S.as_int(args.get("max_tokens"), 600, 16, 3000)
    base = f"【依頼】\n{task}"
    if main_reply:
        base += f"\n\n【メインからの回答（最優先の前提）】\n{S.truncate(main_reply, 3000)}"

    claims = S._ModelClaims(refs, S.as_str_list(args.get("exclude")) + S.env_excluded_models() or None)

    def run_one(ref: str) -> dict:
        history = base
        trace: list[dict] = []
        registry: list[dict] = []      # 番号を振った本文根拠（回答中の [n] と対応させる）
        bibliography: list[dict] = []  # 書誌探索は番号のregistryと分離する
        numbers: dict[tuple, int] = {}
        evidence: list[str] = []
        injected: set[int] = set()

        def register(new_cites: list[dict]) -> list[tuple[int, dict]]:
            """新しい根拠に通し番号を振る（同じ URL には同じ番号を保つ）。"""
            added: list[tuple[int, dict]] = []
            for cite in new_cites or []:
                key = S._kb_citation_key(cite)
                if not S._kb_has_evidence(cite) or not cite.get("url"):
                    continue
                if key in numbers:
                    if numbers[key] not in injected:
                        added.append((numbers[key], registry[numbers[key] - 1]))
                    continue
                numbers[key] = len(registry) + 1
                registry.append(cite)
                added.append((numbers[key], cite))
            return added

        for step in range(max_steps):
            if S.budget_expired():
                return {"model": ref, "steps": step, "trace": trace, "answer": "",
                        "steps_exhausted": True, "timed_out": True, "dropped": True,
                        "error": f"並列実行の予算で打ち切りました（ステップ {step}/{max_steps}）",
                        "evidence": evidence[:8], "citations": registry, "bibliography": bibliography,
                        "injected_citations": sorted(injected), "cited": [], "cited_ok": False}
            final_step = step == max_steps - 1
            res = S.call_model(ref, history + f"\n\n（{step + 1}/{max_steps} ステップ目。"
                                            "JSON を1つだけ出力）",
                             system=S.AGENT_SYSTEM_FINAL if final_step else S.AGENT_SYSTEM,
                             max_tokens=max_tokens, kind="agent", claims=claims)
            if res.get("error"):
                return {"model": ref, "error": res["error"], "steps": step, "trace": trace}
            parsed = _parse_agent_reply(res["text"])
            if parsed.get("tool") and not final_step:
                got = S._agent_tool_call(parsed)
                added = register(got.get("citations") or [])
                bibliography.extend(got.get("bibliography") or [])
                trace.append({"tool": parsed.get("tool"), "args": parsed, "hits": got["hits"],
                              "new_citations": len(added)})
                evidence.extend(got["brief"][:6])
                # **番号つきの本文**を注入する。番号を保たないと回答中の [n] を検査できない。
                window = _evidence_window([c for _, c in added], item_chars=300, total_chars=1200,
                                          numbers=[n for n, _ in added])
                injected.update(window["numbers"])
                history += (f"\n\n【ツール結果 {parsed.get('tool')}】\n"
                            + (window["text"] or "本文根拠を注入できませんでした（書誌のみ・本文予算不足等）"))
                continue
            answer = S.as_str(parsed.get("answer"))
            if answer:
                cited, unsupported = _cited_numbers(answer, len(registry), allowed=list(injected))
                return {"model": ref, "served_by": res.get("served_by"), "steps": step + 1,
                        "answer": S.truncate(answer, 1500), "trace": trace,
                        "citations": registry, "bibliography": bibliography, "injected_citations": sorted(injected),
                        "cited": cited, "cited_ok": bool(cited),
                        "unsupported_citations": unsupported}
            # 最終ステップでツールを求められた場合は実行しない（予算切れ）。推測で埋めず、
            # **集めた根拠だけを返して「回答に到達しなかった」と明示する**。
            return {"model": ref, "served_by": res.get("served_by"), "steps": step + 1,
                    "answer": "", "steps_exhausted": True, "trace": trace,
                    "evidence": evidence[:8], "citations": registry, "bibliography": bibliography,
                    "injected_citations": sorted(injected),
                    "cited": [], "cited_ok": False}
        return {"model": ref, "steps": max_steps, "trace": trace, "answer": "",
                "steps_exhausted": True, "evidence": evidence[:8], "citations": registry, "bibliography": bibliography,
                    "injected_citations": sorted(injected),
                "cited": [], "cited_ok": False}

    # サブエージェントは 1 体あたり最大 4 ステップ（各ステップが推論 1 回）走るので、
    # 予算を渡さないと `max_steps` の掛け算で 300 秒を超える（`tool_map` と同じ穴）。
    results = S.run_parallel(refs, run_one, max_workers=min(len(refs), S.MAX_WORKERS),
                           deadline=S.call_budget())
    good = [r for r in results if not r.get("error")]
    citations, seen = [], set()
    for row in good:
        for cite in row.get("citations") or []:
            key = S._kb_citation_key(cite)
            if key not in seen:
                seen.add(key)
                citations.append(cite)
    _agent_agree = S.agreement_detail([r.get("answer") or "" for r in good])
    return {
        "task": S.truncate(task, 300), "models": refs, "selection": info,
        "agents": results, "answered": len(good), "failed": len(results) - len(good),
        "tool_calls": sum(len(r.get("trace") or []) for r in results),
        "citations": citations, "citation_count": len(citations),
        "agreement": _agent_agree["value"], "agreement_source": _agent_agree["source"],
        "agreement_votes": _agent_agree["votes"],
        "participants": S.participants_of(refs, results),
        "substitutions": S.substitutions_of(results),
        "answers_with_citations": sum(1 for r in good if r.get("cited_ok")),
        "unsupported_citations": sorted({n for r in good
                                        for n in (r.get("unsupported_citations") or [])}),
        "usage_note": "サブエージェントは読み取り専用の知識ツールのみ呼べます。書き込みはしません。"
                      "回答中の [n] は根拠の番号と照合し、根拠に無い番号は unsupported_citations に出します。",
    }


DELEGATE_VERIFY_PREFIX = (
    "【完了報告の条件（この依頼では必ず守る）】\n"
    "- 最終報告に、実行した検証コマンドとその出力（成功/失敗）を必ず含める\n"
    "- 確かめていない項目は「未検証」と明記する（推測で埋めない）\n"
    "- 失敗が 1 つでもあれば「完了」と書かない\n"
    "\n"
    "【依頼】\n"
)


def tool_delegate(args: dict) -> dict:
    """フルツール付きの Hermes 本体を独立プロセスで起動する（opt-in・重い経路）。"""
    if not S.ALLOW_AGENT:
        return {"error": "この経路は無効です。FREEAGENT_ALLOW_AGENT=1 で有効化してください",
                "enabled": False}
    task = S.as_str(args.get("task"))
    if not task:
        return {"error": "task は必須です"}
    timeout = S.as_int(args.get("timeout"), 300, 30, 1800)
    # 真偽の既定は `dict.get(既定)` に載せない（明示 None で既定が死ぬ。規約 D）。
    verify_raw = args.get("verify_required")
    verify_required = True if verify_raw is None else S.as_flag(verify_raw)
    effective = (DELEGATE_VERIFY_PREFIX + task) if verify_required else task
    import subprocess
    started = S.now_ts()
    try:
        _nw = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.run([S.HERMES_BIN, "chat", "-q", effective], capture_output=True,
                              timeout=timeout, text=True, encoding="utf-8", errors="replace",
                              creationflags=_nw)
        return {"task": S.truncate(task, 300), "verification_enforced": verify_required,
                "task_effective": S.truncate(effective, 400),
                "exit_code": proc.returncode,
                "stdout": S.truncate(proc.stdout or "", 4000),
                "stderr": S.truncate(proc.stderr or "", 1000),
                "elapsed_s": round(S.now_ts() - started, 1),
                "next_step": "返ってきた報告の証拠を freeagent_admit(action=\"report\", "
                             "report={kind, evidence:{commands:[{cmd,result}]}}) に渡すと"
                             "受理/差し戻しを決定論で判定できます"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "elapsed_s": round(S.now_ts() - started, 1)}


EXPORTS = {
    "_evidence_window": _evidence_window,
    "_evidence_block": _evidence_block,
    "tool_grounded": tool_grounded,
    "tool_map": tool_map,
    "_consult_prompt": _consult_prompt,
    "_previous_questions": _previous_questions,
    "tool_consult": tool_consult,
    "_agent_tool_call": _agent_tool_call,
    "_parse_agent_reply": _parse_agent_reply,
    "_cited_numbers": _cited_numbers,
    "tool_agent": tool_agent,
    "DELEGATE_VERIFY_PREFIX": DELEGATE_VERIFY_PREFIX,
    "tool_delegate": tool_delegate,
}
