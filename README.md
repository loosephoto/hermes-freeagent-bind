# hermes-freeagent-bind

Hermes Agent の **Free モデルをサブ LLM として並列に走らせる**ための MCP サーバー。メイン LLM の
知識補助として **arXiv / Crossref / OpenAlex / Wikipedia / Wikidata / GitHub** を引き、出典つきで
回答を組み立てる。

- **実行時依存ゼロ**（Python 3.11+ の標準ライブラリのみ・`pip install` 不要）
- **stdout に UTF-8 の改行区切り JSON-RPC 2.0** を自分で書く（クライアント非依存）
- **単一ファイルのモノリス**（`src/freeagent_bind/server.py`）— 肥大化を前提に §区画で増築する

## 出自

旧 `hermes-memex` の**設計と実測知見を継承**しつつ、**実装は新規に書き直した**もの。名称の重複により
旧リポジトリは削除となったため、名前空間（`freeagent_*` / `FREEAGENT_*`）と識別子をすべて新しくした。
コードの丸写しはしていない（コピーではなく、旧実装で実測して裏づけの取れた規約だけを持ち込んでいる）。

## 6 つの知識バックエンド

| ソース | 用途 | 認証 |
|---|---|---|
| `wikipedia` | 百科（言語指定可） | 不要 |
| `wikidata` | 構造化データ（QID・ラベル・説明） | 不要 |
| `arxiv` | プレプリント検索 | 不要（**3 秒間隔のスロットル内蔵**） |
| `crossref` | 出版論文のメタデータ・DOI | 不要（`KB_MAILTO` 推奨） |
| `openalex` | 論文グラフ・被引用数 | 検索は `OPENALEX_API_KEY` が必要（後述） |
| `github` | リポジトリ / Issue / コード | `GITHUB_TOKEN` 推奨（コード検索は必須） |

`freeagent_lookup` と `freeagent_grounded` は 6 ソースを**並列に**引いて、重複を除いた出典リスト
（`[1] タイトル URL`）を返す。**LLM を経由しないので幻覚が混入しない**。

## ツール

| ツール | 用途 |
|---|---|
| `freeagent_models` | 利用可能なモデル・Free 残数・品質統計・クールダウン |
| `freeagent_ask` | 1 モデルへ 1 回（下読み・分類・下書き） |
| `freeagent_fanout` | プロンプト × モデルを並列実行（相互検証・ベストオブN） |
| `freeagent_panel` | 同じ問いを複数モデルへ。合意度・不一致・確信度を構造化 |
| `freeagent_lookup` | 外部知識を出典つきで取得（LLM 不使用） |
| `freeagent_grounded` | 根拠を注入してから複数モデルに回答させる（引用番号つき） |
| `freeagent_map` | 多数要素へ同一指示を並列適用し、必要なら reduce で統合 |
| `freeagent_consult` | メイン↔サブの双方向相談。`debate_depth="deep"` で 3 段討論 |
| `freeagent_agent` | サブ LLM が自分で知識ツールを呼ぶ調査ループ（読み取り専用） |
| `freeagent_delegate` | Hermes 本体を独立プロセスで起動（**既定では無効**・opt-in） |

## セットアップ

```bash
# 1. 推論バックエンド（Hermes のプロキシ）を起動しておく
hermes proxy start

# 2. 動作確認
python -m compileall -q src/freeagent_bind
python scripts/check_integrity.py
python -m unittest discover -s tests
python scripts/smoke_stdio.py
```

`pip install -e .` は不要（依存ゼロ）。インストールする場合のみ:

```bash
pip install -e .
hermes-freeagent-bind      # entry point
# または
python -m freeagent_bind
```

### Hermes への登録

```bash
hermes config set mcp_servers.freeagent-bind.command <python 実行ファイル>
hermes config set mcp_servers.freeagent-bind.args '["<ABS_PATH>/src/freeagent_bind/server.py"]'
hermes config set mcp_servers.freeagent-bind.connect_timeout 45
hermes config set mcp_servers.freeagent-bind.enabled true
```

`hermes mcp add` は対話式で、TTY が無いと `Cancelled.` になり設定が書かれない。**`hermes config set`
で非対話に組む**のが確実。環境変数を渡す場合は `mcp_servers.freeagent-bind.env.<NAME>` を使う。

## 環境変数

