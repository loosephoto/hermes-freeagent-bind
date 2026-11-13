"""MCP Resources（知識のオンデマンド公開）と completion/complete

`server.py` の § から機械的に切り出した（**共有ヘルパーは `S.<name>` の属性参照**で呼ぶ。
テストが `patch.object(server, ...)` で差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations


from . import server as S  # noqa: E402

# ================================================================ §11 MCP Resources（知識のオンデマンド公開）
#
# `tools/list` は起動時に丸ごとモデルへ渡るので、変化の遅い長い知識を description に置くと毎回の文脈を食う。
# Resources は**呼ばれたときだけ**読まれる別チャネルなので、使い分け・在庫・鍵の入れ方・返り値の形を
# ここへ逃がす（v0.22.0）。Hermes は `capabilities.resources` を見て `list_resources` / `read_resource` を
# 自動で生やす（実測: hermes-agent の `tools/mcp_tool_registration.py` は capabilities で分岐する）。
# **read はネットワークを出さない**（登録簿と環境変数だけで組み立てる）。読むたびにプロバイダへ一覧を
# 要求すると、モデルが 1 回読むだけで上流の枠を使う（規約 54 の「早期返却で一覧を出さない」と同じ理由）。

RESOURCE_SCHEME = "resource://freeagent/"
RESOURCE_MIME = "text/markdown"


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Markdown の表。Resources の本文を組み立てるためだけに使う（content には使わない）。"""
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(out)


def _resource_api_rules() -> str:
    """どのツールを選ぶか（列挙は実装のツール名から書く）と、待ち時間の契約。"""
    return "\n".join([
        "# hermes-freeagent-bind の使い分け",
        "",
        "## どのツールを選ぶか",
        "",
        _md_table(["場面", "ツール"], [
            ["出典が要る事実確認（LLM を呼ばない＝幻覚が入らない）", "`freeagent_lookup`"],
            ["根拠を読ませた上で答えさせたい", "`freeagent_grounded`"],
            ["1 モデルに下読み・分類・要約を 1 回任せる", "`freeagent_ask`"],
            ["同じ問いを複数モデルに当てて多様性が要る", "`freeagent_panel` / `freeagent_fanout`"],
            ["大量の要素へ同じ指示を並列適用", "`freeagent_map`"],
            ["設計・デバッグなど 2 段以上の推論", "`freeagent_think` → 必要なら `freeagent_consult`"],
            ["在庫・Free 判定・生存確認", "`freeagent_models`"],
            ["失敗の理由・遅さを記録（traces）から決定論で知る", "`freeagent_diagnose`"],
            ["外部依頼の受付裁定・完了報告の証拠検査（決定論）", "`freeagent_admit`"],
            ["決められた手順で自分に調べさせる（読み取り専用）", "`freeagent_agent`"],
            ["フルツール付き Hermes を別プロセスで動かす（既定 off）", "`freeagent_delegate`"],
        ]),
        "",
        "## 手順が要るとき",
        "",
        "- 長い手順は Resource に置いてある（`resource://freeagent/skills` が目録）。",
        "  必要な 1 本だけ読む: `skills/plan` / `skills/review` / `skills/debug` / `skills/grounded`。",
        "",
        "## 失敗したとき",
        "",
        "- 失敗応答には `structuredContent.next_action`（`kind` / `advice` / `fallback_tools` / `check` / `reenable`）が付く。",
        "  同じ呼び出しを繰り返さず、そこの次の一手に従う。",
        "",
        "## 待ち時間と上限",
        "",
        f"- 1 回の呼び出しの予算は既定 {S.CALL_BUDGET_S:g} 秒（`FREEAGENT_CALL_BUDGET`。0 で無制限）。",
        f"- 知識取得の締め切りは既定 {S.KB_DEADLINE:g} 秒（`FREEAGENT_KB_DEADLINE`）。間に合わなかったソースは",
        "  `timed_out` として返り、裏の取得は続いて次回はキャッシュから返る。",
        f"- 同時実行は既定 {S.MAX_WORKERS} 本（`FREEAGENT_MAX_WORKERS`）。`fanout` のプロンプト上限は {S.MAX_PROMPTS_PER_CALL} 件、",
        f"  `map` の要素上限は {S.MAX_MAP_ITEMS} 件。上限で切った件数は `<label>_dropped` に残る（黙って捨てない）。",
    ])


def _resource_sources() -> str:
    """知識ソースの在庫。既定/明示の別は `DEFAULT_SOURCES` から引く（手で並べない・規約 5）。"""
    defaults = [name for name in S.DEFAULT_SOURCES if name in S.KB_BACKENDS]
    rows = [[name, "既定" if name in defaults else "明示指定のみ"] for name in S.KB_BACKENDS]
    return "\n".join([
        f"# 知識ソース（{len(S.KB_BACKENDS)} 種）",
        "",
        f"既定で引くのは {len(defaults)} 種: " + ", ".join(defaults),
        "",
        "明示指定は `freeagent_lookup` / `freeagent_grounded` の `sources` に名前を並べる",
        "（`sources` の説明に使う条件がある）。",
        "",
        _md_table(["source", "既定/明示"], rows),
    ])


