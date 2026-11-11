"""§8 表示（content）— server.py から機械的に切り出した（v0.41.x の分割）。

**共有ヘルパーは `S.<name>` の属性参照**で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。`render` と `_RENDERERS` はテストが差し替える
名前なので server.py に残してある（移動先からは `S.render` / `S._RENDERERS` で参照）。
"""
from __future__ import annotations

from . import server as S  # noqa: E402

def _render_models(data: dict) -> str:
    """`freeagent_models` の本文（`content` = 人間向け日本語）。"""
    lines = []
    for row in data.get("providers") or []:
        # ✓ は「資格情報がある」だけでなく「一覧が取れている」ことも表す。取れていないのに ✓ だと、
        # モデル 0 件の理由（プロキシ停止など）が分からない（実測: nous が ✓ 0 件で並んだ）。
        if row.get("ready") and not row.get("error"):
            mark = "✓"
        elif row.get("ready"):
            mark = "⚠"
        else:
            mark = "—"
        if not row.get("ready") and row.get("requires_activation"):
            missing = ", ".join(row.get("missing_settings") or []) or "設定確認"
            note = f"  [無効: {missing}]"
        elif not row.get("ready") and row.get("key_env"):
            note = f"  [{row['key_env']} 未設定 → 検索のみ]"
        else:
            note = ""
        lines.append(f"  {mark} {row['provider']:11} {row['models']:4} モデル / Free {row['free']:3}{note}")
        if row.get("error"):
            lines.append(f"      到達不可: {row['error'][:100]}")
    head = (f"サブLLMバックエンド（{sum(1 for r in data.get('providers') or [] if r.get('ready'))} プロバイダ有効）\n"
            + "\n".join(lines))
    head += (f"\nFree候補 {data.get('free_candidates')} 件（うち今すぐ使用可 {data.get('usable_now')}）"
             f" / 全 {data.get('total_models')} モデル\n既定: {data.get('default_model')}")
    non_chat = data.get("non_chat_candidates") or 0
    if non_chat:
        # 選抜から外した分を黙って消さない（「要約で無い」と「そもそも無い」を混同させない）。
        head += (f"\n非チャット（埋め込み・分類器・画像/音声生成）{non_chat} 件は自動選抜から除外"
                 "（一覧には表示）")
    cooling = data.get("cooling") or {}
    if cooling:
        # 全部並べると 1 行が長すぎて読めない（実測: 55 件で画面が埋まった）。先頭だけ出して件数を添える。
        shown = list(cooling)[:8]
        head += (f"\nクールダウン中 {len(cooling)} 件: " + ", ".join(shown)
                 + (f" … 他 {len(cooling) - len(shown)} 件" if len(cooling) > len(shown) else ""))
    harness = data.get("harness") or {}
    if harness:
        label = {"hermes": "Hermes Agent", "other": "Hermes 以外", "unknown": "判別不能"}.get(
            harness.get("kind"), harness.get("kind"))
        head += f"\nハーネス: {label}（{harness.get('reason')}）"
    q = data.get("query")
    if q:
        head += (f"\n\n🔍 検索: query={q.get('query')!r} provider={q.get('provider')}"
                 + (" free 限定" if q.get("free_only") else "")
                 + (f" offset={q['offset']}" if q.get("offset") else "")
                 + f" → {q.get('matched')} 件（Free {q.get('matched_free')}）"
                 f"／表示 {q.get('shown')} 件")
        if q.get("probed"):
            head += (f"\n   生存確認: 試行 {q.get('probe_attempted')} / 応答 {q.get('probe_alive')}"
                     + (f" / 遅い {q.get('probe_slow')}" if q.get("probe_slow") else "")
                     + "（404=廃止・403=権限なしは一覧から除外）")
            for row in (q.get("probe_dropped") or []):
                head += f"\n     ✗ {row['ref']} [{row.get('verdict')}]: {(row.get('error') or '')[:80]}"
            for row in (q.get("probe_errors") or []):
                head += f"\n     ◷ {row['ref']} [{row.get('verdict')}]: {(row.get('error') or '')[:80]}"
        for m in (data.get("models") or []):
            tags = []
            if m.get("free"):
                tags.append("Free" + (f" via {','.join(m['free_via'])}" if m.get("free_via") else ""))
            if not m.get("usable"):
                tags.append("キー未設定")
            if m.get("probe") and m["probe"] != "alive":
                tags.append(f"要再確認={m['probe']}")
            ctx = f" ctx={m['context_length']}" if m.get("context_length") else ""
            head += f"\n  • {m['ref']}{ctx} [{', '.join(tags) or '有料'}]"
        if not (data.get("models") or []):
            hint = ("該当なし。query を短くするか provider を外してください" if not q.get("probed")
                    else "生存確認で全滅しました（キー・権限を確認してください）")
            head += f"\n  （{hint}）"
    for row in (data.get("stats") or [])[:12]:
        head += (f"\n  • {row['model']} [{row['status']}] 品質 {row['quality']}"
                 f" 観測 {row['observations']}")
    return head


