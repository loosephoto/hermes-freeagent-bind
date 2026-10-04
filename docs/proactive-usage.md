# 率先して使わせる／OFF でも壊れない

このサーバーは「置けば使われる」ものではない。**使われるように設定する**必要があり、**無効化したときに
メイン LLM が壊れない**ようにも作ってある。以下は旧実装（`hermes-memex` / `polylogue-mcp`）で
実測した知見を、この実装向けに組み直したもの。数値はその実測値。

---

## 1. 何が効いて、何が効かないか（実測）

| レバー | 効果 | 理由 |
|---|---|---|
| **MCP `instructions`**（`initialize` 応答） | **効かない（Hermes）** | Hermes は読まない。現行版のソース `tools/mcp_tool_*.py` にも参照が無い（確認済み）。他クライアント（Claude Desktop 等）は読むので返している |
| `description` の工夫（【使う条件】【差分】【競合より優先】） | **1/2 で頭打ち** | モデルが見る唯一の窓口だが、読まれなければ意味が無い。競合の実名を書いても覆らない |
| **毎ターン注入される判断規則**（memory / `SOUL.md`） | **効く** | ツール一覧と一緒に毎ターン提示される唯一の場所。ここに「いつ使うか」を書く |
| **競合サーバーの汎用面を外す**（`tools.exclude`） | **効く** | 用途の近いサーバーが併存すると「先に見つけた方」が勝つ。名前を明記しても負ける |

> なお**採用率 2/2 は、除外設定が空振りしていた状態（memory の判断規則だけ）で記録した値**。
> 除外が実名に一致していれば、効果はこれより悪くならない。
| 上の 2 つの**併用** | **2/2** | 単独では 1/2 だった |

> 測定は**回答本文ではなく `state.db` の記録**で行う。遅延カタログ経由の呼び出しは `tool_call` という
> 名前で記録され、実名は `arguments.calls[].name` に入る（素朴に名前を数えると 0 件に見える）。

### 手順

```bash
python scripts/apply_proactive.py            # 何をするか確認（書き換えない）
python scripts/apply_proactive.py --apply    # 競合の汎用面を外す設定を適用
#  → memory へ判断規則を保存（エージェントに頼む）か、表示された文面を SOUL.md へ
#  → Hermes を再起動（MCP はホットリロードしない）
python scripts/measure_adoption.py --sessions 20
python scripts/measure_adoption.py --sessions 20 --min-needed-rate 0.5   # 必要場面の採用率でゲート
```

**最低 2 標本で測る。** 1/2 と 2/2 の差は標本 1 つでは判定できない。測定は**新しいプロセス**で
（`hermes chat -q "..."`）行う。実行中のセッションは起動時のツール一覧を保持しているので、
設定を変えても反映されない。

### 落とし穴

- **`AGENTS.md` は cwd 依存**（git root → cwd の連鎖でしか読まれない）。ホームに置いても全 cwd には効かない。
  全セッションに効かせたいなら memory か `SOUL.md`。
- **memory は毎ターン注入される**。長文を入れると全ターンのコンテキストを食う。判断規則は 2〜3 文に絞る。
- **競合サーバーを丸ごと無効化しない**。使えるツール（`deliberation` の `researcher` / `code-reviewer` /
  `debugger` など）まで失う。**汎用面だけ**を外す。
- **除外パターンは実ツール名に照合してから書く。** Hermes の照合は `fnmatchcase`（大小文字区別）で、
  `*` / `?` / `[` を含まない項目は**完全一致**。実測で 2 回踏んでいる:
  1. `ask_*`（アンダースコア）は**どの実名にも一致しない**（実名は `ask-all` / `consensus-step` … と
     ハイフン区切り）。流布している例をそのまま書くと空振りする。
  2. **`cache/mcp_schema_cache.json` は不完全**（実測: 18 件しか無く、実在する `panel` / `consensus` /
     `consensus-step` が載っていなかった）。キャッシュで照合すると「存在しない」と**誤判定**する。
- したがって照合は **`hermes mcp test <server>` のライブ一覧**で行う（deliberation の実測 21 件）。
  `python scripts/apply_proactive.py --check` が照合結果を出し、**設定済みの除外が一致 0 件なら exit 1**。
  正しい設定は `tools.exclude = ["ask-*", "panel", "consensus*"]`
  （汎用 9 件を外し、専門の `researcher` / `code-reviewer` / `debugger` / `architect` /
  `security-analyst` / `scope-analyst` / `plan-reviewer` / `analyze` / `session-*` は残す）。