def _resource_providers() -> str:
    """推論プロバイダと「何を入れれば ready か」。**鍵の値は出さない**（名前だけ）。"""
    rows = []
    for name in S.PROVIDER_ORDER:
        spec = S.PROVIDER_SPECS.get(name) or {}
        missing = S._provider_missing_settings(spec)
        rows.append([name, "ready" if S.provider_ready(name) else "未設定",
                     S.as_str(spec.get("key_env")) or "-",
                     ", ".join(missing) if missing else "-",
                     S.as_str(spec.get("free_kind")) or "-"])
    return "\n".join([
        f"# 推論プロバイダ（{len(S.PROVIDER_ORDER)} 社）",
        "",
        "`未設定` は鍵または確認フラグが足りない状態（**値はここに出さない**）。",
        "キーの置き場所は stdio なら MCP の `env`、常駐 HTTP なら `%LOCALAPPDATA%\\hermes\\.env`（Linux / macOS は `~/.hermes/.env`）。",
        "",
        _md_table(["provider", "状態", "key_env", "足りないもの", "free_kind"], rows),
    ])


def _resource_result_shape() -> str:
    """`content` / `structuredContent` の契約と、引用をどこから読むか。"""
    return "\n".join([
        "# 返り値の形（契約）",
        "",
        "- `content`: 人間向けの日本語テキスト（表・箇条書き）。LLM 向けの指示は書かない。",
        "- `structuredContent`: LLM 向けの純粋な JSON。失敗も `error` としてここに入る。",
        "- 失敗時は `structuredContent.next_action` に次の一手が入る。",
        "- `freeagent_lookup` の引用は**トップレベルの `citations`** に統合済み。",
        "  `results[source]` の中に同じ引用は重ねない（`citations_in_results` が false なら統合済み）。",
        "- 引用 1 件は `source` / `title` / `url` / `summary`（+ `year` / `doi` / `summary_kind` / `metadata_only`）。",
        "- 本文根拠が無いものは `metadata_only: true` で、根拠として数えない。",
    ])


# --- スキル（必要になったときだけ読む手順書）----------------------------------
#
# 外部の Agent Skills（`anthropics/skills` / `obra/superpowers`）の「段階開示」を、既存の Resource チャネルへ
# 写したもの。**ツール定義を増やさない**のが要点で、手順の本文は呼ばれたときだけ渡る（約 1〜1.5KB）。
# 本文は**実装の定数と登録簿から生成**する（数字や語彙を文章に書き写さない）。

def _resource_skills() -> str:
    """スキルの目録（どれをいつ読むか）。"""
    rows = [
        ["`resource://freeagent/skills/plan`", "大きめの作業を分解してから着手するとき", "plan の粒度・改訂・仮説"],
        ["`resource://freeagent/skills/review`", "成果物を査読させるとき（`consult` の review）", "4 行の型・重要度・5 軸・質問の上限"],
        ["`resource://freeagent/skills/debug`", "原因が分からない不具合を追うとき", "症状→再現→仮説→証拠→修正"],
        ["`resource://freeagent/skills/grounded`", "出典つきで答えさせたいとき", "検索語・ソース・引用の読み方"],
    ]
    return "\n".join([
        "# スキル（必要なときだけ読む手順書）",
        "",
        "ツール定義は起動時に丸ごと渡るので、**長い手順はここに置く**（読むのは必要なときだけ）。",
        "どれも 1〜1.5KB で、読んでも上流のモデルは呼ばれない（ネットワークも出ない）。",
        "",
        _md_table(["URI", "いつ読む", "中身"], rows),
        "",
        "## 使い方",
        "",
        "- まず目録（これ）を見て、必要な 1 本だけ読む。**全部を読み通さない**。",
        "- 手順は「型」であって、判断と責任はメイン LLM に残る。",
    ])