def _render_ask(data: dict) -> str:
    """`freeagent_ask` の本文（`content` = 人間向け日本語）。"""
    out = [f"[{data.get('served_by')}] {data.get('answer', '')}"]
    meta = [f"{data.get('latency_s')}s"]
    if data.get("fell_back"):
        meta.append(f"代替へ回った（要求 {data.get('model')}）")
    if data.get("truncated"):
        meta.append("切断あり")
    if data.get("cot_leak"):
        meta.append("思考過程の混入")
    if data.get("quota_accounting"):
        meta.append(f"クォータ警告: {data['quota_accounting']}")
    out.append("(" + ", ".join(str(m) for m in meta) + ")")
    return "\n".join(out)


def _render_fanout(data: dict) -> str:
    """`freeagent_fanout` の本文（`content` = 人間向け日本語）。"""
    head = (f"{data.get('calls')} 呼び出し / 成功 {data.get('ok')} / 失敗 {data.get('failed')}"
            f" / wall {data.get('wall_s')}s（逐次なら約 {data.get('sequential_estimate_s')}s、"
            f"倍率 {data.get('speedup')}）")
    if data.get("slot_waited"):
        head += f" / 提供元の枠待ち {data['slot_waited']} 件（429 を踏まずに順番待ちで吸収）"
    if data.get("quota_report_error"):
        head += f"\n⚠️ クォータ報告失敗: {data['quota_report_error']}"
    head += "".join(f"\n{line}" for line in _dropped_lines(data, "prompts", S.MAX_PROMPTS_PER_CALL))
    subs = data.get("substitutions") or {}
    if S.as_int(subs.get("count"), 0, 0, 10 ** 9):
        who = ", ".join(f"{row.get('served_by')}×{row.get('n')}" for row in (subs.get("models") or [])[:3])
        head += f"\nℹ️ うち {subs.get('count')} 件は別モデルが回答（{who}）"
    body = ""
    for row in (data.get("results") or [])[:12]:
        text = row.get("answer") or row.get("error") or ""
        body += f"\n  • {row['model']}: {S.truncate(text, 200)}"
    for index, row in enumerate(data.get("results") or [], 1):
        if row.get("quota_accounting"):
            body += f"\n  ⚠️ #{index} クォータ: {row['quota_accounting']}"
    return head + body


def _render_panel(data: dict) -> str:
    """`freeagent_panel` の本文（`content` = 人間向け日本語）。"""
    conf_mean = data.get("confidence_mean")
    conf_note = f"（確信度平均 {conf_mean}）" if isinstance(conf_mean, (int, float)) else ""
    parts = data.get("participants") or {}
    asked = S.as_int(parts.get("requested"), 0, 0, 10 ** 9) or len(data.get("models") or [])
    got = S.as_int(parts.get("answered"), 0, 0, 10 ** 9) or S.as_int(data.get("answered"), 0, 0, 10 ** 9)
    agree_src = S.as_str(data.get("agreement_source"))
    agree_note = "（完全一致）" if agree_src == "exact" else ("（類似度）" if agree_src == "similarity" else "")
    lines = [f"回答 {got}/{asked} 体"
             f" / 独立した実モデル {data.get('independent_sources', data.get('answered'))} 体"
             f" / 合意度 {data.get('agreement')}{agree_note}{conf_note}"]
    subs = data.get("substitutions") or {}
    if S.as_int(subs.get("count"), 0, 0, 10 ** 9):
        who = ", ".join(f"{row.get('served_by')}×{row.get('n')}" for row in (subs.get("models") or [])[:3])
        lines.append(f"ℹ️ うち {subs.get('count')} 件は別モデルが回答（{who}）")
    if S.as_str(data.get("confidence_warning")):
        lines.append(f"⚠️ {data['confidence_warning']}")
    dropped = parts.get("dropped") or []
    if dropped:
        reasons = {}
        for row in dropped:
            reasons[row.get("reason")] = reasons.get(row.get("reason"), 0) + 1
        detail = ", ".join(f"{k} {v}" for k, v in sorted(reasons.items()))
        lines.append(f"ℹ️ 応答しなかった {len(dropped)} 体（{detail}）")
    if data.get("slot_waited"):
        lines.append(f"ℹ️ 提供元の枠待ち {data['slot_waited']} 件（429 を踏まずに順番待ちで吸収しました）")
    if data.get("quota_report_error"):
        lines.append(f"⚠️ クォータ報告失敗: {data['quota_report_error']}")
    for group in (data.get("consensus") or [])[:5]:
        lines.append(f"  • {', '.join(group['models'])}: {S.truncate(group['excerpt'], 160)}")
    if data.get("open_questions"):
        lines.append("⚠️ メインに確認したい点:")
        for row in data["open_questions"][:5]:
            lines.append(f"  - {row['model']}: {S.truncate(row['question'], 160)}")
    refined = data.get("refined")
    for index, row in enumerate(data.get("answers") or [], 1):
        if row.get("quota_accounting"):
            lines.append(f"⚠️ #{index} クォータ: {row['quota_accounting']}")
    if isinstance(refined, dict):
        if refined.get("quota_accounting"):
            lines.append(f"⚠️ 統合クォータ: {refined['quota_accounting']}")
        if refined.get("error"):
            lines.append(f"⚠️ 統合に失敗: {S.truncate(refined['error'], 120)}")
        elif refined.get("conclusion"):
            lines.append(f"🔗 統合（{refined.get('model')}）: {S.truncate(refined['conclusion'], 220)}")
    if agree_src == "exact":
        lines.append("ℹ️ 合意度は短答の完全一致（記号・数値・短い語）で数えたもので、正しさの保証ではありません。")
    else:
        lines.append("ℹ️ 合意度は表層の一致であって正しさの保証ではありません。")
    return "\n".join(lines)