- 依頼文にツール名を書いて測ると、**汎用面を外した効果**が消える（名前で選べてしまう）。

---

## 2. OFF・不通のときに副作用を残さない

MCP を `enabled false` にしても、バックエンド（プロキシ／各社 API）が全滅しても、**利用者のターンは続く**。
このとき「存在しないツールを探す空振り」「同じ失敗の再試行」「見当違いの統計の汚染」が起きると、
利用者から見た副作用になる。この実装は次の 4 点で潰している。

### (1) 不通では**状態を一切書かない**

接続不可（プロキシ停止・DNS 不達・TCP 拒否・タイムアウト）は **モデルの成績ではない**。統計に入れると
「プロキシが落ちていた 10 分」が全モデルの成績を下げ、**復旧後も選抜が歪む**。トレースにも意味のある
情報が無い（切り分けは `FREEAGENT_DEBUG_LOG` で足りる）。よって環境障害の経路では
**統計・トレース・クールダウンのどれも書かない**（no-op）。

検証（状態ディレクトリにファイルが 1 つも増えないことを機械的に確認する）:

```bash
env -u PYTHONPATH PYTHONPATH=src python scripts/check_offline.py
```

チェック内容: 全ツールが例外を漏らさない ／ 20 秒以内に返る（ハングしない） ／ `isError` の応答には
`next_action` がある ／ **状態ディレクトリが空のまま**。

### (2) 失敗のたびに**次の一手**を返す

`isError` の応答は `structuredContent.next_action` に、`kind` と `advice`（＋ `fallback_tools` /
`check` / `reenable`）を入れて返す。メイン LLM はこれを見て**代替へ落ちる**。主な `kind`:

| `kind` | 起きる状況 | 返す指示の要旨 |
|---|---|---|
| `unknown_tool` | ツール名が存在しない（無効化されている） | 掘り直さない。`hermes mcp list` で確認し、代替で続行 |
| `unavailable_backend` | プロキシ停止・キー未設定・Free 0 件 | **再試行しない**。`delegate_task` / `web_search` / `web_extract` で完遂 |
| `auth` | 401 / 署名付き 403 | キーを直す（直せば即復帰）。このプロバイダは自動選抜から外れる |
| `rate_limited` | 429 | クールダウン期限まで待つ／別モデルへ回す |
| `cooling` | 全候補がクールダウン中 | 待つか `models` を明示する |
| `empty_answer` | 空応答（思考トークンで予算消費） | `max_tokens` を増やして**1 回だけ**再試行 |

これらは `content` ではなく `structuredContent` に置く。`content` は人間が読むチャネルで、指示文が
混ざると意味不明な文が表示される。

### (3) 無効でも壊れない判断規則を**手元に置く**

`scripts/apply_proactive.py` が出す文面には、**「無効・不通なら存在しないツールを探さず代替で完遂し、
実際に応答した独立ソースの件数を明記する」**という節を含めてある。旧実装では、この節が無いまま
サーバーを OFF にすると、**存在しないツールを掘り続けた**。逆に、この節がある状態で OFF にすると、
`delegate_task` / `deliberation` / `web_search` で回答まで完遂し、エラーは 0 件だった。
OFF でも多視点の手段は残る（速度と質が落ちるだけ）。
**「1 件しか取れていないのに複数視点で検討したと書く」**のを防ぐ一文も同じ場所に置いてある。

### (4) OFF の手順と、切り替え時の注意

```bash
hermes config set mcp_servers.freeagent-bind.enabled false   # 戻すなら true
#  → 必ず Hermes を再起動
hermes mcp list        # ✓ enabled / ✗ disabled を確認
```

- **MCP はホットリロードしない。** 切り替えは再起動を伴うので、セッションの途中では変わらない。
- 再起動したくない一時的な遮断は、キーを外す／`FREEAGENT_PROVIDER_ORDER` を到達不能な値にする方が安全
  （サーバー自体は起動したまま、推論だけ失敗し、上の (1)(2) が働く）。
- 状態ディレクトリ（`%LOCALAPPDATA%\hermes-freeagent-bind\`）は**OFF にしても削除不要**。
  クールダウン（15 分）と認証記憶（15 分）は期限で切れ、統計は次回起動時にそのまま使われる。