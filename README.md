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

## 推論バックエンド（4 プロバイダ）

| プロバイダ | 一覧の取得 | 推論に必要な資格情報 | 備考 |
|---|---|---|---|
| `nous` | ローカルプロキシ | 不要（`hermes proxy start` が必要） | Hermes のプロキシが返すモデル群 |
| `openrouter` | 未認証でも可 | `OPENROUTER_API_KEY` | 無料は `:free` / pricing が 0。**21 件中 11 件が実応答**（実測） |
| `nvidia` | 未認証でも可 | `NVIDIA_API_KEY` | 無料クレジット枠。**一覧 82 件中 55 件は 404=EOL**（実測） |
| `huggingface` | **未認証でも可** | `HF_TOKEN`（**Inference Providers の権限が必要**） | 料金・文脈長は**提供元ごと**（`providers[]`）。無料枠は**月次クレジット**（尽きると全モデルが 402） |

### 一覧は実態と乖離する — だから「検索 → 生存確認 → 利用」の 3 段で使う

各社の `/v1/models` は**呼べないモデルを含む**（NVIDIA は EOL が 55/82、HF は権限不足で全滅、
OpenRouter の `:free` にも提供元都合の 403 がある）。`freeagent_models` はこの 3 段を 1 つの道具で回す:

```jsonc
// 1. 検索: 語句・プロバイダ・無料限定で絞る
{"query": "nemotron", "free_only": true, "limit": 20}
// 2. 生存確認: 実際に 1 回呼び、404=廃止 / 403=権限なし を一覧から除外（429 や timeout は残す）
{"provider": "nvidia", "free_only": true, "all": true, "probe": true, "probe_limit": 25}
// 3. 得た ref をそのまま他ツールへ
{"prompt": "...", "models": ["nvidia/nvidia/nemotron-3-super-120b-a12b"]}
```

```bash
# 生存確認の結果を**永続ストア**へ定着させる（モデルは週単位で入れ替わるので定期実行）
env -u PYTHONPATH PYTHONPATH=src python scripts/warmup_models.py --page 25

# プロバイダごとに「鍵で実際に推論できるか」を 1 件ずつ確かめる（鍵の設定直後の切り分け用）
env -u PYTHONPATH PYTHONPATH=src python scripts/probe_providers.py
```

生存確認の結果は `cooldowns.json`（404/410 は 1 時間）/ `model_stats.json` / `provider_auth.json`
（プロバイダ単位で 15 分）に残り、以後の**自動選抜が生きているモデルだけを選ぶ**。

## API キー — 各キーの意味・入手先・未設定時の挙動

**置き場所は MCP クライアントの env**（`mcp_servers.freeagent-bind.env.<NAME>`）。サーバーはキーを
**保存もログ出力もしない**（蓄積ストア `FREEAGENT_STATE_DIR` にも残らない）。`.env` の自動読み込みは
しないので、`hermes config set` か OS の環境変数で渡すこと。

| キー | 対象 | 必須度 | 未設定時の挙動 | 入手先 |
|---|---|---|---|---|
| `OPENROUTER_API_KEY` | `openrouter` の推論 | 任意 | 一覧は取れる。表示が `[未設定 → 検索のみ]` になり推論候補から外れる | <https://openrouter.ai/settings/keys> |
| `NVIDIA_API_KEY` | `nvidia`（NIM）の推論 | 任意 | 同上（一覧は未認証で取れる） | <https://build.nvidia.com>（プロフィール → API Keys） |
| `HF_TOKEN` | `huggingface` の推論 | 任意 | 同上。**無認証の推論は 401** | <https://huggingface.co/settings/tokens> |
| `OPENALEX_API_KEY` | `openalex` の検索 | 任意（実質推奨） | 匿名検索が提供元側で停止され `503 Anonymous search is paused` / `429` になりうる（キーで日次予算 10 倍） | <https://openalex.org/settings/api> |
| `GITHUB_TOKEN` / `GH_TOKEN` | `github` の検索 | コード検索は**必須** | `kind="code"` は明示エラー。`repo`/`issue` は未認証枠 60 req/h で動く | <https://github.com/settings/tokens> |
| `FREEAGENT_MAILTO` | Crossref / OpenAlex の polite pool | 任意 | 動くが共有レート枠で不利 | 自分のメールアドレス（登録不要） |
| `FREEAGENT_API_KEY` | `nous`（ローカルプロキシ） | **不要** | プロキシが実資格情報を付与するため**形だけ**の値でよい | 不要（`hermes proxy start` が必要） |