def _render_lookup(data: dict) -> str:
    """`freeagent_lookup` の本文（`content` = 人間向け日本語）。"""
    timings = data.get("timings") or {}
    late = data.get("timed_out") or []
    head = f"出典 {data.get('citation_count')} 件（{', '.join(data.get('sources') or [])}）"
    if late:
        head += f" / 締め切り {data.get('deadline_s')} 秒に間に合わず {len(late)} 件"
    lines = [head]
    if data.get("citations_in_results") is False:
        lines.append("ℹ️ 引用は citations に統合済み（results の各ソース内には重複して置きません）")
    # **指定したのに使えなかったソース名を隠さない**（typo を黙って捨てない）。
    unknown = data.get("unknown_sources") or []
    if unknown:
        lines.append(f"  ⚠ 未知のソースを無視しました: {', '.join(unknown)}"
                     "（名前は sources の説明を参照）")

    def secs(src: str) -> str:
        t = timings.get(src)
        return f"・{t:.1f} 秒" if isinstance(t, (int, float)) else ""

    for src, res in (data.get("results") or {}).items():
        items = res.get("items") or []
        if res.get("timed_out"):
            lines.append(f"  ⏱ {src}: 締め切りに間に合いませんでした（取得は継続・次回はキャッシュから）")
            continue
        if not items:
            lines.append(f"  × {src}{secs(src)}: {(res.get('error') or '該当なし')[:90]}")
            if res.get("fallback_attempt"):
                lines.append(f"      代替 datacite も失敗: {S.truncate(res['fallback_attempt'].get('error') or '', 90)}")
            continue
        acquisition = (res.get("fallback") or {}).get("served_by")
        label = f"{src} → {acquisition}（代替）" if acquisition else src
        lines.append(f"  ✓ {label} ({len(items)} 件{secs(src)})")
        if acquisition:
            lines.append(f"      主系: {S.truncate(res['fallback'].get('primary_error') or '', 100)}")
        if res.get("attribution"):
            lines.append(f"      {res['attribution']}")
        for item in items[:2]:
            title = item.get("title") or item.get("label") or ""
            lines.append(f"      {S.truncate(title, 90)} — {item.get('url', '')}")
    return "\n".join(lines)


def _render_grounded(data: dict) -> str:
    """`freeagent_grounded` の本文（`content` = 人間向け日本語）。"""
    lines = [f"根拠 {data.get('evidence_citation_count', data.get('citation_count'))} 件 / 引用付き回答 "
             f"{data.get('answers_with_citations')}/{data.get('answered')} 体"
             f" / 合意度 {data.get('agreement')}"]
    if data.get("quota_report_error"):
        lines.append(f"⚠️ クォータ報告失敗: {data['quota_report_error']}")
    for index, row in enumerate(data.get("answers") or [], 1):
        if row.get("quota_accounting"):
            lines.append(f"⚠️ #{index} クォータ: {row['quota_accounting']}")
    warning_end = len(lines)
    # 検索の失敗（未知のソース名など）も人間に見せる（typo を黙って捨てない）。
    lookup = data.get("lookup")
    lookup = lookup if isinstance(lookup, dict) else {}
    unknown = lookup.get("unknown_sources") or data.get("unknown_sources") or []
    if unknown:
        lines.append(f"⚠ 未知のソースを無視しました: {', '.join(unknown)}"
                     "（名前は sources の説明を参照）")
    # 質問文と検索語が違うときは**どちらで引いたか**を出す（0 件の切り分けができる）。
    searched, asked = S.as_str(data.get("search_query")), S.as_str(data.get("question"))
    if searched and asked and searched != asked:
        lines.append(f"検索語: {S.truncate(searched, 120)}")
    for row in (data.get("answers") or []):
        if row.get("error"):
            lines.append(f"  • {row['model']}: ✗ {S.truncate(row['error'], 120)}")
            continue
        lines.append(f"  • {row['model']}: {S.truncate(row.get('answer') or '', 400)}")
    lines.append("出典:")
    for i, cite in enumerate(data.get("citations") or [], 1):
        lines.append(f"  [{i}] [{cite.get('source')}] {S.truncate(cite.get('title') or '', 80)} {cite.get('url')}")
    credits = [c for cite in (data.get("citations") or []) for c in
               ((cite.get("attributions") or []) + ([cite["attribution"]] if cite.get("attribution") else []))]
    if credits:
        lines[warning_end:warning_end] = list(dict.fromkeys(credits))
    return "\n".join(lines[:40])