| 変数 | 既定 | 意味 |
|---|---|---|
| `FREEAGENT_STATE_DIR` | `%LOCALAPPDATA%\hermes-freeagent-bind` | 蓄積ストア（統計・クールダウン・相談セッション）。**一時領域に置かない** |
| `FREEAGENT_DEFAULT_MODEL` | 空（自動選抜） | 既定モデル（`provider/model`） |
| `FREEAGENT_MAX_WORKERS` | 4 | 並列度（1〜16） |
| `FREEAGENT_MAX_CALLS_PER_RUN` | 40 | 1 呼び出しで許す最大推論回数 |
| `FREEAGENT_RANK` | 1 | 品質統計による並べ替えを使う |
| `FREEAGENT_SESSIONS` | 1 | 相談セッションを永続化する |
| `FREEAGENT_SESSION_TTL` | 3600 | 相談セッションの寿命（秒） |
| `FREEAGENT_ALLOW_AGENT` | 0 | `freeagent_delegate`（Hermes 本体の起動）を許可 |
| `FREEAGENT_ARXIV_INTERVAL` | 3.0 | arXiv の最小呼び出し間隔（秒） |
| `FREEAGENT_EMPTY_TOKEN_FLOOR` / `_CAP` | 512 / 2048 | 空応答時の予算引き上げ幅 |
| `OPENALEX_API_KEY` | 空 | OpenAlex の検索に必要（後述） |
| `KB_MAILTO` | 空 | Crossref/OpenAlex の polite pool 用メールアドレス |
| `GITHUB_TOKEN` / `GH_TOKEN` | 空 | GitHub のレート制限緩和・コード検索 |

## 設計判断（実測に基づく）

- **実行時依存ゼロ**。遅延 import するネイティブ拡張は、stdio 起動後に import すると**ツールが
  無応答になる環境がある**ため、必要なものは起動前に import する。
- **protocolVersion はクライアント提示値をそのまま返す**。固定すると新しめのクライアントが
  `tools/list` を取り消し、「60 秒タイムアウト」に見える。
- **stdout へは必ず UTF-8 バイト列**で書く。日本語 Windows では cp932 に落ちて応答が黙って捨てられる。
- **どのツールも例外を外へ漏らさない**。失敗は `structuredContent.error` で返す。
- **空応答を成功として返さない**。思考トークンで予算を使い切るモデルがあり（実測: `max_tokens=220`
  で 3 体中 2 体が空）、空を回答として渡すとメイン LLM が無回答を回答と誤解する。予算を上げて
  1 回だけ再試行し、それでも空なら明示的なエラーにする。
- **クールダウン中を選択段階で後回し**にする。除外ではなく後回し（空きが足りなければ補充）。選択直後に
  429 が記録されると「全候補がクールダウン中」で 1 体へ縮退し、失敗に見える。
- **合意度は表層の一致であって正しさの確率ではない**。返り値にもその旨を明記する。
- **決定はメイン LLM が行う**。サブの出力は仮説・根拠として返す。
- **蓄積するのは作業状態だけ**（品質統計・クールダウン・進行中の相談）。知識は蓄積しない。
- **arXiv は 3 秒間隔で直列化**する（連続アクセスで CDN が 406 を返す実測による）。

## 既知の制約

- **arXiv の HTTP 406**: 同一リクエストでも Python クライアントに確率的に 406 を返す（curl では
  常に 200）。レート・問いの内容には依存しない。スロットル + 最大 3 回の再試行で緩和しているが、
  落ちることはある（その場合 `results.arxiv.error` に出る）。
- **OpenAlex の匿名検索**: 提供元側で匿名検索が制限されており、実測では `503 Anonymous search is
  paused` と `429 Rate limit exceeded (Anonymous ...)` の両方が返る。`OPENALEX_API_KEY` を設定するまで
  検索系は失敗する（単一 work の取得はキー無しでも通る）。他 5 ソースは影響を受けず、失敗は
  `results.openalex.error` に隔離される。
- **`freeagent_delegate` は既定で無効**（起動コストが高く、独立した Hermes プロセスを立てるため）。

## 検証

```bash
python -m compileall -q src/freeagent_bind   # 構文
python scripts/check_integrity.py            # レジストリ・スキーマ・版の整合
python -m unittest discover -s tests         # オフライン回帰テスト
python scripts/smoke_stdio.py                # 実クライアント経路（stdio）
FREEAGENT_PROBE_NET=1 python scripts/smoke_stdio.py   # バックエンド生存も確認
```

## ライセンス

MIT。データは各提供元（arXiv / Crossref / OpenAlex / Wikimedia / GitHub）の条件に従うこと。