**キーが無くても全体は止まらない**。`freeagent_models` は 4 プロバイダの一覧を常に集め、推論の可否だけを
キーの有無で分ける（`ready`）。キー未設定のプロバイダも「今どの Free モデルが存在するか」は見えるので、
「このキーを入れればこのモデルが使える」という誘導ができる。ただし**一覧の件数を使える数と思わない**こと
（下記の生存確認を参照）。

### 設定する

```bash
# 値は config.yaml（自分のマシン内）にだけ書かれる。チャットに貼らない・コミットしない。
hermes config set mcp_servers.freeagent-bind.env.OPENROUTER_API_KEY '<値>'
hermes config set mcp_servers.freeagent-bind.env.NVIDIA_API_KEY     '<値>'
hermes config set mcp_servers.freeagent-bind.env.HF_TOKEN           '<値>'
hermes config set mcp_servers.freeagent-bind.env.OPENALEX_API_KEY   '<値>'
hermes config set mcp_servers.freeagent-bind.env.GITHUB_TOKEN       '<値>'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_MAILTO   'you@example.com'

# 鍵で「実際に推論できるか」を 1 件ずつ確かめる（設定直後の切り分け）
env -u PYTHONPATH PYTHONPATH=src python scripts/probe_providers.py
```

**MCP はホットリロードしない**ので、キーを足したらクライアント（Hermes）を再起動する。設定が届いて
いれば `freeagent_models` の先頭行が `✓ openrouter … / ✓ nvidia …` に変わる。`hermes mcp add` は対話式で
TTY が無いと `Cancelled.` になり設定が書かれないため、**`hermes config set` で非対話に組む**のが確実。

### プロバイダごとの実測（キーの挙動）

**`OPENROUTER_API_KEY`** — `sk-or-...`。無料の `:free` SKU を使うだけでもキーが要る。実測: 458 モデル /
Free 21 件 / **実応答 11 件**。`:free` でも **403（モデル単位の提供元制限）** と **429（レート）** がある。
未認証で叩くと `401 No cookie auth credentials found`。残量は `GET /api/v1/key` で確認できる。

**`NVIDIA_API_KEY`** — `nvapi-...`。**一覧 82 件のうち 55 件は 404（アカウントで未有効）か 410（EOL）**で
呼べないので、`freeagent_models(probe=true)` か `scripts/warmup_models.py` で生存確認してから使う。
症状の読み分け: キー無し `401 authorization missing` / 不正キー `403 Authorization failed` /
未有効モデル `404` / 廃止 `410`。

**`HF_TOKEN`** — `hf_...`。別名 `HUGGINGFACE_API_KEY` / `HUGGINGFACEHUB_API_TOKEN` も読む。
**fine-grained トークンでは「Make calls to Inference Providers」を有効にする**必要があり、読み取りだけの
トークンは全モデル 403 になる（実測: `does not have sufficient permissions to call Inference Providers`）。
キーが有効でも推論できるとは限らない。加えて **無料枠は月次クレジット**で、尽きると全モデルが
`402 You have depleted your monthly included credits` になる（サーバーは 402 を記憶せず別プロバイダへ回す）。
`:together` 経由は **Cloudflare Error 1010** を返すことがあるので、その場合は `model:提供元`
（例: `inclusionAI/Ling-3.0-flash-Fin:novita`）で別経路を試す。

**`OPENALEX_API_KEY`** — **無料**。キー無しでも基本利用はできるが、キーがあると日次予算が 10 倍になる
（提供元の記載では無料枠 100,000 credits/日・毎秒 100 リクエスト。超えると `429`）。<https://help.openalex.org/api/authentication/> / <https://api.openalex.org/rate-limit?api_key=…> で残量確認。
**未設定だと匿名検索が提供元側で停止されうる**（実測: `503 Anonymous search is paused` と `429 Rate limit exceeded (Anonymous ...)` の両方）。
単一 work の取得はキー無しでも通る。失敗は `results.openalex.error` に隔離され、他の 5 ソースは影響を
受けない。

**`GITHUB_TOKEN` / `GH_TOKEN`** — コード検索（`kind="code"`）は**トークン必須**で、無いとサーバーが
`コード検索は GITHUB_TOKEN（または GH_TOKEN）が必要です` と返す（黙って空を返さない）。`repo` / `issue` は
未認証でも動くが 60 req/h。`GH_TOKEN` は gh CLI と共用できる（`gh auth token`）。取得は classic なら
`public_repo`、fine-grained なら public リポジトリの読み取り権限で足りる。

**`FREEAGENT_MAILTO`** — キーではないが、Crossref と OpenAlex は連絡先を入れた UA / `mailto` を求める
（polite pool）。未設定でも動くが、混雑時に共有枠へ回される。