def _dropped_lines(data: dict, label: str, cap: int) -> list[str]:
    """上限で実行しなかった件数の警告行（規約 63・無ければ空）。

    同じ穴を 4 回書き直さないよう 1 箇所に置く。**「切り捨てた」ことを必ず本文に出す**。
    """
    dropped = S.as_int(data.get(f"{label}_dropped"), 0, 0, 100000)
    if not dropped:
        return []
    return [f"⚠️ {label} は上限 {cap} 件までです（{dropped} 件は使いませんでした。"
            f"分割して呼び直してください）"]


def _render_map(data: dict) -> str:
    """`freeagent_map` の本文（`content` = 人間向け日本語）。"""
    lines = [f"{data.get('count')} 件 / 成功 {data.get('ok')} / 失敗 {data.get('failed')}"
             f"（モデル {data.get('model')}）"]
    if data.get("wall_s") is not None:
        speed = data.get("speedup")
        seq = data.get("sequential_estimate_s")
        extra = f"（逐次なら {seq}s・{speed} 倍）" if seq and speed else ""
        lines.append(f"  wall {data.get('wall_s')}s{extra}")
    lines.extend(_dropped_lines(data, "items", S.MAX_MAP_ITEMS))
    for row in (data.get("results") or [])[:10]:
        text = row.get("output") or f"✗ {row.get('error', '')}"
        lines.append(f"  • {S.truncate(row.get('item') or '', 60)} → {S.truncate(text, 160)}")
    for index, row in enumerate(data.get("results") or [], 1):
        if row.get("quota_accounting"):
            lines.append(f"⚠️ #{index} クォータ: {row['quota_accounting']}")
    if data.get("reduce_quota_accounting"):
        lines.append(f"⚠️ 統合クォータ: {data['reduce_quota_accounting']}")
    if data.get("reduced"):
        lines.append(f"統合:\n{S.truncate(data['reduced'], 1200)}")
    return "\n".join(lines)