def _resource_skill_plan() -> str:
    """計画の型（`freeagent_think` の plan と検証の規律）。"""
    return "\n".join([
        "# 計画の立て方（`freeagent_think` の `plan`）",
        "",
        "- 各項目は **2〜5 分で終わる単位**で、**対象**（ファイル・節・コマンド）と**検証方法**まで書く。",
        "  検証できない項目は項目として数えない（数えた時点で「やったつもり」が増える）。",
        "- 1 項目終わるごとに検証を通してから次へ進む。**最後にまとめて検証しない**。",
        f"- 1 回のツール呼び出しの予算は既定 {S.CALL_BUDGET_S:g} 秒（`FREEAGENT_CALL_BUDGET`）。",
        "  長い調査は項目を分けて、その都度返す（切られると取れていた結果も失う）。",
        "",
        "## 台帳の使い方",
        "",
        "- 理解が変わったら `revises_thought`（消さずに改訂として残る）。",
        "- 代替案は `branch_from_thought` で分岐し、`resolve_branch` + `branch_status` で採用/棄却を記録する。",
        "- 原因の見当は `kind=\"hypothesis\"` で立て、`tests_hypothesis` + `hypothesis_status` で確かめる。",
        "- `total_thoughts` は見積り。進めながら増減してよい（自動でも引き上げられる）。",
        "- `subgoal` / `subgoal_done` は**同じ `session_id`** を付ける（落とすと別の台帳に当たる）。",
        "",
        "## 完了の条件",
        "",
        "- 「終わった」と言う前に、**実行したコマンドとその出力**で確かめる。確かめていない項目は未検証と書く。",
    ])


def _resource_skill_review() -> str:
    """査読の型（`freeagent_consult` の review モード）。"""
    return "\n".join([
        "# 査読の型（`freeagent_consult` の `mode=\"review\"` + `draft`）",
        "",
        "- 返るのは 4 行（結論 / 重要度 / 指摘 / 確認したい点）。重要度は "
        + " / ".join(f"`{sev}`" for sev in S.REVIEW_SEVERITIES) + "。",
        "- 見る軸は " + " / ".join(S.REVIEW_AXES) + " の 5 つ。**軸を名乗らない回答は `unknown`** として",
        f"  `blocking` に数える（保守側。空を「軽い」と読まない）。",
        f"- 質問は最大 {S.QUESTION_CAP} 件。超えた分は `deferred_questions` に残る（捨てない）。",
        "",
        "## 2 段で使う",
        "",
        "1. **仕様適合**: 頼まれたこと・合意した設計に合っているか。",
        "2. **品質**: 読めるか・安全か・テストがあるか。",
        "",
        "混ぜると「動くが読めない」「速いが仕様違い」を見逃す。v0.33.0 以降は 1 回の consult で 2 段を回せる。",
        "",
        "## 指摘の扱い",
        "",
        "- 人数で重みを付ける（4 体中 4 体が挙げたものは重い）。合意度の低さ＝意見が割れている、ではない。",
        "- 採否は自分で決める。直さない場合は理由を残す（黙って捨てない）。",
    ])


def _resource_skill_debug() -> str:
    """原因追及の型（4 段）。"""
    return "\n".join([
        "# デバッグの型（症状 → 再現 → 原因 → 修正）",
        "",
        "1. **症状を 1 行で書く**（何が、どこで、期待とどう違う）。",
        "2. **再現する最小手順**を作る。再現しないなら条件（時刻・状態・並列度・入力）を変えて切り分ける。",
        "3. **仮説を 2 つ以上**立て、消去法ではなく**証拠**（ログ・実測値・差分）で決める。",
        "4. 直す前に「直ったことを確かめる手段」（テスト・コマンド）を決める。",
        "5. 直したら同じ手順で再現しないことを示す。示せないなら**未検証**と書く。",
        "",
        "## このサーバーでの道具",
        "",
        "- 失敗の切り分け: `freeagent_diagnose`（traces の集計。失敗理由の内訳・遅いモデル・直近の失敗）。",
        "- 仮説の記録: `freeagent_think`（`kind=\"hypothesis\"` → `tests_hypothesis`）。",
        "- 独立した目: `freeagent_consult`（`mode=\"review\"`）に原因の説明を読ませる。",
        "",
        "**推測で原因を書かない**。記録に無いことは「分からない」と書き、次に測る手段を添える。",
    ])


def _resource_skill_grounded() -> str:
    """出典つきで答えるときの型。"""
    defaults = [name for name in S.DEFAULT_SOURCES if name in S.KB_BACKENDS]
    return "\n".join([
        "# 出典を使うとき（`freeagent_lookup` / `freeagent_grounded`）",
        "",
        "- まず `freeagent_lookup`（**モデルを呼ばない**＝幻覚が入らない）。本文を読ませたいなら `freeagent_grounded`。",
        "- 検索語は**英語の語**にする。日本語の質問のまま arXiv / Crossref / Internet Archive を引くと 0 件になりやすい",
        "  （`grounded` は `search_query` に分けて渡せる）。",
        f"- 既定のソースは {len(defaults)} 種: " + ", ".join(defaults) + "。追加は `sources` に明示する。",
        f"- 取得の締め切りは既定 {S.KB_DEADLINE:g} 秒（`FREEAGENT_KB_DEADLINE`）。間に合わないソースは `timed_out` として",
        "  返り、裏の取得は続いて次回はキャッシュから返る。",
        "",
        "## 引用の読み方",
        "",
        "- 引用は**トップレベルの `citations`** に統合済み（`results[source]` に重ねない）。",
        "- `metadata_only: true` は本文根拠が無い＝**根拠として数えない**（目録・登録者の自己申告）。",
        "- 「該当なし」は失敗ではない（そのソースに無い）。**別のソースか別の語で 1 回だけ**言い換える。",
        "- 番号付きで注入された根拠は `[n]` で引く。根拠に無い番号は `unsupported_citations` に出る。",
    ])