**`FREEAGENT_API_KEY` / `FREEAGENT_BASE_URL`** — `nous` プロバイダ（ローカルプロキシ）用。**キーは不要**
で、`FREEAGENT_API_KEY` の既定値は形だけのプレースホルダ（プロキシが実資格情報を付与する）。プロキシが
停止していると到達不可（`WinError 10061`）になり、表示は `⚠ nous … 到達不可` になる。`hermes proxy start`
で復帰する。

## 6 つの知識バックエンド

| ソース | 用途 | 認証 |
|---|---|---|
| `wikipedia` | 百科（言語指定可） | 不要 |
| `wikidata` | 構造化データ（QID・ラベル・説明） | 不要 |
| `arxiv` | プレプリント検索 | 不要（**3 秒間隔のスロットル内蔵**） |
| `crossref` | 出版論文のメタデータ・DOI | 不要（`FREEAGENT_MAILTO` 推奨） |
| `openalex` | 論文グラフ・被引用数 | 検索は `OPENALEX_API_KEY` を推奨（無いと匿名検索が止められうる） |
| `github` | リポジトリ / Issue / コード | `GITHUB_TOKEN` / `GH_TOKEN`（コード検索は必須） |

`freeagent_lookup` と `freeagent_grounded` は 6 ソースを**並列に**引いて、重複を除いた出典リスト
（`[1] タイトル URL`）を返す。**LLM を経由しないので幻覚が混入しない**。

## ツール

| ツール | 用途 |
|---|---|
| `freeagent_models` | **モデル検索**（`query` / `provider` / `free_only` / `offset`）＋**生存確認**（`probe`）＋ Free 残数・品質統計 |
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
| `FREEAGENT_PROVIDER_ORDER` | `nous,openrouter,nvidia,huggingface` | 一覧に出すプロバイダと優先順 |
| `FREEAGENT_AUTH_TTL` | 900 | 認証失敗を覚えて自動選抜から外す時間（秒） |
| `FREEAGENT_PROBE_TIMEOUT` | 25.0 | 生存確認 1 件の読み取りタイムアウト（秒） |
| `FREEAGENT_PROBE_WORKERS` | 8 | 生存確認の並列度（1〜8） |
| `FREEAGENT_SESSIONS` | 1 | 相談セッションを永続化する |
| `FREEAGENT_SESSION_TTL` | 3600 | 相談セッションの寿命（秒） |
| `FREEAGENT_ALLOW_AGENT` | 0 | `freeagent_delegate`（Hermes 本体の起動）を許可 |
| `FREEAGENT_ARXIV_INTERVAL` | 3.0 | arXiv の最小呼び出し間隔（秒） |
| `FREEAGENT_EMPTY_TOKEN_FLOOR` / `_CAP` | 512 / 2048 | 空応答時の予算引き上げ幅 |
| `FREEAGENT_USER_AGENT` | `hermes-freeagent-bind/0.1 (+…/hermes-freeagent-bind)` | 知識 API に名乗る UA（連絡先入りが望ましい） |
| `FREEAGENT_MAILTO` | 空 | Crossref / OpenAlex の polite pool 用メールアドレス |
| `FREEAGENT_CONNECT_TIMEOUT` / `FREEAGENT_READ_TIMEOUT` | 10.0 / 180.0 | 外部 HTTP の (connect, read) タイムアウト（秒） |
| `FREEAGENT_KB_TTL` / `FREEAGENT_KB_TIMEOUT` | 1800.0 / 20.0 | 知識取得のキャッシュ TTL・読み取りタイムアウト（秒） |
| `FREEAGENT_TRACE` / `FREEAGENT_STATS` / `FREEAGENT_COOLDOWN` | 1 | トレース・品質統計・クールダウンの記録 |
| `FREEAGENT_DEBUG_LOG` | 空 | 指定パスへ stdio の送受信を 1 行ずつ追記（クライアント互換の切り分け用） |
| `FREEAGENT_HERMES_BIN` | `hermes`（`which` で探索） | `freeagent_delegate` / サブエージェント起動に使う実行ファイル |

**キー（プロバイダの資格情報）**

| 変数 | 既定 | 意味 |
|---|---|---|
| `OPENROUTER_API_KEY` | 空 | OpenRouter の推論。無料 `:free` SKU でも必須（一覧は未認証でも取れる） |
| `NVIDIA_API_KEY` | 空 | NVIDIA NIM の推論（一覧は未認証でも取れる。生存確認が必須） |
| `HF_TOKEN`（別名 `HUGGINGFACE_API_KEY` / `HUGGINGFACEHUB_API_TOKEN`） | 空 | Hugging Face の推論。**Inference Providers 権限**が必要 |
| `OPENALEX_API_KEY` | 空 | OpenAlex の検索。無いと匿名検索が停止されうる |
| `GITHUB_TOKEN` / `GH_TOKEN` | 空 | GitHub のレート制限緩和（コード検索は必須） |
| `FREEAGENT_API_KEY` | `proxy-attaches-real-credentials` | `nous` プロキシ用のダミー（実資格情報はプロキシが付与） |