def _render_consult(data: dict) -> str:
    """`freeagent_consult` の本文（`content` = 人間向け日本語）。"""
    lines = [f"相談 {data.get('session_id')} / mode={data.get('mode')} / "
             f"ラウンド{data.get('rounds_run')} / stage={data.get('stage')}",
             f"合意度 {data.get('agreement')} / 確信度平均 {data.get('confidence_mean')}"]
    if data.get("quota_report_error"):
        lines.append(f"⚠️ クォータ報告失敗: {data['quota_report_error']}")
    for warning in data.get("quota_accounting") or []:
        lines.append(f"⚠️ 第{warning['round']}ラウンド {warning['model']} クォータ: {warning['warning']}")
    failed_rows = data.get("failed") or []
    shown = {(row.get("model"), row.get("error")) for row in failed_rows}
    for row in failed_rows:
        lines.append(f"  ✗ {row.get('model')}（このラウンドは脱落）: {S.truncate(row.get('error') or '', 90)}")
    for row in (data.get("consensus") or [])[:5]:
        conf = row.get("confidence")
        suffix = f"（確信度 {conf}）" if isinstance(conf, int) else ""
        lines.append(f"  • {row['model']}: {S.truncate(row.get('conclusion') or '', 160)}{suffix}")
    debate = data.get("debate_summary")
    if debate:
        lines.append("討論推移（表層合意度）: "
                     + " → ".join(str(x) for x in debate.get("agreement_by_round", [])))
        for p in debate.get("participants", [])[:5]:
            lines.append(f"  ◦ {p['model']}: 初回「{S.truncate(p.get('initial_position') or '', 60)}」"
                         f" → 最終「{S.truncate(p.get('final_position') or '', 60)}」"
                         + ("（立場変更）" if p.get("changed") else ""))
            if p.get("strongest_objection"):
                lines.append(f"      反論: {S.truncate(p['strongest_objection'], 120)}")
        for row in debate.get("dropped") or []:
            if (row.get("model"), row.get("error")) in shown:
                continue  # 直近ラウンドの脱落として既に出している（二重表示しない）
            lines.append(f"  ✗ {row.get('model')}（第{row.get('round')}ラウンド "
                         f"{row.get('kind')} で脱落）: {S.truncate(row.get('error') or '', 90)}")
    if data.get("redactions"):
        lines.append(f"伏せ字: 上流へ送る前に {data['redactions']} 件の秘密を伏せました"
                     "（FREEAGENT_REDACT_SECRETS=0 で無効）")
    fs = data.get("findings_summary")
    if fs:
        lines.append(f"重要度: critical {fs.get('critical', 0)} / required {fs.get('required', 0)} / "
                     f"optional {fs.get('optional', 0)} / nit {fs.get('nit', 0)}"
                     + (f" / 語彙外 {fs['unknown']}" if fs.get("unknown") else "")
                     + ("（**blocking**）" if fs.get("blocking") else "（差し戻し不要）"))
        cs = data.get("compliance_summary")
        if cs:
            lines.append(f"適合: compliant {cs.get('compliant', 0)} / partial {cs.get('partial', 0)} "
                         f"/ noncompliant {cs.get('noncompliant', 0)} / 判定なし {cs.get('unknown', 0)}"
                         + ("（**blocking**）" if cs.get("blocking") else "（適合）"))
        if fs.get("truncated"):
            lines.append(f"⚠️ 切り詰められた回答 {fs['truncated']} 件"
                         "（max_tokens を上げるか、draft を短くしてください）")
    cov = data.get("coverage") or {}
    if cov:
        lines.append(f"質問: 未回答 {cov.get('outstanding', 0)} / 保留 {cov.get('deferred', 0)} / "
                     f"解消 {len(cov.get('resolved') or [])}")
    if data.get("open_questions_for_main"):
        lines.append("⚠️ メインに確認したい点:")
        defaults = {q["question"]: q.get("default") for q in (data.get("open_questions") or [])}
        for q in data["open_questions_for_main"][:5]:
            default = defaults.get(q) or ""
            lines.append(f"  - {q}" + (f"（既定: {default}）" if default else ""))
        if data.get("deferred_questions"):
            lines.append(f"  （上限 {S.QUESTION_CAP} 件を超えた "
                         f"{len(data['deferred_questions'])} 件は deferred_questions にあります）")
        lines.append("→ 回答して session_id を付けて再呼び出ししてください（main_reply）")
    lines.append("ℹ️ 最終判断はメイン。サブの出力は仮説として扱ってください。")
    return "\n".join(lines)


