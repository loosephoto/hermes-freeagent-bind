"""§6 ツール実装（プロンプト・応答の解析・models/ask/fanout/panel・統合）

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import re

from . import server as S  # noqa: E402

CONSULT_SYSTEM = (
    "あなたはメインLLMの補佐です。出力は必ず次の3行だけで答えてください。\n"
    "結論: <1〜3文。断定を避け、確信が無い部分は「不明」と書く>\n"
    "確信度: <0〜100 の整数>\n"
    "メインに確認したい点: <前提が足りず判断できない点。無ければ なし>\n"
    "前置き・思考過程・挨拶・Markdown の見出しは書かない。"
)


DEBATE_SYSTEM = (
    "あなたは複数の立場を比較する補佐です。出力は必ず次の4行だけです。\n"
    "立場: <あなたの立場を1文>\n"
    "最強の反論: <自分と異なる立場への、最も強い反論を1〜2文>\n"
    "応答: <その反論への応答を1〜2文>\n"
    "未解決: <合意できずに残った点。無ければ なし>"
)


REVIEW_SYSTEM = (
    "あなたは下書きの査読者です。出力は必ず次の4行だけです。\n"
    "結論: <総評を1〜3文>\n"
    "重要度: <critical / required / optional / nit のうち最も重いものを1つ>\n"
    "指摘: <根拠つきの指摘を最大3個。各「[軸] 箇所 — 理由」。軸は correctness / readability / "
    "architecture / security / performance>\n"
    "メインに確認したい点: <前提が足りず判断できない点を1つだけ。推奨の既定値を「既定: …」で添える。"
    "無ければ なし>\n"
    "前置き・思考過程・挨拶・Markdown の見出しは書かない。"
)


COMPLIANCE_SYSTEM = (
    "あなたは仕様適合の査読者です。**品質の良し悪しは判定せず**、下書きが【仕様・計画】の要件を"
    "満たしているかだけを見てください。出力は必ず次の4行だけです。\n"
    "結論: <満たしている点と足りない点を1〜3文>\n"
    "判定: <compliant / partial / noncompliant のうち1つ>\n"
    "指摘: <満たしていない要件を最大3個。各「[要件] 該当箇所 — 何が足りないか」>\n"
    "メインに確認したい点: <前提が足りず判断できない点を1つだけ。推奨の既定値を「既定: …」で"
    "添える。無ければ なし>\n"
    "前置き・思考過程・挨拶・Markdown の見出しは書かない。"
)


REVIEW_AXES = ("correctness", "readability", "architecture", "security", "performance")


REVIEW_SEVERITIES = ("critical", "required", "optional", "nit")


_SEVERITY_ALIAS = {
    "critical": "critical", "クリティカル": "critical", "重大": "critical", "blocker": "critical",
    "required": "required", "必須": "required", "high": "required",
    "optional": "optional", "任意": "optional", "推奨": "optional", "medium": "optional",
    "nit": "nit", "軽微": "nit", "low": "nit", "nitpick": "nit",
}


QUESTION_CAP = 5


AGENT_SYSTEM_FINAL = (
    "これ以上のツール呼び出しはできません。集めた情報だけを根拠に、最終回答を JSON 1つで出してください。\n"
    '  {"answer": "<回答。使った根拠の番号 [n] を本文に書き、根拠が足りない点は「根拠に無い」と明記>"}\n'
    "根拠を 1 つも使っていない場合は [0] を書いてください。JSON 以外の文字を書かない。"
)


AGENT_SYSTEM = (
    "あなたは調査補佐です。次のいずれか**1つだけ**を JSON で出力してください。\n"
    '  ツールを使う: {"tool": "lookup", "query": "<検索語>", "sources": ["arxiv","crossref",...]}\n'
    '  使える source: wikipedia, wikidata, arxiv, crossref, openalex, github, datacite, openaire, zenodo, ror, doaj, npm, crates, cinii\n'
    '  追加（明示指定のみ）: osv, ietf, inspirehep, oeis, hfhub, hn, swh, librariesio, ia, qiita, openreview, mdn, devdocs, nvd, terraform, zbmath\n'
    '  追加sourceは明示指定のみ。datacite_kind: all / arxiv / dataset。fallback: trueでarXivのDataCite代替を許可。\n'
    '  回答する:     {"answer": "<回答。使った根拠の番号 [n] を本文に書く>"}\n'
    "ツール結果は [1] [2] … の番号つきで返ります。回答では使った根拠の番号を本文に書き、"
    "根拠に無い事実は書かない（書けない点は「根拠に無い」と明記する）。"
    "JSON 以外の文字（前置き・コードフェンス）を書かない。根拠が足りなければツールを使う。"
)


THINK_CRITIC_SYSTEM = (
    "あなたは思考ステップの検証者です。同意を探すのではなく**反証**を探してください。"
    "出力は必ず次の4行だけです。\n"
    "判定: <妥当 / 要修正 / 根拠不足 のいずれか1つ>\n"
    "反証: <この思考への最も強い反証を1〜2文。無ければ なし>\n"
    "見落とし: <検討されていない観点を1つ。無ければ なし>\n"
    "確信度: <0〜100 の整数>\n"
    "前置き・思考過程・挨拶・Markdown の見出しは書かない。"
)


_LABEL_CONCLUSION = re.compile(
    r"(?:結論|まとめ|conclusion)\s*[:：]\s*(.+?)(?=\s+(?:確信度|自信|confidence|メインに確認したい点|確認したい点|質問|questions?)\s*[:：]|$)", re.I)


_LABEL_CONFIDENCE = re.compile(r"(?:確信度|自信|confidence)\s*[:：]?\s*([0-9]{1,3}(?:\.[0-9]+)?|0?\.[0-9]+)", re.I)


_LABEL_QUESTION = re.compile(r"(?:メインに確認したい点|確認したい点|質問|questions?)\s*[:：]\s*(.+)", re.I)


_NO_ANSWER = ("なし", "無し", "ありません", "none", "-", "n/a", "特になし")


def parse_labeled(text: str) -> dict:
    """「結論/確信度/確認したい点」形式の回答を構造化する。取れない項目は None（推測しない）。"""
    out = {"conclusion": "", "confidence": None, "question": "", "labels_found": 0}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LABEL_CONCLUSION.search(line)
        if m and not out["conclusion"]:
            out["conclusion"] = S.truncate(m.group(1).strip().strip("* "), 600)
            out["labels_found"] += 1
        m = _LABEL_CONFIDENCE.search(line)
        if m and out["confidence"] is None:
            raw = m.group(1)
            val = S.as_float(raw, -1.0)
            if "." in raw and val <= 1.0:
                val *= 100.0
            if 0 <= val <= 100:
                out["confidence"] = int(val)
                out["labels_found"] += 1
        m = _LABEL_QUESTION.search(line)
        if m and not out["question"]:
            value = m.group(1).strip().strip("* ")
            empty_value = value.lower().rstrip(" .。!?！？;；").strip()
            out["question"] = "" if empty_value in _NO_ANSWER else S.truncate(value, 400)
            out["labels_found"] += 1
    return out


_LABEL_SEVERITY = re.compile(r"(?:重要度|severity|重大度)\s*[:：]\s*([A-Za-zぁ-んァ-ヶ一-龠]{2,12})", re.I)


_LABEL_FINDINGS = re.compile(r"(?:指摘|findings?)\s*[:：]\s*(.+)", re.I)


_LABEL_DEFAULT = re.compile(r"既定\s*[:：]\s*([^。\n]{1,60})")


def parse_review(text: str) -> dict:
    """査読回答から重要度・指摘・軸を取る。取れない項目は空（推測で埋めない）。"""
    out = {"severity": "", "findings": [], "axes": []}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LABEL_SEVERITY.search(line)
        if m and not out["severity"]:
            out["severity"] = _SEVERITY_ALIAS.get(m.group(1).strip().lower(), "unknown")
        m = _LABEL_FINDINGS.search(line)
        if m and len(out["findings"]) < 6:
            out["findings"].append(S.truncate(m.group(1).strip().strip("* "), 240))
    low = (text or "").lower()
    out["axes"] = [axis for axis in REVIEW_AXES if axis in low]
    return out


def summarize_review(rows: list[dict]) -> dict:
    """査読の重要度を数える。**語彙外も、名乗らない回答も unknown** として blocking に数える。

    査読モードの system は重要度の表明を要求しているので、無言は「軽い」ではなく「不明」として扱う。
    """
    counts = {sev: 0 for sev in REVIEW_SEVERITIES}
    counts["unknown"] = 0
    reviewed = 0
    for row in rows:
        if row.get("error"):
            continue
        reviewed += 1
        sev = S.as_str(row.get("severity")).strip().lower()
        if sev in counts:
            counts[sev] += 1
        else:
            counts["unknown"] += 1
    counts["blocking"] = counts["critical"] + counts["required"] + counts["unknown"] > 0
    counts["reviewed"] = reviewed
    counts["unreviewed"] = len(rows) - reviewed
    return counts


_LABEL_COMPLIANCE = re.compile(
    r"(?:判定|適合|compliance)\s*[:：]\s*([A-Za-zぁ-んァ-ヶ一-龠]{2,16})", re.I)


_COMPLIANCE_ALIAS = {
    "compliant": "compliant", "適合": "compliant", "ok": "compliant", "満たす": "compliant",
    "partial": "partial", "一部適合": "partial", "一部": "partial", "部分的": "partial",
    "noncompliant": "noncompliant", "不適合": "noncompliant", "ng": "noncompliant",
    "満たさない": "noncompliant",
}


def parse_compliance(text: str) -> dict:
    """仕様適合の判定を取る。語彙外・名乗りなしは `unknown` として残す（推測で丸めない）。"""
    out = {"verdict": "", "findings": []}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LABEL_COMPLIANCE.search(line)
        if m and not out["verdict"]:
            out["verdict"] = _COMPLIANCE_ALIAS.get(m.group(1).strip().lower(), "unknown")
        m = _LABEL_FINDINGS.search(line)
        if m and len(out["findings"]) < 6:
            out["findings"].append(S.truncate(m.group(1).strip().strip("* "), 240))
    return out


def summarize_compliance(rows: list[dict]) -> dict:
    """適合判定を数える。**noncompliant と unknown（名乗りなし）は blocking**。"""
    counts = {"compliant": 0, "partial": 0, "noncompliant": 0, "unknown": 0}
    reviewed = 0
    for row in rows:
        if row.get("error"):
            continue
        reviewed += 1
        verdict = S.as_str(row.get("verdict")).strip().lower()
        if verdict in counts:
            counts[verdict] += 1
        else:
            counts["unknown"] += 1
    counts["blocking"] = counts["noncompliant"] + counts["unknown"] + len(rows) - reviewed > 0
    counts["reviewed"] = reviewed
    counts["unreviewed"] = len(rows) - reviewed
    return counts


def parse_question_default(text: str) -> tuple[str, str]:
    """質問文から「既定: …」を切り離す（本文は変えない。既定値は別のキーで返す）。"""
    raw = (text or "").strip()
    m = _LABEL_DEFAULT.search(raw)
    return raw, (S.truncate(m.group(1).strip(), 120) if m else "")


_LABEL_VERDICT = re.compile(
    r"(?:判定|verdict|assessment)\s*[:：]\s*(妥当|要修正|根拠不足|ok|needs?[ _-]?revision|insufficient)",
    re.I)


_LABEL_OBJECTION = re.compile(r"(?:反証|反論|counter(?:argument)?)\s*[:：]\s*(.+)", re.I)


_LABEL_OVERSIGHT = re.compile(r"(?:見落とし|抜け|oversight|missing)\s*[:：]\s*(.+)", re.I)


def _is_no_answer(value: str) -> bool:
    """「なし」系の空ラベルかどうか（`なし` を反証として数えないための判定）。"""
    return (value or "").lower().rstrip(" .。!?！？;；").strip() in _NO_ANSWER


def parse_verdict(text: str) -> dict:
    """検証者の「判定/反証/見落とし/確信度」を構造化する。取れない項目は空（推測しない）。"""
    out = {"verdict": "", "objection": "", "oversight": "", "confidence": None, "labels_found": 0}
    for line in (text if isinstance(text, str) else "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LABEL_VERDICT.search(line)
        if m and not out["verdict"]:
            raw = m.group(1).lower()
            if raw in ("妥当", "ok"):
                out["verdict"] = "妥当"
            elif raw.startswith("needs") or raw == "要修正":
                out["verdict"] = "要修正"
            else:
                out["verdict"] = "根拠不足"
            out["labels_found"] += 1
        m = _LABEL_OBJECTION.search(line)
        if m and not out["objection"]:
            value = m.group(1).strip().strip("* ")
            out["objection"] = "" if _is_no_answer(value) else S.truncate(value, 400)
            out["labels_found"] += 1
        m = _LABEL_OVERSIGHT.search(line)
        if m and not out["oversight"]:
            value = m.group(1).strip().strip("* ")
            out["oversight"] = "" if _is_no_answer(value) else S.truncate(value, 400)
            out["labels_found"] += 1
        m = _LABEL_CONFIDENCE.search(line)
        if m and out["confidence"] is None:
            raw = m.group(1)
            val = S.as_float(raw, -1.0)
            if "." in raw and val <= 1.0:
                val *= 100.0
            if 0 <= val <= 100:
                out["confidence"] = int(val)
                out["labels_found"] += 1
    return out


def _no_models() -> dict:
    status = S.provider_status()
    ready = [row["provider"] for row in status if row["ready"]]
    return {
        "error": "推論可能な Free モデルが 0 件です。"
                 "`hermes proxy start` が動いているか、プロバイダのキーを確認してください。",
        "providers": status,
        "ready_providers": ready,
    }


def _expand_models(names: list[str]) -> tuple[list[str], bool]:
    """`models: ["all"]`（または `*` / `全部`）を**在庫の全 chat Free** へ展開する。

    戻り値は (refs, 上限で切り詰めたか)。名指しした ref を先に残し、そのうえで在庫を足す。
    上限は `FREEAGENT_MAX_MODELS_PER_CALL`（既定 16）。`freeagent_models` の `ref` を並べる代わりに
    「今 使える Free を全部」を 1 語で指定するための入口（ツールを増やさずに済ませる）。
    """
    wildcard = ("all", "*", "全部", "すべて")
    toks = [n.strip() for n in names if isinstance(n, str) and n.strip()]
    if any(t.lower() in wildcard for t in toks):
        out = [t for t in toks if t.lower() not in wildcard]
        for ref in S.free_model_refs(free_only=True, chat_only=True):
            if ref not in out:
                out.append(ref)
    else:
        out = toks
    return out[:S.MAX_MODELS_PER_CALL], len(out) > S.MAX_MODELS_PER_CALL


def _select_or_error(args: dict, default_size: int = 3, kind: str | None = None) -> tuple[list[str], dict]:
    """`models`（`all` 展開つき）→ `prefer` → 品質統計の順に参加モデルを決める。"""
    requested, cut = _expand_models(S.as_str_list(args.get("models")))
    size = S.as_int(args.get("size"), default_size, 1, S.MAX_SUB_MODELS)
    if requested:
        size = max(size, len(requested))     # 明示指定は size で切り落とさない
    refs, info = S.select_models(size, requested or None,
                               prefer=S.as_str_list(args.get("prefer")) or None,
                               exclude=S.as_str_list(args.get("exclude")) or None, kind=kind)
    if cut:
        info.setdefault("notes", []).append(
            f"models は上限 {S.MAX_MODELS_PER_CALL} 件で切りました（FREEAGENT_MAX_MODELS_PER_CALL）")
    return refs, info


def _rate_limit_report(answers: list[dict]) -> dict:
    limited = [a for a in answers if a.get("rate_limited")]
    skipped = sorted({ref for a in answers for ref in (a.get("skipped_cooling") or [])})
    # 「429 を踏まずに枠待ちで吸収した」件数（0 なら提供元の枠に余裕があった）。
    return {"rate_limited": len(limited), "skipped_cooling": skipped,
            "slot_waited": sum(1 for a in answers if (a.get("gate_wait_s") or 0) > 0)}


def _consensus_groups(entries: list[dict]) -> list[dict]:
    """結論が似ている回答をまとめる（表層の類似であって正しさの一致ではない）。"""
    groups: list[dict] = []
    for entry in entries:
        text = entry.get("conclusion") or entry.get("answer") or ""
        placed = False
        for group in groups:
            if S.similarity(text, group["representative"]) >= 0.55:
                group["models"].append(entry["model"])
                placed = True
                break
        if not placed:
            groups.append({"representative": text, "models": [entry["model"]],
                           "excerpt": S.truncate(text, 200)})
    return groups


def tool_models(args: dict) -> dict:
    """利用可能なモデルを**検索**し、必要なら**生存確認**する。

    `query` / `provider` を付けると、その条件に合うモデルを（Free かどうか付きで）返す。引数なしなら
    プロバイダの要約と Free モデル一覧を返す。`probe=true` なら候補を実際に 1 回ずつ呼び、
    **一覧に載っているが呼べないモデル**（廃止 410 / アカウント未有効 404 など）を除外する。
    ここで得た `provider/model`（HF は `:提供元` を付けられる）は他ツールの `models` 引数に渡せる。
    """
    status = S.provider_status()
    free = S.free_model_refs(chat_only=True)          # サブLLM に使える Free だけ（§3.2）
    # **在庫と「いま使える」を分ける**（v0.50.0・issue #12）。直近 404/410 で廃止と判定したモデルは
    # 一覧には残す（理由つき）が、`usable_now` と自動選抜からは外す。既定の鮮度は
    # `FREEAGENT_GONE_TTL_H`（既定 24 時間）で、期限が切れたら再確認に戻る。
    gone = S.gone_refs()
    include_gone = S.as_flag(args.get("include_gone"))
    usable = [ref for ref in free if not S.is_cooling(ref) and ref not in gone]

    def _fresh_row(row: dict) -> dict:
        ref = S.make_ref(row["provider"], row["id"])
        info = gone.get(ref) or S.model_freshness(ref)
        item = {
            "ref": ref,
            "provider": row["provider"],
            "id": row["id"],
            "free": bool(row.get("free")),
            "free_via": row.get("free_via") or [],
            "context_length": row.get("context_length"),
            "usable": S.provider_ready(row["provider"]),
            "probe": row.get("probe"),
            "gone": bool(info.get("gone")),
        }
        if info:
            item["freshness"] = info
        return item
    query = S.as_str(args.get("query")) or S.as_str(args.get("q"))
    provider_filter = S.as_str(args.get("provider"))
    limit = S.as_int(args.get("limit"), 40, 1, 200)
    shown: list[dict] = []          # 検索モードで絞り込んだ一覧（非検索モードでは空）
    data = {
        "providers": status,
        "total_models": sum(row["models"] for row in status),
        "free_candidates": len(free),
        "non_chat_candidates": sum(row.get("non_chat", 0) for row in status),
        "usable_now": len(usable),
        # 在庫・使用可・廃止の 3 段（差が説明できない数字を出さない）。`gone_models` は理由つきで返す。
        "gone": len(gone),
        "gone_models": [{"ref": ref, "age_s": info.get("gone_age_s"),
                         "reason": info.get("gone_reason") or "",
                         "retry_in_s": info.get("gone_expires_in_s")}
                        for ref, info in list(gone.items())[:20]],
        "include_gone": include_gone,
        "default_model": S.default_model(),
        "cooling": {ref: row for ref, row in S.cooling_refs().items()},
        "ranking_enabled": S.RANK_ENABLED,
    }
    harness = S.harness_status()   # §8.6（initialize 前は None）
    if harness:
        data["harness"] = harness

    searching = bool(query or provider_filter or args.get("all") or args.get("probe"))
    # probe は実際にモデルを呼ぶため、free_only 省略時でも有料モデルは対象にしない。
    free_only = S.as_flag(args.get("free_only")) or bool(args.get("probe"))
    if searching:
        rows = S.all_models()
        needle = query.lower()
        matched = [r for r in rows
                   if (not provider_filter or r.get("provider") == provider_filter)
                   and (not free_only or r.get("free"))
                   and (not needle or needle in str(r.get("id", "")).lower())]
        if args.get("probe") and not provider_filter:
            ready_rows = [r for r in matched if S.provider_ready(S.as_str(r.get("provider")))]
            if ready_rows:
                matched = ready_rows
        matched.sort(key=lambda r: (not r.get("free"),
                                    not S.provider_ready(S.as_str(r.get("provider"))),
                                    str(r.get("id", ""))))
        matched_free = sum(1 for r in matched if r.get("free"))   # ページ前の総数（offset で減らさない）
        offset = S.as_int(args.get("offset"), 0, 0, 5000)
        matched = matched[offset:]
        data["query"] = {
            "query": query, "provider": provider_filter or None,
            "offset": offset,
            "free_only": free_only,
            "matched": len(matched) + offset,
            "matched_free": matched_free,
            "shown": min(len(matched), limit),
            "probed": False,
        }
        shown = matched[:limit]
        if not include_gone:
            # 廃止は**隠さずに数える**（`gone_models` に理由が出る）。行だけ既定で伏せる。
            hidden = [r for r in shown if S.make_ref(r["provider"], r["id"]) in gone]
            if hidden:
                shown = [r for r in shown if S.make_ref(r["provider"], r["id"]) not in gone]
                data["query"]["gone_hidden"] = len(hidden)
        if args.get("probe") and shown:
            # 一覧は実態と乖離する（実測: NVIDIA の一覧 82 件の大半が 410 EOL、HF は権限で 403）。
            # 実際に 1 回だけ呼んで**呼べるものだけ**を残す。件数は絞る（呼び出しは課金・レートに触れる）。
            probe_limit = S.as_int(args.get("probe_limit"), min(len(shown), 12), 1, 40)
            shown = [r for r in shown if r.get("free")][:probe_limit]
            probe_prompt = S.as_str(args.get("probe_prompt")) or "1+1=? 数字1つだけ。"
            # 既定を 16 から 64 へ上げる（v0.50.0・issue #12）。思考モデルは 16 トークンだと
            # 本文 0 で終わり、「廃止」ではなく `slow`（空応答）と判定されるが、利用者には
            # 「死んでいる」ように見える。空応答は `call_model` 側で床まで引き上げて再試行もする。
            probe_tokens = S.as_int(args.get("probe_max_tokens"), 64, 1, 512)
            probe_timeout = S._env_float("FREEAGENT_PROBE_TIMEOUT", 25.0)
            probe_workers = max(1, min(8, S._env_int("FREEAGENT_PROBE_WORKERS", 8),
                                       len(shown))) if shown else 1

            def check(row: dict) -> dict:
                model = row["id"]
                if S._is_hf(row["provider"]) and row.get("free_via"):
                    model = f"{model}:{row['free_via'][0]}"
                # **フォールバックを切る**: 有効なままだと他プロバイダが答えて「生存」と誤判定する
                # （実測: HF の 403 が OpenRouter の応答で隠れ、生きているように見えた）。
                # 読み取りタイムアウトは短くする（既定 180 秒だと 1 件の遅いモデルが探索全体を止める）。
                res = S.call_model(S.make_ref(row["provider"], model), probe_prompt,
                                 max_tokens=probe_tokens, kind="probe", allow_fallback=False,
                                 timeout=probe_timeout)
                err = res.get("error") or ""
                if res.get("cooling_reason"):
                    err = f"{err} / {res['cooling_reason']}"
                err_l = err.lower()
                if not err:
                    verdict = "alive"
                elif "timed out" in err_l or "timeout" in err_l:
                    # **遅いだけかもしれない**（NVIDIA はコールドスタートが長い）。死んだ扱いにしない。
                    verdict = "slow"
                elif "空応答" in err:
                    # 応答自体は返っている（思考トークンで probe の予算を使い切った）。生存側に倒す。
                    verdict = "slow"
                elif "http 404" in err_l or "http 410" in err_l or (
                    "not found" in err_l and not re.search(r"\bhttp\s+[45]\d\d\b", err_l)
                ):
                    verdict = "gone"
                elif "http 403" in err_l:
                    # 403 は**署名があるときだけ**権限なし扱い。Cloudflare のブロック（Error 1010 等）や
                    # 提供元の障害・モデル単位の制限は、プロバイダ全体の問題ではないので `error`（残す）。
                    # 401 より先に見る（"HTTP 401/403" のような表記が 401 に誤マッチしていた実測がある）。
                    verdict = "auth" if S._is_auth_error(403, err) else "error"
                elif "http 401" in err_l or "未設定/未確認です" in err:
                    verdict = "auth"
                else:
                    # 429（提供元が一時制限）や 5xx。**除外しない**（生きているが今は応えない）。
                    verdict = "error"
                # 予算遵守の記録（v0.47.0・issue #8 の案2）。**実効の予算**と比べる:
                # 空応答リトライでサーバが 64→512 へ引き上げた場合、64 と比べると誤検出になる
                # （v0.42.5 の実測で「モデルが守らない」は 0 件だった）。
                _tok = res.get("tokens") or {}
                _eff = res.get("max_tokens") or probe_tokens
                _comp = S.as_int(_tok.get("completion"), 0, 0, 10 ** 9)
                budget = {"asked": int(probe_tokens), "effective": int(_eff),
                          "completion": _comp,
                          "chars": len((res.get("text") or "").strip()),
                          "respected": bool(_comp <= int(_eff))}
                return {"model": row, "verdict": verdict, "served_by": res.get("served_by"),
                        "error": S.truncate(err, 200),
                        "sample": S.truncate((res.get("text") or "").strip(), 60),
                        "budget": budget}

            probed = S.run_parallel(shown, check, max_workers=probe_workers, deadline=S.call_budget())
            # 消えたもの（404/410）と権限で拒否されたものだけ一覧から外す。**遅い・一時エラーは残す**
            # （残さないと「まだ生きているが遅い」モデルを永久に隠すことになる）。
            dropped = {"gone", "auth"}
            alive = [r["model"] for r in probed if r["verdict"] == "alive"]
            kept = [r["model"] for r in probed if r["verdict"] not in dropped]
            data["query"].update({
                "probed": True, "probe_attempted": len(probed),
                "probe_alive": len(alive),
                "probe_slow": sum(1 for r in probed if r["verdict"] == "slow"),
                "probe_dropped": [{"ref": S.make_ref(r["model"]["provider"], r["model"]["id"]),
                                   "verdict": r["verdict"], "error": r["error"]}
                                  for r in probed if r["verdict"] in dropped],
                "probe_errors": [{"ref": S.make_ref(r["model"]["provider"], r["model"]["id"]),
                                  "verdict": r["verdict"], "error": r["error"]}
                                 for r in probed if r["verdict"] in ("slow", "error")],
                # 予算遵守（issue #8 の案2）。逸脱が居れば名指しで返す（黙って 0 件にしない）。
                "probe_budget": {
                    "checked": sum(1 for r in probed if (r.get("budget") or {}).get("completion")),
                    "not_respected": [
                        {"ref": S.make_ref(r["model"]["provider"], r["model"]["id"]),
                         "asked": r["budget"]["asked"], "effective": r["budget"]["effective"],
                         "completion": r["budget"]["completion"], "chars": r["budget"]["chars"]}
                        for r in probed if (r.get("budget") or {}).get("respected") is False],
                    "note": "実効の予算（リトライで引き上げた後）と completion を比べています。",
                },
            })
            by_id = {(r["model"]["provider"], r["model"]["id"]): r["verdict"] for r in probed}
            shown = [dict(m, probe=by_id.get((m["provider"], m["id"])) or "alive") for m in kept]
        data["models"] = [_fresh_row(r) for r in shown]
    if args.get("stats"):
        data["stats"] = [
            {"model": ref, "status": S.model_status(ref), "quality": S.model_quality(ref),
             "observations": S.model_observations(ref)}
            for ref in free
        ]
    # **返り値のキーはモードで変えない**。`models`（絞り込んだ一覧）と
    # `free_models`（サブLLMに使える Free 全体）は**常に両方**を返す。片方だけだと
    # 利用側がモードで別のキーを読む羽目になる（実測: 引数なし → free_models のみ /
    # query 付き → models のみ）。
    data["models"] = [_fresh_row(r) for r in shown]
    # `free_models` は**在庫**（廃止を含む）。使えるかどうかは `usable_now` / 各 `ref` の `gone` を見る。
    data["free_models"] = free
    return data


def tool_ask(args: dict) -> dict:
    """1 つのサブLLMへ 1 回だけ問い合わせる。"""
    prompt = S.as_str(args.get("prompt"))
    if not prompt:
        return {"error": "prompt は必須です（空文字は不可）"}
    model = S.as_str(args.get("model")) or S.default_model()
    result = S.call_model(model, prompt, system=S.as_str(args.get("system")),
                        max_tokens=S.as_int(args.get("max_tokens"), 800, 16, 8000),
                        temperature=S.as_float(args.get("temperature"), None),
                        kind="ask")
    if result.get("error"):
        return {"error": result["error"], "model": result.get("ref"), "kind": "ask",
                "rate_limited": result.get("rate_limited"),
                **({"quota_accounting": result["quota_accounting"]} if result.get("quota_accounting") else {})}
    return {
        "answer": result["text"],
        "model": result["ref"],
        "served_by": result.get("served_by"),
        "fell_back": bool(result.get("fallback")),
        "latency_s": result.get("latency_s"),
        "truncated": result.get("truncated"),
        "cot_leak": result.get("cot_leak"),
        "tokens": result.get("tokens"),
        **({"quota_accounting": result["quota_accounting"]} if result.get("quota_accounting") else {}),
        "quality": {"status": S.model_status(result["ref"]), "score": S.model_quality(result["ref"])},
    }


def tool_fanout(args: dict) -> dict:
    """プロンプト × モデルを並列に実行する（相互検証・ベストオブN・多数決）。"""
    prompts = S.as_str_list(args.get("prompts") or args.get("items"))
    if not prompts:
        single = S.as_str(args.get("prompt"))
        prompts = [single] if single else []
    if not prompts:
        return {"error": "prompts（または prompt）は必須です"}
    prompts_dropped = max(0, len(prompts) - S.MAX_PROMPTS_PER_CALL)
    prompts = prompts[:S.MAX_PROMPTS_PER_CALL]
    explicit, cut = _expand_models(S.as_str_list(args.get("models")))
    if explicit:
        refs = explicit
        info = {"requested": True, "models": refs,
                "notes": [f"models は上限 {S.MAX_MODELS_PER_CALL} 件で切りました"
                          "（FREEAGENT_MAX_MODELS_PER_CALL）"] if cut else []}
    else:
        size = S.as_int(args.get("size"), 2, 1, S.MAX_SUB_MODELS)
        refs, info = S.select_models(size, None, kind="fanout")
    if not refs:
        return S._no_models()
    system = S.as_str(args.get("system"))
    max_tokens = S.as_int(args.get("max_tokens"), 800, 16, 8000)
    pairs = [(ref, prompt) for prompt in prompts for ref in refs]
    if len(pairs) > S.MAX_CALLS_PER_RUN:
        return {"error": f"呼び出し数が上限を超えています（{len(pairs)} > {S.MAX_CALLS_PER_RUN}）。"
                         "prompts か size を減らしてください"}
    started = S.now_ts()
    results = S.ask_map(pairs, system=system, max_tokens=max_tokens, kind="fanout")
    wall = round(S.now_ts() - started, 3)
    rows = []
    for (ref, prompt), res in zip(pairs, results):
        row = {"model": ref, "prompt": S.truncate(prompt, 200)}
        if res.get("quota_accounting"):
            row["quota_accounting"] = res["quota_accounting"]
        if res.get("coalesced"):
            row["coalesced"] = True      # 同一実行内の重複を畳んだ（上流は 1 回だけ）
        if res.get("error"):
            row["error"] = res["error"]
        else:
            row["answer"] = res["text"]
            row["served_by"] = res.get("served_by")
            row["cot_leak"] = res.get("cot_leak")
            row["truncated"] = res.get("truncated")
        rows.append(row)
    ok = sum(1 for r in rows if not r.get("error"))
    coalesced_calls = sum(1 for r in rows if r.get("coalesced"))
    sequential = round(sum(S.as_float(r.get("latency_s"), 0.0) for r in results), 3)
    return {
        "calls": len(rows), "ok": ok, "failed": len(rows) - ok, "coalesced_calls": coalesced_calls,
        "wall_s": wall, "sequential_estimate_s": sequential,
        "speedup": round(sequential / wall, 2) if wall > 0 else None,
        "results": rows, "selection": info, "concurrency": S.PROVIDER_GATE.report(refs),
        **S._quota_report_after_answers(refs),
        "prompts_dropped": prompts_dropped,
        "participants": S.participants_of(refs, rows),
        "substitutions": S.substitutions_of(rows),
        **_rate_limit_report(results),
    }


_REFINE_PROMPT = (
    "あなたは複数の独立した回答を統合する編集者です。平均せず、対立点は事実確認して決めてください。\n\n"
    "【問い】\n{question}\n\n【候補の回答】\n{candidates}\n\n"
    "次の形式だけで答えてください。\n"
    "結論: （統合した最終回答）\n"
    "確信度: （0-100 の整数）\n"
    "根拠: （候補のうち、どの番号の主張を採用し、どれを却下したか）"
)


REFINE_BAND = max(0.0, min(0.5, S._env_float("FREEAGENT_REFINE_BAND", 0.05)))


def _pick_refiner(candidates: list[str], fallback: str = "品質順の先頭") -> tuple[str, str]:
    """統合役を選ぶ。品質の帯（`S.REFINE_BAND`）の中で**平均所要が速い**モデルを優先する。

    帯に該当が無い（学習前・帯の外しか居ない）ときは `fallback` の文面で品質順の先頭を返す
    （**表示の後方互換**＝既存の `refined.reason` の文言を保つ）。
    """
    ranked = S.rank_models(candidates)
    if not ranked:
        return "", ""
    if S.REFINE_BAND <= 0.0:
        return ranked[0], fallback
    best = S._rank_score(ranked[0])
    band = [ref for ref in ranked if best - S._rank_score(ref) <= S.REFINE_BAND]
    observed = [ref for ref in band if S.model_mean_latency(ref) is not None]
    if len(band) > 1 and observed:
        fastest = min(observed, key=lambda ref: float(S.model_mean_latency(ref) or 0.0))
        if fastest != ranked[0]:
            return fastest, (f"品質の帯（差 {S.REFINE_BAND} 以内）で最も速い"
                             f"（{float(S.model_mean_latency(fastest) or 0.0):.0f}ms）")
    return ranked[0], fallback


def _refine_answers(question: str, answers: list[dict], refs: list[str], args: dict) -> dict:
    """参加者の回答を 1 つの答えに統合する（MoA の集約層）。失敗も dict で返す（規約 1）。"""
    good = [a for a in answers if not a.get("error")]
    if not good:
        return {"model": None, "calls": 0, "error": "統合できる回答がありません（全員脱落）"}
    if S.budget_expired():
        # 予算を使い切った後の統合は 300 秒のクライアント待ちに当たるだけ（本体の結果は返す）。
        return {"model": None, "calls": 0,
                "error": "呼び出しの予算を使い切ったため統合を実行しませんでした（パネルの回答は有効です）"}
    requested = S.as_str(args.get("refine_model"))
    if requested:
        model, why = requested, "refine_model の指定"
    else:
        # 統合役は**参加していないモデル**（要求 ref と実応答 served_by の両方）を優先する。
        # 同じモデルに自分の答えを統合させると合意度が上がるだけで新しい情報が入らない。
        # 全員参加しているときだけ最上位が兼務する。
        used = set(refs) | {S.as_str(a.get("served_by")) for a in good if S.as_str(a.get("served_by"))}
        ex_set = set(S.as_str_list(args.get("exclude")) + S.env_excluded_models())
        cooling = S.cooling_refs()
        blocked = {p for p in S.PROVIDER_ORDER if S.provider_auth_blocked(p)}
        pool = [r for r in S.free_model_refs(free_only=True, chat_only=True)
                if r not in used and not S._is_model_excluded(r, ex_set)
                and r not in cooling and r.split("/", 1)[0] not in blocked]
        if pool:
            model, why = S._pick_refiner(pool, fallback="参加していないモデルの最上位")
        else:
            fallback_pool = [r for r in refs if not S._is_model_excluded(r, ex_set)] or list(refs)
            model, why = S._pick_refiner(fallback_pool, fallback="全員参加のため最上位が兼務")
    candidates = "\n".join(
        f"[{i + 1}] {a['model']}: {S.truncate(a.get('answer') or a.get('conclusion') or '', 900)}"
        for i, a in enumerate(good))
    res = S.call_model(model, _REFINE_PROMPT.format(question=question, candidates=candidates),
                     max_tokens=S.as_int(args.get("max_tokens"), 500, 16, 4000),
                     temperature=0.0, kind="panel_refine")
    out: dict = {"model": model, "calls": 1, "reason": why,
                 "from_panel": model in (set(refs) if requested else used)}
    if res.get("quota_accounting"):
        out["quota_accounting"] = res["quota_accounting"]
    if res.get("error"):
        out["error"] = S.truncate(res["error"], 240)
        return out
    parsed = parse_labeled(res["text"])
    out.update({
        "served_by": res.get("served_by"),
        "text": S.truncate(res["text"], 2000),
        "text_truncated": len(res["text"]) > 2000,
        "conclusion": parsed["conclusion"] or S.truncate(res["text"], 400),
        "confidence": parsed["confidence"],
        "cot_leak": res.get("cot_leak"),
        "truncated": res.get("truncated"),
        "rate_limited": res.get("rate_limited"),
        "gate_wait_s": res.get("gate_wait_s"),
    })
    return out


def tool_panel(args: dict) -> dict:
    """同じ問いを複数モデルへ投げ、合意度・不一致・CoT混入つきで返す。"""
    question = S.as_str(args.get("question") or args.get("prompt"))
    if not question:
        return {"error": "question は必須です"}
    refs, info = S._select_or_error(args, default_size=3, kind="panel")
    if not refs:
        return S._no_models()
    results = S.ask_many(refs, question, system=S.as_str(args.get("system")) or CONSULT_SYSTEM,
                       max_tokens=S.as_int(args.get("max_tokens"), 500, 16, 4000), kind="panel",
                       avoid=S.as_str_list(args.get("exclude")) or None)
    answers = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            answers.append({"model": ref, "error": res["error"],
                            **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
            continue
        parsed = parse_labeled(res["text"])
        answers.append({
            "model": ref, "served_by": res.get("served_by"),
            "label": parsed["conclusion"] or S.truncate(res["text"], 400),
            "conclusion": parsed["conclusion"] or S.truncate(res["text"], 400),
            "confidence": parsed["confidence"], "labels_found": parsed["labels_found"],
            "question": parsed["question"], "answer": S.truncate(res["text"], 1500),
            "text_truncated": len(res["text"]) > 1500,
            "cot_leak": res.get("cot_leak"), "truncated": res.get("truncated"),
            **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {}),
        })
    good = [a for a in answers if not a.get("error")]
    independent = S.independent_answers(good)
    conf = [a["confidence"] for a in independent if isinstance(a.get("confidence"), int)]
    agree = S.agreement_detail([a["conclusion"] for a in independent])
    # `refine: true` で MoA の集約層（§6.12）。統合役の呼び出しも枠・クォータ・統計を通る。
    refine = _refine_answers(question, answers, refs, args) if S.as_flag(args.get("refine")) else None
    reported = results + ([refine] if refine and refine.get("calls") else [])
    data = {
        "question": question, "models": refs, "selection": info,
        "answered": len(good), "independent_sources": len(independent), "failed": len(answers) - len(good),
        "agreement": agree["value"], "agreement_source": agree["source"],
        "agreement_votes": agree["votes"],
        "consensus_share": agree["share"], "consensus_clusters": agree["clusters"],
        "consensus_note": S.consensus_note(),
        "participants": S.participants_of(refs, answers),
        "substitutions": S.substitutions_of(answers),
        "confidence_mean": round(sum(conf) / len(conf), 1) if conf else None,
        "confidence_source": "self_report",
        "confidence_warning": S.confidence_warning(
            round(sum(conf) / len(conf), 1) if conf else None, agree["value"]),
        "consensus": _consensus_groups(independent),
        "disagreements": [a["model"] for a in good if a.get("question")],
        "open_questions": [{"model": a["model"], "question": a["question"]}
                           for a in good if a.get("question")],
        "answers": answers,
        "concurrency": S.PROVIDER_GATE.report(refs),
        **S._quota_report_after_answers(refs),
        **_rate_limit_report(reported),
        "agreement_note": S._agreement_note(agree["source"]),
    }
    if refine is not None:
        data["refined"] = refine
        data["calls"] = len(results) + int(refine.get("calls") or 0)
    return data


def tool_lookup(args: dict) -> dict:
    """外部知識を出典つきで取得する（LLM を使わない＝幻覚が入らない経路）。"""
    query = S.as_str(args.get("query") or args.get("topic") or args.get("q"))
    if not query:
        return {"error": "query は必須です"}
    return S.knowledge_lookup(query,
                            S.as_str_list(args.get("sources")) or None,
                            limit=S.as_int(args.get("limit"), 3, 1, 10),
                            lang=S.as_str(args.get("lang"), ""),
                            kind=S.as_str(args.get("github_kind"), "repo"),
                            datacite_kind=S.as_str(args.get("datacite_kind"), "all"), fallback=args.get("fallback") is True,
                            doc=S.as_str(args.get("doc")))


_EVIDENCE_ITEM_CHARS = max(0, min(2000, S._env_int("FREEAGENT_EVIDENCE_ITEM_CHARS", 360)))


_EVIDENCE_TOTAL_CHARS = max(0, min(20000, S._env_int("FREEAGENT_EVIDENCE_TOTAL_CHARS", 3200)))


EXPORTS = {
    "CONSULT_SYSTEM": CONSULT_SYSTEM,
    "DEBATE_SYSTEM": DEBATE_SYSTEM,
    "REVIEW_SYSTEM": REVIEW_SYSTEM,
    "COMPLIANCE_SYSTEM": COMPLIANCE_SYSTEM,
    "REVIEW_AXES": REVIEW_AXES,
    "REVIEW_SEVERITIES": REVIEW_SEVERITIES,
    "_SEVERITY_ALIAS": _SEVERITY_ALIAS,
    "QUESTION_CAP": QUESTION_CAP,
    "AGENT_SYSTEM_FINAL": AGENT_SYSTEM_FINAL,
    "AGENT_SYSTEM": AGENT_SYSTEM,
    "THINK_CRITIC_SYSTEM": THINK_CRITIC_SYSTEM,
    "_LABEL_CONCLUSION": _LABEL_CONCLUSION,
    "_LABEL_CONFIDENCE": _LABEL_CONFIDENCE,
    "_LABEL_QUESTION": _LABEL_QUESTION,
    "_NO_ANSWER": _NO_ANSWER,
    "parse_labeled": parse_labeled,
    "_LABEL_SEVERITY": _LABEL_SEVERITY,
    "_LABEL_FINDINGS": _LABEL_FINDINGS,
    "_LABEL_DEFAULT": _LABEL_DEFAULT,
    "parse_review": parse_review,
    "summarize_review": summarize_review,
    "_LABEL_COMPLIANCE": _LABEL_COMPLIANCE,
    "_COMPLIANCE_ALIAS": _COMPLIANCE_ALIAS,
    "parse_compliance": parse_compliance,
    "summarize_compliance": summarize_compliance,
    "parse_question_default": parse_question_default,
    "_LABEL_VERDICT": _LABEL_VERDICT,
    "_LABEL_OBJECTION": _LABEL_OBJECTION,
    "_LABEL_OVERSIGHT": _LABEL_OVERSIGHT,
    "_is_no_answer": _is_no_answer,
    "parse_verdict": parse_verdict,
    "_no_models": _no_models,
    "_expand_models": _expand_models,
    "_select_or_error": _select_or_error,
    "_rate_limit_report": _rate_limit_report,
    "_consensus_groups": _consensus_groups,
    "tool_models": tool_models,
    "tool_ask": tool_ask,
    "tool_fanout": tool_fanout,
    "_REFINE_PROMPT": _REFINE_PROMPT,
    "REFINE_BAND": REFINE_BAND,
    "_pick_refiner": _pick_refiner,
    "_refine_answers": _refine_answers,
    "tool_panel": tool_panel,
    "tool_lookup": tool_lookup,
    "_EVIDENCE_ITEM_CHARS": _EVIDENCE_ITEM_CHARS,
    "_EVIDENCE_TOTAL_CHARS": _EVIDENCE_TOTAL_CHARS,
}