**接続先の上書き（通常は触らない）**

| 変数 | 既定 | 意味 |
|---|---|---|
| `FREEAGENT_BASE_URL` | `http://127.0.0.1:8645/v1` | `nous` の接続先 |
| `FREEAGENT_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter の接続先 |
| `FREEAGENT_NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | NVIDIA NIM の接続先 |
| `FREEAGENT_HF_BASE_URL` | `https://router.huggingface.co/v1` | HF Inference Providers の接続先 |

**蓄積ストアのパス上書き**（既定は `FREEAGENT_STATE_DIR` 配下。**ユーザーの予定・統計が消えない場所**に置く）

| 変数 | 既定のファイル名 |
|---|---|
| `FREEAGENT_COOLDOWN_PATH` | `cooldowns.json`（404/410 は 1 時間、429 は `Retry-After`） |
| `FREEAGENT_AUTH_PATH` | `provider_auth.json`（プロバイダ単位の認証失敗・`FREEAGENT_AUTH_TTL` 秒） |
| `FREEAGENT_STATS_PATH` | `model_stats.json`（品質統計） |
| `FREEAGENT_TRACE_PATH` | `traces.jsonl`（トレース） |
| `FREEAGENT_SESSIONS_PATH` | `consult_sessions.json`（相談セッション） |

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
- **モデル一覧を信じない**。実測で NVIDIA は 82 件中 55 件が 404（EOL）、HF は無料 3 件すべてが 403
  （トークン権限）、OpenRouter の `:free` にも提供元都合の 403 がある。`probe` で生存確認し、
  404/410（廃止）と 401/403（権限）だけを除外する。**429 と timeout は残す**（生きているが今は
  応えないだけのものを永久に隠さない）。
- **認証失敗はプロバイダ単位で覚え、自動選抜からだけ外す**（実測: HF の権限不足で 4 体選抜のうち 3 体が
  HF になり、失敗→代替で無駄が積み上がった）。明示指定は常に試すので、キーを直せば即復帰する。
- **選抜はプロバイダを巡回させる**。品質観測が無いモデルは同点になり、素の順序だとモデル ID の
  アルファベット順で 1 プロバイダが枠を独占する（`huggingface/…` が最初に来る）。パネルの意味は
  多様性なので、プロバイダ交互に取り、プロバイダ順は最良モデルの順位で決める（品質順は捨てない）。
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
- **Hugging Face はトークン権限が要る**: 一覧（検索）は未認証でも取れるが、推論は
  `Inference Providers` 権限を持つトークンが必要。権限が無いと全モデルが 403 になり
  （実測: fine-grained トークンで `does not have sufficient permissions to call Inference
  Providers`）、サーバーは理由と直し方を返して**そのプロバイダを自動選抜から外す**。
- **HF の無料枠は月次クレジット**で、尽きると全モデルが **402 `You have depleted your monthly included
  credits`** になる（実測: 生存確認とパネルを繰り返すと枯渇した）。402 はキー・権限の問題ではないので
  サーバーは**記憶せず、別プロバイダへ回す**（翌月に回復する）。枯渇中は他の 3 プロバイダを使うこと。
- **HF の 403 は 2 種類ある**（サーバーは署名で区別する）: 権限不足（トークンを直す）と
  **提供元/CDN の拒否**（実測: `prism-ml/…:together` が **Cloudflare Error 1010 "Access denied"**）。
  後者はトークンが正しくても起きるので、`model:提供元`（例: `:novita`）で別経路を試す。
  サーバーはこれを**除外せず「要再確認」として残す**。
- **NVIDIA の一覧は古い**: 82 件のうち 55 件が 404（EOL）。`probe` / `scripts/warmup_models.py` で
  生存確認してから使うこと。

## 検証

```bash
python -m compileall -q src/freeagent_bind   # 構文
python scripts/check_integrity.py            # レジストリ・スキーマ・版の整合
python -m unittest discover -s tests         # オフライン回帰テスト
python scripts/smoke_stdio.py                # 実クライアント経路（stdio）
FREEAGENT_PROBE_NET=1 python scripts/smoke_stdio.py   # バックエンド生存も確認
env -u PYTHONPATH PYTHONPATH=src python scripts/probe_providers.py          # 鍵で実推論できるか
env -u PYTHONPATH PYTHONPATH=src python scripts/warmup_models.py --page 25  # 生存確認を永続ストアへ
```

## ライセンス

MIT。データは各提供元（arXiv / Crossref / OpenAlex / Wikimedia / GitHub）の条件に従うこと。