def _render_think(data: dict) -> str:
    """`freeagent_think` の本文（`content` = 人間向け日本語）。"""
    ledger = data.get("ledger") or {}
    branches = ledger.get("branches") or []
    head = (f"思考 #{data.get('step') or '-'}（記録 {ledger.get('steps_recorded', 0)} 件"
            f" / 分岐 {len(branches)} / 修正 {len(ledger.get('revisions') or [])}）")
    if data.get("total_thoughts"):
        head += f" / 見積り総数 {data['total_thoughts']}"
        if data.get("total_auto_adjusted"):
            head += "（自動で引き上げ）"
    if data.get("view"):
        head = ("【台帳の閲覧（記録なし・要約）】" if data.get("brief")
                else "【台帳の閲覧（記録なし）】") + head
    lines = [head]
    plan = ledger.get("plan") or []
    if plan:
        prog = ledger.get("plan_progress") or {}
        retired_done = S.as_int(ledger.get("plan_retired_done"), 0, 0, 999)
        # 進捗は**今の plan の項目だけ**で数える。廃止した達成済みは減って見えるので、
        # 消さずに別枠で明示する（実測: 1/3 だった達成が plan の差し替えで 0/3 に見えた）。
        note = f"（廃止した達成済み {retired_done} 件は含みません）" if retired_done else ""
        lines.append(f"計画: {prog.get('done', 0)}/{prog.get('total', len(plan))} 達成{note}")
        for item in plan:
            mark = "✅" if item.get("done") else "□"
            refs = "".join(f" #{x}" for x in (item.get("steps") or []))
            # 見出しは**位置**（1 始まり）。id は同一性なので同じ値なら省略する。
            pos = item.get("position") or item.get("id")
            ident = item.get("id")
            tail = "" if ident is None or ident == pos else f"（id={ident}）"
            lines.append(f"  {mark} {pos}. {S.truncate(item.get('text') or '', 100)}{tail}"
                         + (f"（{refs.strip()}）" if refs else ""))
    state_ja = {"open": "未決着", "adopted": "採用", "abandoned": "棄却", "merged": "統合"}
    if branches:
        lines.append("分岐: " + " / ".join(
            f"{b.get('branch_id')}"
            + (f"（#{b['from']} から・" if b.get("from") else "（")
            + f"{state_ja.get(b.get('status') or 'open', b.get('status'))}）"
            for b in branches))
    hyps = ledger.get("hypotheses") or []
    if hyps:
        hyp_ja = {"open": "未検証", "supported": "支持", "refuted": "反証", "inconclusive": "保留"}
        lines.append("仮説: " + " / ".join(
            f"#{h.get('n')} {hyp_ja.get(h.get('status') or 'open', h.get('status'))}"
            + ("（改訂済み）" if h.get("superseded_by") else "") for h in hyps))
    # 要約（brief）は現行の道筋だけなので全部出す。通常は末尾だけ（閲覧 12 / 記録 5）。
    shown_rows = ledger.get("latest") or []
    if not data.get("brief"):
        shown_rows = shown_rows[-(12 if data.get("view") else 5):]
    for row in shown_rows:
        tag = f" [{row['branch_id']}]" if row.get("branch_id") else ""
        tag += " [修正]" if row.get("is_revision") else ""
        if row.get("kind") == "hypothesis":
            tag += " [仮説]"
        elif row.get("kind") == "test" and row.get("tests_hypothesis"):
            tag += f" [#{row['tests_hypothesis']} の検証]"
        elif row.get("kind") == "conclusion":
            tag += " [結論]"
        if row.get("superseded_by"):
            tag += f" [#{row['superseded_by']} で改訂済み]"
        lines.append(f"  • #{row.get('n')}{tag}: {S.truncate(row.get('text') or '', 140)}")
    if data.get("brief") and ledger.get("omitted"):
        lines.append(f"  （改訂・棄却で外した思考 {ledger['omitted']} 件は省略。全文は brief なしで view）")
    verify = data.get("verification")
    if verify:
        counts = verify.get("verdicts") or {}
        conf = verify.get("confidence_mean")
        lines.append(f"検証（独立 {verify.get('answered')}/{len(verify.get('models') or [])} 体）: "
                     f"妥当 {counts.get('妥当', 0)} / 要修正 {counts.get('要修正', 0)}"
                     f" / 根拠不足 {counts.get('根拠不足', 0)}"
                     + (f" / 確信度平均 {conf}" if isinstance(conf, (int, float)) else ""))
        for row in (verify.get("answers") or []):
            if row.get("error"):
                lines.append(f"  ✗ {row.get('model')}: {S.truncate(row.get('error') or '', 100)}")
                continue
            lines.append(f"  ◦ {row.get('model')}: {row.get('verdict') or '判定なし'}")
            if row.get("objection"):
                lines.append(f"      反証: {S.truncate(row['objection'], 140)}")
            if row.get("oversight"):
                lines.append(f"      見落とし: {S.truncate(row['oversight'], 140)}")
        if verify.get("failed_rows"):
            lines.append(f"  ⚠️ 検証に失敗 {len(verify['failed_rows'])} 体（脱落は隠していません）")
    elif not data.get("view"):
        lines.append("検証なし（台帳のみ。verify=true で独立モデルの反証が付きます）")
    alt = data.get("alternatives")
    if alt:
        lines.append(f"代替案（独立 {alt.get('answered')}/{len(alt.get('models') or [])} 体・"
                     f"{len(alt.get('items') or [])} 件）:")
        for item in (alt.get("items") or [])[:8]:
            lines.append(f"  ◇ {item.get('model')}: {S.truncate(item.get('text') or '', 160)}")
        for row in (alt.get("failed_rows") or []):
            lines.append(f"  ✗ {row.get('model')}: {S.truncate(row.get('error') or '', 100)}")
    for note in (data.get("notes") or []):
        lines.append(f"⚠️ {note}")
    lines.append("ℹ️ 検証・代替案は独立モデルによる反証・提案で、合意は正しさの保証ではありません。")
    return "\n".join(lines)