# 資源の目録。**並びは固定**（辞書の挿入順）で、名前は URI の末尾と一致させる。
_RESOURCE_BUILDERS = {
    "api-rules": ("ツールの使い分けと待ち時間の契約", _resource_api_rules),
    "sources": ("知識ソースの一覧（既定/明示）", _resource_sources),
    "providers": ("推論プロバイダと必要な設定（値は出さない）", _resource_providers),
    "result-shape": ("content / structuredContent の契約と引用の位置", _resource_result_shape),
    "skills": ("スキルの目録（必要なときだけ読む手順書）", _resource_skills),
    "skills/plan": ("計画の型（plan の粒度・改訂・仮説）", _resource_skill_plan),
    "skills/review": ("査読の型（4 行・重要度・5 軸・2 段）", _resource_skill_review),
    "skills/debug": ("デバッグの型（症状→再現→仮説→証拠→修正）", _resource_skill_debug),
    "skills/grounded": ("出典つきで答える型（検索語・ソース・引用の読み方）", _resource_skill_grounded),
}


def resource_list() -> list[dict]:
    """`resources/list` の応答（MCP の Resource 形）。"""
    return [{"uri": RESOURCE_SCHEME + name, "name": name, "title": title,
             "description": title, "mimeType": RESOURCE_MIME}
            for name, (title, _builder) in _RESOURCE_BUILDERS.items()]


# --- 引数の補完（MCP の completion/complete）--------------------------------
#
# 仕様（2025-06-18 §Completion）で補完の対象になるのは **`ref/prompt` と `ref/resource` だけ**で、
# ツール引数の補完は規格に無い（2026-11-02 に仕様を確認）。そこで**リソース URI の補完**を実装する
# （`resource://freeagent/sk…` と打つと候補が出る）。プロンプトは公開していないので空を返す
# （「知らない名前を捏造しない」）。

COMPLETION_MAX = 100


def completion_complete(params: dict) -> dict:
    """`completion/complete` の応答（values / total / hasMore）。"""
    params = params if isinstance(params, dict) else {}
    ref = params.get("ref") if isinstance(params.get("ref"), dict) else {}
    argument = params.get("argument") if isinstance(params.get("argument"), dict) else {}
    value = S.as_str(argument.get("value"))
    ref_type = S.as_str(ref.get("type"))
    candidates: list[str] = []
    if ref_type == "ref/resource":
        uris = [row["uri"] for row in S.resource_list()]
        template = S.as_str(ref.get("uri"))
        if "{" in template and "}" in template:
            head = template.split("{", 1)[0]
            tail = template.split("}", 1)[1]
            prefix = head + value
            candidates = [u for u in uris
                          if u.startswith(prefix) and (not tail or u.endswith(tail))]
        else:
            prefix = value or template
            candidates = [u for u in uris if u.startswith(prefix)] if prefix else list(uris)
    total = len(candidates)
    values = candidates[:COMPLETION_MAX]
    return {"completion": {"values": values, "total": total, "hasMore": total > len(values)}}


def resource_read(uri) -> str | None:
    """`resources/read` の本文。未知の URI は None（呼び出し側が -32002 を返す）。"""
    text = S.as_str(uri)
    if not text.startswith(RESOURCE_SCHEME):
        return None
    builder = _RESOURCE_BUILDERS.get(text[len(RESOURCE_SCHEME):])
    if builder is None:
        return None
    try:
        return builder[1]()
    except Exception as exc:   # 例外は外へ漏らさない（規約 1）。読めなかったことを本文で示す。
        return f"# 生成に失敗しました\n\n{type(exc).__name__}: {exc}\n"


EXPORTS = {name: globals()[name] for name in (
    'COMPLETION_MAX',
    'RESOURCE_MIME',
    'RESOURCE_SCHEME',
    '_RESOURCE_BUILDERS',
    '_md_table',
    '_resource_api_rules',
    '_resource_providers',
    '_resource_result_shape',
    '_resource_skill_debug',
    '_resource_skill_grounded',
    '_resource_skill_plan',
    '_resource_skill_review',
    '_resource_skills',
    '_resource_sources',
    'completion_complete',
    'resource_list',
    'resource_read',
)}