def _render_agent(data: dict) -> str:
    """`freeagent_agent` の本文（`content` = 人間向け日本語）。"""
    lines = [f"【サブエージェント調査】{data.get('answered')}/{len(data.get('models') or [])} 体が回答"
             f" / ツール呼び出し {data.get('tool_calls')} 回"
             f" / 出典 {data.get('citation_count')} 件"
             f" / 根拠を引用した回答 {data.get('answers_with_citations')} 体"
             f" / 合意度 {data.get('agreement')}"]
    for row in (data.get("agents") or []):
        # 予算切れの行は `run_parallel` が入れる `{error, dropped}` で **`model` を持たない**
        # （実測: v0.23.1 で予算を渡した直後に `KeyError: 'model'` で表示だけが壊れ、
        # 取れていた agents まで消えた）。行の欠けで全体を落とさない。
        name = row.get("model") or "（未実行）"
        if row.get("error"):
            lines.append(f"  • {name}: ✗ {S.truncate(row['error'], 120)}")
            continue
        mark = "✅" if row.get("cited_ok") else "⚠️ 引用なし"
        lines.append(f"  • {name}（{row.get('steps')}ステップ, {mark}）: "
                     f"{S.truncate(row.get('answer') or '', 220)}")
        if row.get("unsupported_citations"):
            nums = ", ".join(f"[{n}]" for n in row["unsupported_citations"])
            lines.append(f"      ⚠️ 根拠に無い引用番号: {nums}")
        if row.get("steps_exhausted"):
            lines.append("      ⚠️ ステップ上限に達し、回答に到達しませんでした（推測はしません）")
            for ev in (row.get("evidence") or [])[:4]:
                lines.append(f"      根拠: {S.truncate(ev, 130)}")
        for step in (row.get("trace") or [])[:3]:
            lines.append(f"      ↳ {step['tool']}"
                         f"({S.truncate(S._dumps(step['args']), 60)})"
                         f" hits={step['hits']}")
    if data.get("citations"):
        lines.append("出典:")
        for cite in data["citations"][:6]:
            lines.append(f"  - [{cite.get('source')}] {S.truncate(cite.get('title') or '', 80)} {cite.get('url')}")
    lines.append(f"ℹ️ {data.get('usage_note', '')}")
    return "\n".join(lines)


def _render_delegate(data: dict) -> str:
    """`freeagent_delegate` の本文（`content` = 人間向け日本語）。"""
    enforced = data.get("verification_enforced")
    head = (f"exit={data.get('exit_code')}（{data.get('elapsed_s')}s）"
            + ("" if enforced is None else
               ("／報告条件: 強制（検証コマンドと出力の明記）" if enforced else "／報告条件: 未強制")))
    return f"{head}\n{S.truncate(data.get('stdout') or '', 2000)}"


# ツール別の本文レンダラ（`_render_body` から機械的に分離した）。
# 名前 → 関数の対応表。**分岐表を手で書くのは漏れるので、関数名から生成**する。


def _render_diagnose(data: dict) -> str:
    """診断の本文（人間向け）。**記録に無いことは「無い」と書く**。"""
    window = data.get("window") or {}
    totals = data.get("totals") or {}
    latency = data.get("latency") or {}
    advice = data.get("advice") or {}
    lines = [f"診断: 直近 {S.as_int(window.get('days'), 7, 0, 365)} 日"
             f"（{S.as_str(window.get('since_utc'))} 〜 {S.as_str(window.get('until_utc'))}）"]
    filters = window.get("filters") or {}
    if filters.get("kind") or filters.get("model"):
        lines.append(f"絞り込み: kind={S.as_str(filters.get('kind')) or '-'} / "
                     f"model={S.as_str(filters.get('model')) or '-'}")
    calls = S.as_int(totals.get("calls"), 0, 0, 10 ** 9)
    if not calls:
        lines.append("呼び出しの記録がありません（traces が空・窓の外・絞り込みで 0 件）。")
    else:
        rate = totals.get("ok_rate")
        rate_text = f"{S.as_float(rate, 0.0):.1%}" if rate is not None else "-"
        lines.append(f"呼び出し {calls} 件 / 成功 {S.as_int(totals.get('ok'), 0, 0, 10 ** 9)}"
                     f" / 失敗 {S.as_int(totals.get('failed'), 0, 0, 10 ** 9)}（成功率 {rate_text}）")
    classes = data.get("by_error_class") or {}
    if classes:
        lines.append("  失敗の内訳: " + ", ".join(f"{k} {v}" for k, v in classes.items()))
    chans = data.get("totals_by_channel") or {}
    if chans:
        parts = []
        for name, label in (("tool", "本番（ツール経由）"), ("bench", "ベンチ直呼び")):
            slot = chans.get(name) or {}
            n = S.as_int(slot.get("calls"), 0, 0, 10 ** 9)
            if n:
                parts.append(f"{label} {S.as_int(slot.get('ok'), 0, 0, 10 ** 9)}/{n}")
        if parts:
            lines.append("  チャネル（成功/呼び出し）: " + ", ".join(parts))
    budget = data.get("budget") or {}
    for item in (budget.get("models") or [])[:3]:
        lines.append(f"  ⚠️ {item.get('model')} が max_tokens の {item.get('max_ratio')} 倍まで返しています"
                     f"（{item.get('n')} 件・予算超過）")
    finish = data.get("by_finish_reason") or {}
    if finish:
        lines.append("  finish_reason: "
                     + ", ".join(f"{k} {v}" for k, v in list(finish.items())[:5]))
    kinds = data.get("by_kind") or {}
    if kinds:
        parts = []
        for name, slot in list(kinds.items())[:6]:
            n = S.as_int((slot or {}).get("calls"), 0, 0, 10 ** 9)
            ok = S.as_int((slot or {}).get("ok"), 0, 0, 10 ** 9)
            parts.append(f"{name} {ok}/{n}")
        lines.append("  種別（成功/呼び出し）: " + ", ".join(parts))
    if S.as_int(latency.get("n"), 0, 0, 10 ** 9):
        lines.append(f"  遅延: p50 {latency.get('p50')}s / p90 {latency.get('p90')}s"
                     f" / p95 {latency.get('p95')}s / p99 {latency.get('p99')}s"
                     f"（n={S.as_int(latency.get('n'), 0, 0, 10 ** 9)}）")
    slow = data.get("slowest_models") or []
    if slow:
        lines.append("  遅いモデル（p95）:")
        for row in slow[:5]:
            lines.append(f"    - {S.as_str(row.get('model'))} n={S.as_int(row.get('n'), 0, 0, 10 ** 9)}"
                         f" p50 {row.get('p50')}s / p95 {row.get('p95')}s")
    errors = data.get("top_errors") or []
    if errors:
        lines.append("  多い失敗:")
        for row in errors[:5]:
            lines.append(f"    - {S.as_int(row.get('count'), 0, 0, 10 ** 9)} 件: {S.truncate(S.as_str(row.get('error')), 100)}")
    runs = data.get("runs") or []
    if runs:
        lines.append(f"  呼び出し単位の内訳（{S.as_int(data.get('runs_total'), 0, 0, 10 ** 9)} 件中、失敗の多い順）:")
        for row in runs[:5]:
            classes = row.get("error_classes") or {}
            detail = (" [" + ", ".join(f"{k} {v}" for k, v in list(classes.items())[:3]) + "]") if classes else ""
            lines.append(f"    - {S.as_str(row.get('at'))} {S.as_str(row.get('tool'))} {S.as_str(row.get('run'))}"
                         f" {S.as_int(row.get('ok'), 0, 0, 10 ** 9)}/{S.as_int(row.get('calls'), 0, 0, 10 ** 9)}{detail}")
    recent = data.get("recent_failures") or []
    if recent:
        lines.append("  直近の失敗:")
        for row in recent[:5]:
            lines.append(f"    - {S.as_str(row.get('at'))} {S.as_str(row.get('kind'))}"
                         f" {S.as_str(row.get('model'))} [{S.as_str(row.get('error_class'))}]"
                         f" {S.truncate(S.as_str(row.get('error')), 90)}")
    if advice:
        lines.append(f"次の一手（{S.as_str(advice.get('kind')) or '-'}）: {S.as_str(advice.get('advice'))}")
        check = advice.get("check") or []
        if check:
            lines.append("  確認する設定: " + ", ".join(check))
    for note in data.get("notes") or []:
        lines.append(f"※ {note}")
    return "\n".join(lines)


def _render_body(name: str, data: dict) -> str:
    """ツールの応答を `content`（人間向け日本語）にする。

    ツール別の描画は `S._RENDERERS`（ツール名 → 描画関数）へ分けた。
    描画関数はこの関数の `name == ...` 分岐をそのまま移したもの（**振る舞いは同一**）。
    """
    if not isinstance(data, dict):
        return str(data)[:2000]
    if data.get("error"):
        # エラーでも**原因になる情報**は残す（未知のソース名など、切り分けに必要）。
        extra = ""
        lookup = data.get("lookup")
        lookup = lookup if isinstance(lookup, dict) else {}
        unknown = lookup.get("unknown_sources") or data.get("unknown_sources") or []
        if unknown:
            extra = ("\n⚠ 未知のソースを無視しました: " + ", ".join(unknown)
                     + "（名前は sources の説明を参照）")
        errs = lookup.get("errors") or data.get("errors")
        if isinstance(errs, dict) and errs:
            detail = "; ".join(f"{k}: {S.truncate(str(v), 90)}" for k, v in list(errs.items())[:5])
            extra += f"\n内訳: {detail}"
        return f"⚠️ {data['error']}{extra}"
    renderer = S._RENDERERS.get(name)
    if renderer is not None:
        return renderer(data)

    return S.truncate(S._dumps(data), 2000)


# server.py 側の互換（テストは server.<name> を直接参照する）
EXPORTS = {
    "_render_models": _render_models,
    "_render_ask": _render_ask,
    "_render_fanout": _render_fanout,
    "_render_panel": _render_panel,
    "_render_lookup": _render_lookup,
    "_render_grounded": _render_grounded,
    "_dropped_lines": _dropped_lines,
    "_render_map": _render_map,
    "_render_consult": _render_consult,
    "_render_think": _render_think,
    "_render_agent": _render_agent,
    "_render_delegate": _render_delegate,
    "_render_diagnose": _render_diagnose,
    "_render_body": _render_body,
}
