# hermes-freeagent-bind

**Hermes Agent の Free モデルを「サブ LLM」として並列に走らせ、外部知識で根拠づける MCP サーバー。**

メインの LLM が判断するための材料 —— **複数モデルの意見と一致・不一致**、**出典つきの知識**、**大量要素の
並列処理結果** —— を、無料枠だけで組み立てます。ツールは 10 個（`freeagent_*`）です。

- **実行時依存ゼロ**（Python 3.11+ の標準ライブラリのみ。`pip install` 不要）
- **モデルは 4 プロバイダ横断**（Nous / OpenRouter / NVIDIA NIM / Hugging Face）で、生きている Free を自動選抜
- **知識は 6 ソース**（arXiv / Crossref / OpenAlex / Wikipedia / Wikidata / GitHub）を LLM を介さず取得

**向いている用途**

- 判断が割れる問いを**複数モデルに当てて、一致点と対立点を洗い出す**（設計レビュー、リスク抽出、要約の検証）
- 大量の下読み・分類・下書きを**並列に流す**（メイン LLM が 1 件ずつ考えるより速く安い）
- **出典つきの事実**が欲しい（ハルシネーションを混ぜたくない調査）
- 100 件のタイトルへ同じ指示を一括適用して統合する

**向いていない用途**

- サブの出力をそのまま最終回答にすること。**合意度は「表層の一致」であって正しさの確率ではありません**
  ので、決定はメイン LLM が行う設計です
- 長時間のエージェント実行や、課金モデル前提の高品質推論（**Free 枠のみ**を扱います）

---

## 期待しないこと（実測に基づく）

**この MCP を入れても「超高性能」にはなりません。** メイン LLM を置き換えるものでも、賢くするものでも
ありません。期待されやすいことと、実測で分かっていることを先に書きます。

| 期待されやすいこと | 実測 | 実際に起きること |
|---|---|---|
| 頭が劇的に良くなる | **ならない** | 参加するのは **Free 枠の小型モデル**。返るのは「提案＋根拠」で、**メインより劣る答えも普通に返る**（だから少数意見・未解決点・合意度を残す設計にしています） |
| 並列で超高速になる | **ならない** | 速くなるのは**メインが待たずに走らせられる部分だけ**。1 体が数秒〜数十秒かかるので、往復する `freeagent_consult` は**分単位**です（実測: 4 体の `freeagent_panel` は 14〜20 秒、多段討議を含む 1 ターンは **3分52秒**、別の標本は 420 秒を超えても継続）。多くの場合、総時間はむしろ延びます |
| 多数決を取れば正解が保証される | **保証されない** | 同系統の Free モデルは**同じ誤りを共有**しやすい。**投げ先も減ります**: 実測で `nous` はプロキシ停止で 0 モデル（到達不可）、HF の無料 3 件はクレジット枯渇で 402、NVIDIA は 82 件中 55 件が 404（廃止）。応答が返っても中身は玉石混交で、実測の 4 体には **6.7B のコーダーモデルが API 設計の問いに答えていました**。だから「独立意見は N 体・うち応答 M 体」と留保を付けて返します |
| 合意度が高ければ正しい | **別物** | 合意度は**表層の一致**（言い回しの類似）であって、正しさの確率ではありません。**確信度の自己申告は当てになりません**（実測: 確信度 `confidence_mean` **90.5** に対し合意度 `agreement` **0.094** の回がありました＝自信満々でも言っていることがバラバラ） |
| 出典が付くので幻覚が消える | **消えない** | `freeagent_lookup` / `freeagent_grounded` は LLM を介さず事実を取りますが、**解釈・推論は各モデルの出力**です。保証されるのは「取得した事実」までです |
| 常時 ON にすると他のツールが遅くなる | **ならない** | 固定費は起動と一覧で**約 0.17 秒**（実測: spawn → `tools/list` 完了まで **170 ms**）、スキーマは **10 ツールで 9,451 文字**。実タスクの差はノイズ程度です |
| OFF にすれば速くなる | **ならない** | 浮くのは上の分だけ。相談が要る場面では代替手段（`delegate_task` / `deliberation` を N 回）の方が遅いことがあります |

**では何に向くのか**: 「メイン 1 体では見落とす観点を、独立した数体に当てて洗い出す」「出典つきの事実を
LLM を介さず取る」「大量の要素へ同じ指示を並列に流す」です。質の上限は **「メイン＋独立した数体」まで**で、
決定と責任はメイン LLM（＝あなた）に残ります。

---

## 目次

1. [期待しないこと（実測に基づく）](#期待しないこと実測に基づく)
2. [30 秒でわかる使い方](#30-秒でわかる使い方)
3. [どのツールを使うか](#どのツールを使うか)
4. [出力の読み方](#出力の読み方)
5. [率先して使わせる（と、OFF でも壊れない）](#率先して使わせるoff-でも壊れない)
6. [API キー](#api-キー)
7. [推論バックエンド](#推論バックエンド)
8. [知識バックエンド](#知識バックエンド)
9. [つまずいたとき](#つまずいたとき)
10. [環境変数](#環境変数)
11. [既知の制約](#既知の制約) ・ [設計判断](#設計判断) ・ [検証](#検証) ・ [出自](#出自)

---

## 30 秒でわかる使い方

**必要なもの**: Python 3.11+ と Hermes Agent。このサーバー自体の追加インストールは不要です（依存ゼロ）。

### 1. 登録する（4 コマンド）

```bash
hermes config set mcp_servers.freeagent-bind.command <python の絶対パス>
hermes config set mcp_servers.freeagent-bind.args '["<ABS_PATH>/src/freeagent_bind/server.py"]'
hermes config set mcp_servers.freeagent-bind.connect_timeout 45
hermes config set mcp_servers.freeagent-bind.enabled true
```

`<python の絶対パス>` は `python -c "import sys; print(sys.executable)"` で分かります。
`hermes mcp add` は**対話式**で、TTY が無い環境では `Cancelled.` になり設定が書かれません。
**`hermes config set` で非対話に組む**のが確実です。

### 2. キーを入れる（任意 — 入れなくても動きます）

```bash
# Free モデルで推論したいプロバイダのキーを、必要なぶんだけ
hermes config set mcp_servers.freeagent-bind.env.OPENROUTER_API_KEY '<値>'
hermes config set mcp_servers.freeagent-bind.env.NVIDIA_API_KEY     '<値>'
hermes config set mcp_servers.freeagent-bind.env.HF_TOKEN           '<値>'
# 知識検索を安定させたいとき
hermes config set mcp_servers.freeagent-bind.env.OPENALEX_API_KEY   '<値>'
hermes config set mcp_servers.freeagent-bind.env.GITHUB_TOKEN       '<値>'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_MAILTO   'you@example.com'
```

**キーが 1 つも無くてもサーバーは使えます**（モデル一覧の検索と知識検索は動きます）。入手先と挙動の詳細は
[API キー](#api-キー)を参照してください。

### 3. Hermes を再起動して確認

```bash
hermes proxy start              # nous プロバイダ（ローカルプロキシ）を使うなら
hermes mcp test freeagent-bind  # → Connected / 10 tools なら成功
```

**MCP はホットリロードしません。** 設定やキーを変えたら Hermes を再起動してください。

### 4. メイン LLM に頼む（プロンプト例）

```
freeagent_panel で size=4 にして、この設計案のリスクを挙げて。
一致した指摘と、モデルごとに割れた指摘を分けて出して。
```

```
freeagent_lookup で「宇宙エレベータの材料研究」を arXiv と Crossref から調べて、
出典番号つきで 5 件まとめて。本文は要約せず、根拠として引用して。
```

```
freeagent_map で、以下のタイトル 30 件を 1 件ずつ 1 行に要約して。
最後に reduce で全体の傾向を 3 行にまとめて。
```

---

## どのツールを使うか

目的から選んでください。どのツールも `size` を省略すると、Free の中から**生きているモデルだけを
プロバイダ巡回で自動選抜**します。

| やりたいこと | ツール | 主な引数 |
|---|---|---|
| **使えるモデルを探す／生きているか確かめる** | `freeagent_models` | `query` `provider` `free_only` `probe` `limit` `offset` |
| 1 モデルに 1 回だけ聞く（下読み・分類・下書き） | `freeagent_ask` | `prompt` `model` `system` `max_tokens` |
| **同じ問いを複数モデルへ**（合意・不一致の把握） | `freeagent_panel` | `question` `size` `models` `prefer` `exclude` |
| 複数プロンプト × 複数モデルを並列（ベストオブ N） | `freeagent_fanout` | `prompts` `models` `size` `system` |
| **出典を注入してから**複数モデルに答えさせる | `freeagent_grounded` | `question` `sources` `limit` `models` `size` |
| **出典つきの知識だけ**取る（LLM を経由しない） | `freeagent_lookup` | `query` `sources` `limit` `lang` `github_kind` |
| 多数の要素へ同じ指示 ＋ 必要なら統合 | `freeagent_map` | `items` `instruction` `model` `reduce` `reduce_model` |
| メイン ↔ サブの**往復相談**（`debate_depth="deep"` で 3 段討論） | `freeagent_consult` | `question` `session_id` `main_reply` `mode` `debate_depth` |
| サブが**自分で知識ツールを呼ぶ**調査ループ（読み取り専用） | `freeagent_agent` | `task` `models` `size` `max_steps` `main_reply` |
| Hermes 本体を別プロセスで起動（**既定では無効**・opt-in） | `freeagent_delegate` | `task` `timeout` |

**迷ったら**: 意見の食い違いを見たい → `freeagent_panel` / 事実が欲しい → `freeagent_lookup` /
件数が多い → `freeagent_map` / 1 回だけ聞きたい → `freeagent_ask`。

`freeagent_models` で得た ref（`provider/model` の形）は、そのまま他のツールの `models` に渡せます。
`model:提供元`（例 `inclusionAI/Ling-3.0-flash-Fin:novita`）の形で経路を固定することもできます。

---

## 出力の読み方

`freeagent_models` の先頭はプロバイダの状態です。

| 表示 | 意味 |
|---|---|
| `✓ openrouter 458 モデル / Free 21` | 資格情報があり、一覧も取れている |
| `— nvidia 82 モデル / Free 82 [NVIDIA_API_KEY 未設定 → 検索のみ]` | 一覧は取れるが**推論はできない**（キーを入れれば使える） |
| `⚠ nous 0 モデル … 到達不可: URLError …` | 一覧が取れていない。理由が出る（例: プロキシ停止） |

**Free 候補 N 件（うち今すぐ使用可 M）** の `M` が「キーがあり、生きている」数です。`N` をそのまま
「使える数」と読み替えないでください（[生存確認の 3 段](#生存確認の-3-段)）。

`freeagent_models(probe=true)` の生存確認は 5 分類で返ります。

| 判定 | 意味 | 一覧での扱い |
|---|---|---|
| `alive` | 実際に応答した | 使える |
| `slow` | timeout・空応答（コールドスタートなど） | **残す**（今は応えないだけ） |
| `gone` | `404` / `410`（未有効・廃止） | 除外 |
| `auth` | `401` / `403`（キー・権限） | 除外（プロバイダ単位で 15 分記憶） |
| `error` | `429`・`5xx`・`402`（クレジット枯渇）・CDN の 403 | **残す** |

**「要再確認」** と付いたモデルは `slow` / `error` です。消してはいません（生きているが今は応えない、
またはキーや枠の問題）。**クールダウン中**のモデルは除外ではなく後回しにされ、呼び出し結果の
`skipped_cooling` に現れます。

### 失敗したとき（`structuredContent.next_action`）

失敗した呼び出しには **`next_action`** が付きます。`kind` で状況が分かり、`advice` に次の一手、
`fallback_tools` に代替手段が入ります（`delegate_task` / `web_search` / `web_extract` など）。

| `kind` | 意味 | すること |
|---|---|---|
| `unknown_tool` | ツールが無い（無効化されている可能性） | 探し直さない。`hermes mcp list` で確認し代替で続行 |
| `unavailable_backend` | プロキシ停止・キー未設定・Free 0 件 | **再試行しない**（同じ失敗が返る）。代替へ回る |
| `auth` | キー・権限の問題 | キーを直す（直せば即復帰） |
| `rate_limited` | `429` | クールダウン期限まで待つ／別モデルへ回す |
| `cooling` / `empty_answer` | 全候補が休止中／空応答 | 待つ・`models` を明示・`max_tokens` を上げて 1 回だけ再試行 |

**不通のときは状態を一切書きません。** 接続不可・タイムアウトは「モデルの成績」ではないので、統計・
トレース・クールダウンのどれにも記録しません（プロキシが落ちていた数分でモデルの評価が下がり、
復旧後も選抜が歪むのを避けるため）。この契約は `python scripts/check_offline.py` が機械的に検証します。

---

## 率先して使わせる（OFF でも壊れない）

**置けば使われるものではありません。** 実測では `description` の工夫だけでは自発率が **1/2 で頭打ち**、
次を併用して **2/2** になりました。

| 手順 | なぜ |
|---|---|
| `python scripts/apply_proactive.py --apply` | 用途の近い競合（`deliberation` の `ask-*` / `panel` / `consensus*` = **9 件の汎用面だけ**）を外す。サーバーごと止めると専門ツール 12 件まで失う |
| 表示される文面を memory（または `SOUL.md`）へ入れる | **毎ターン注入される場所に判断規則を置く**のが唯一効くレバー。`AGENTS.md` は cwd 依存で全セッションには効かない |
| **Hermes を再起動** | MCP はホットリロードしない |
| `python scripts/apply_proactive.py --check` | 除外が**実ツール名に一致しているか**を照合する（空振りなら exit 1） |
| `python scripts/measure_adoption.py --sessions 20` | 効いたかどうかを `state.db` で測る。**最低 2 標本**（1/2 と 2/2 は標本 1 つでは区別できない） |

設定後の実測（依頼は「意見が割れている。独立した複数の視点から検討して」＝**ツール名を含まない**）:

| 標本 | 実際の呼び出し | 競合の使用 |
|---|---|---|
| 1 | `freeagent_consult`×2 / `freeagent_panel`×2 / `freeagent_models`×1 / `web_search`×1 | `deliberation.ask-*` **0 回** |
| 2 | `freeagent_panel`×3 / `freeagent_consult`×2 | `deliberation.ask-*` **0 回** |

> MCP の `instructions`（`initialize` 応答）は **Hermes では読まれません**（ソース確認済み）。他の
> クライアント向けに返しています。頼み方は[プロンプト例](#4-メイン-llm-に頼むプロンプト例)のように
> 用途で書くのが確実です（ツール名を書くと「名前で選ぶ」を測ってしまうので、測定時は書かない）。
>
> **除外パターンは写経しないでください。** Hermes の照合は `fnmatchcase`（大小文字区別）で、glob でなければ
> **完全一致**です。よく出回る `ask_*`（アンダースコア）は実名（`ask-all` / `consensus-step` … ハイフン区切り）
> に **1 件も一致せず、何も変えずに「設定した」気にさせます**。`--check` がこれを検出します。照合は
> **ライブの一覧**（`hermes mcp test deliberation` = 実測 21 件）で行います — `cache/mcp_schema_cache.json`
> は**不完全**（実測 18 件で、実在する `panel` / `consensus` / `consensus-step` が欠けている）ため、
> キャッシュだけで照合すると実在するツールを「存在しない」と誤判定します。

### OFF にしても壊れない

`hermes config set mcp_servers.freeagent-bind.enabled false`（＋再起動）で無効にできます。無効・不通でも、
**メイン LLM が代替で回答を完遂する**ように作ってあります。

- 失敗のたびに `structuredContent.next_action` で**次の一手**を返す（存在しないツールを掘り続けない）
- 不通では**状態を書かない**（統計・トレース・クールダウンに痕跡を残さない）
- `scripts/apply_proactive.py` が出す文面に「**無効なら代替で完遂し、実際に応答した独立ソースの件数を
  明記する**」を含めてある（1 件しか取れていないのに「複数視点で検討した」と書かないため）

**一時的に止めたいだけなら、`enabled false` よりキーを外す／バックエンドを届かない状態にする方が安全**です
（サーバーは起動したまま、推論だけが失敗し、上の `next_action` と「状態を書かない」が働きます。再起動も不要）。
蓄積ストアは `enabled false` にしても**削除不要**です（クールダウンと認証記憶は 15 分で切れます）。

詳しい手順・落とし穴・実測値は **[docs/proactive-usage.md](docs/proactive-usage.md)** にあります。

---

## API キー

各キーの意味・入手先・未設定時の挙動です。

**置き場所は MCP クライアントの env**（`mcp_servers.freeagent-bind.env.<NAME>`）。サーバーはキーを
**保存もログ出力もしません**（蓄積ストア `FREEAGENT_STATE_DIR` にも残りません）。`.env` の自動読み込みは
しないので、`hermes config set` か OS の環境変数で渡してください。

| キー | 対象 | 必須度 | 未設定時の挙動 | 入手先 |
|---|---|---|---|---|
| `OPENROUTER_API_KEY` | `openrouter` の推論 | 任意 | 一覧は取れる。表示が `[未設定 → 検索のみ]` になり推論候補から外れる | <https://openrouter.ai/settings/keys> |
| `NVIDIA_API_KEY` | `nvidia`（NIM）の推論 | 任意 | 同上（一覧は未認証で取れる） | <https://build.nvidia.com>（プロフィール → API Keys） |
| `HF_TOKEN` | `huggingface` の推論 | 任意 | 同上。**無認証の推論は 401** | <https://huggingface.co/settings/tokens> |
| `OPENALEX_API_KEY` | `openalex` の検索 | 任意（実質推奨） | 匿名検索が提供元側で停止され `503 Anonymous search is paused` / `429` になりうる（キーで日次予算 10 倍） | <https://openalex.org/settings/api> |
| `GITHUB_TOKEN` / `GH_TOKEN` | `github` の検索 | コード検索は**必須** | `kind="code"` は明示エラー。`repo`/`issue` は未認証枠 60 req/h で動く | <https://github.com/settings/tokens> |
| `FREEAGENT_MAILTO` | Crossref / OpenAlex の polite pool | 任意 | 動くが共有レート枠で不利 | 自分のメールアドレス（登録不要） |
| `FREEAGENT_API_KEY` | `nous`（ローカルプロキシ） | **不要** | プロキシが実資格情報を付与するため**形だけ**の値でよい | 不要（`hermes proxy start` が必要） |

**キーが無くても全体は止まりません。** `freeagent_models` は 4 プロバイダの一覧を常に集め、推論の可否だけを
キーの有無で分けます（`ready`）。キー未設定のプロバイダも「今どの Free モデルが存在するか」は見えるので、
「このキーを入れればこのモデルが使える」という誘導ができます。ただし**一覧の件数を使える数と思わない**
でください（[生存確認の 3 段](#生存確認の-3-段)）。

### 設定する

```bash
# 値は config.yaml（自分のマシン内）にだけ書かれます。チャットに貼らない・コミットしない。
hermes config set mcp_servers.freeagent-bind.env.OPENROUTER_API_KEY '<値>'
hermes config set mcp_servers.freeagent-bind.env.NVIDIA_API_KEY     '<値>'
hermes config set mcp_servers.freeagent-bind.env.HF_TOKEN           '<値>'
hermes config set mcp_servers.freeagent-bind.env.OPENALEX_API_KEY   '<値>'
hermes config set mcp_servers.freeagent-bind.env.GITHUB_TOKEN       '<値>'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_MAILTO   'you@example.com'

# 鍵で「実際に推論できるか」を 1 件ずつ確かめる（設定直後の切り分け）
env -u PYTHONPATH PYTHONPATH=src python scripts/probe_providers.py
```

**MCP はホットリロードしない**ので、キーを足したらクライアント（Hermes）を再起動してください。設定が
届いていれば `freeagent_models` の先頭行が `✓ openrouter … / ✓ nvidia …` に変わります。

### プロバイダごとの実測（キーの挙動）

**`OPENROUTER_API_KEY`** — `sk-or-...`。無料の `:free` SKU を使うだけでもキーが要ります。実測: 458 モデル /
Free 21 件 / **実応答 11 件**。`:free` でも **403（モデル単位の提供元制限）** と **429（レート）** があります。
未認証で叩くと `401 No cookie auth credentials found`。残量は `GET /api/v1/key` で確認できます。

**`NVIDIA_API_KEY`** — `nvapi-...`。**一覧 82 件のうち 55 件は 404（アカウントで未有効）か 410（EOL）**で
呼べないので、`freeagent_models(probe=true)` か `scripts/warmup_models.py` で生存確認してから使ってください。
症状の読み分け: キー無し `401 authorization missing` / 不正キー `403 Authorization failed` /
未有効モデル `404` / 廃止 `410`。

**`HF_TOKEN`** — `hf_...`。別名 `HUGGINGFACE_API_KEY` / `HUGGINGFACEHUB_API_TOKEN` も読みます。
**fine-grained トークンでは「Make calls to Inference Providers」を有効にする**必要があり、読み取りだけの
トークンは全モデル 403 になります（実測: `does not have sufficient permissions to call Inference Providers`）。
**キーが有効でも推論できるとは限りません。** 加えて **無料枠は月次クレジット**で、尽きると全モデルが
`402 You have depleted your monthly included credits` になります（サーバーは 402 を記憶せず別プロバイダへ
回します）。`:together` 経由は **Cloudflare Error 1010** を返すことがあるので、その場合は `model:提供元`
（例: `inclusionAI/Ling-3.0-flash-Fin:novita`）で別経路を試してください。

**`OPENALEX_API_KEY`** — **無料**。キー無しでも基本利用はできますが、キーがあると日次予算が 10 倍になります
（提供元の記載では無料枠 100,000 credits/日・毎秒 100 リクエスト。超えると `429`）。
詳細は <https://help.openalex.org/api/authentication/>、残量は
<https://api.openalex.org/rate-limit?api_key=…> で確認できます。
**未設定だと匿名検索が提供元側で停止されうる**（実測: `503 Anonymous search is paused` と
`429 Rate limit exceeded (Anonymous ...)` の両方）。単一 work の取得はキー無しでも通ります。失敗は
`results.openalex.error` に隔離され、他の 5 ソースは影響を受けません。

**`GITHUB_TOKEN` / `GH_TOKEN`** — コード検索（`kind="code"`）は**トークン必須**で、無いとサーバーが
`コード検索は GITHUB_TOKEN（または GH_TOKEN）が必要です` と返します（黙って空を返しません）。
`repo` / `issue` は未認証でも動きますが 60 req/h。`GH_TOKEN` は gh CLI と共用できます（`gh auth token`）。
取得は classic なら `public_repo`、fine-grained なら public リポジトリの読み取り権限で足ります。

**`FREEAGENT_MAILTO`** — キーではありませんが、Crossref と OpenAlex は連絡先を入れた UA / `mailto` を
求めます（polite pool）。未設定でも動きますが、混雑時に共有枠へ回されます。

**`FREEAGENT_API_KEY` / `FREEAGENT_BASE_URL`** — `nous` プロバイダ（ローカルプロキシ）用。**キーは不要**で、
`FREEAGENT_API_KEY` の既定値は形だけのプレースホルダ（プロキシが実資格情報を付与します）。プロキシが
停止していると到達不可（`WinError 10061`）になり、表示は `⚠ nous … 到達不可` になります。
`hermes proxy start` で復帰します。

---

## 推論バックエンド

4 プロバイダから Free モデルを集めます。

| プロバイダ | 一覧の取得 | 推論に必要な資格情報 | 備考 |
|---|---|---|---|
| `nous` | ローカルプロキシ | 不要（`hermes proxy start` が必要） | Hermes のプロキシが返すモデル群 |
| `openrouter` | 未認証でも可 | `OPENROUTER_API_KEY` | 無料は `:free` / pricing が 0。**21 件中 11 件が実応答**（実測） |
| `nvidia` | 未認証でも可 | `NVIDIA_API_KEY` | 無料クレジット枠。**一覧 82 件中 55 件は 404=EOL**（実測） |
| `huggingface` | **未認証でも可** | `HF_TOKEN`（**Inference Providers の権限が必要**） | 料金・文脈長は**提供元ごと**（`providers[]`）。**137 モデル / Free 3 / 生存 1**（実測）。無料枠は**月次クレジット**（尽きると全モデルが 402） |

### 生存確認の 3 段

一覧は実態と乖離します。各社の `/v1/models` は**呼べないモデルを含み**（NVIDIA は EOL が 55/82、HF は
権限不足で全滅した実測、OpenRouter の `:free` にも提供元都合の 403）、件数をそのまま使える数として
提示してはいけません。`freeagent_models` は次の 3 段を 1 つの道具で回します。

```jsonc
// 1. 検索: 語句・プロバイダ・無料限定で絞る
{"query": "nemotron", "free_only": true, "limit": 20}
// 2. 生存確認: 実際に 1 回呼び、404=廃止 / 401・403=キー・権限 を一覧から除外（429・timeout・402 は残す）
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

> `env -u PYTHONPATH` を前置しているのは、環境によって Hermes 側の `PYTHONPATH` が混ざって import が
> 壊れるためです（本 README のスクリプト実行例はすべてこの形にしています）。

生存確認の結果は `cooldowns.json`（404/410 は 1 時間）/ `model_stats.json`（品質統計）/ `provider_auth.json`
（プロバイダ単位で 15 分）に残り、以後の**自動選抜が生きているモデルだけを選びます**。

---

## 知識バックエンド

| ソース | 用途 | 認証 |
|---|---|---|
| `wikipedia` | 百科（言語指定可） | 不要 |
| `wikidata` | 構造化データ（QID・ラベル・説明） | 不要 |
| `arxiv` | プレプリント検索 | 不要（**3 秒間隔のスロットル内蔵**） |
| `crossref` | 出版論文のメタデータ・DOI | 不要（`FREEAGENT_MAILTO` 推奨） |
| `openalex` | 論文グラフ・被引用数 | 検索は `OPENALEX_API_KEY` を推奨（無いと匿名検索が止められうる） |
| `github` | リポジトリ / Issue / コード | `GITHUB_TOKEN` / `GH_TOKEN`（コード検索は必須） |

`freeagent_lookup` と `freeagent_grounded` は 6 ソースを**並列に**引いて、重複を除いた出典リスト
（`[1] タイトル URL`）を返します。**LLM を経由しないので幻覚が混入しません。** Wikipedia は検索結果と要約を 1 回の API 呼び出しで取得します。未知の `sources` だけが指定された場合は、誤って全ソースへ問い合わせず、有効なソース名を案内します。混在指定なら有効なソースだけを検索し、未知名も報告します。

---

## つまずいたとき

症状から引いてください。

| 症状 | 原因 | 対処 |
|---|---|---|
| `freeagent_*` ツールが出てこない | 未登録、または設定後に再起動していない | `hermes mcp test freeagent-bind` → 直したら**Hermes を再起動** |
| `Free候補 0 件` | キー未設定、または全部クールダウン中 | キーを設定 / `freeagent_models(probe=true)` で生存確認 / 少し待つ |
| `⚠ nous … 到達不可 WinError 10061` | ローカルプロキシが停止 | `hermes proxy start` |
| どの候補でも `401` で失敗 | キー未設定・無効 | 該当キーを設定（[API キー](#api-キー)） |
| HF が全モデル `403` | トークンに Inference Providers 権限が無い | fine-grained トークンで権限を有効化して差し替え |
| HF が全モデル `402` | 月次クレジット枯渇 | 別プロバイダへ fallback。該当モデルは短時間クールダウン後に再試行（翌月に回復） |
| HF の一部だけ `403`（`:together` 等） | 提供元/CDN のブロック（実測: Cloudflare 1010） | `model:提供元` で別経路（例 `:novita`） |
| NVIDIA が `404` / `410` | モデルが未有効 / 廃止（82 件中 55 件） | `probe` で生存確認し、生きているものだけ使う |
| OpenAlex だけ `503` / `429` | 匿名検索が提供元側で停止 | `OPENALEX_API_KEY` を設定（他の 5 ソースは影響なし） |
| コード検索だけ失敗 | トークンが無い（コード検索は必須） | `GITHUB_TOKEN` を設定、または `kind="repo"` / `"issue"` を使う |
| arXiv が時々 `406` | 既知の提供元挙動（curl では再現しない） | 再試行（`results.arxiv.error` に出る。他ソースは生きる） |
| クールダウンだらけで 1 体に縮退した | 429 が続けて記録された | キーを設定するか少し待つ（後回しにされるので補充される） |
| 応答が空 | 思考トークンで予算を使い切った | `max_tokens` を上げる（サーバーも 1 回だけ自動で再試行する） |
| `freeagent_*` を呼ぶが毎回失敗する | プロキシ停止・キー未設定（`next_action.kind` を見る） | 応答の `next_action.advice` に従う。`hermes proxy start` / キー設定 |
| 少し待っても選ばれない（自発しない） | 競合の汎用面が残っている・判断規則が毎ターンの場所に無い | `python scripts/apply_proactive.py --apply` → memory へ文面 → 再起動 → 測定 |
| 除外したはずの競合がまだ出てくる | パターンが実ツール名に一致していない（例: `ask_*` は実名 `ask-all` に一致しない） | `python scripts/apply_proactive.py --check`（空振りなら exit 1）で照合し、一致したパターンだけを設定 |
| 依頼した数より参加が少ない | 在庫に無い ref を指定した | content の `⚠️` に出る除外理由を確認（[出力の読み方](#出力の読み方)） |

---

## 環境変数

**通常は既定のままで使えます。** 変えるのは、各プロバイダのキーと `FREEAGENT_STATE_DIR`
（置き場を移したいとき）くらいです。

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

---

## 既知の制約

> 「どのくらい賢く／速くなるのか」の現実的な期待値は [期待しないこと（実測に基づく）](#期待しないこと実測に基づく) にまとめています。

- **arXiv の HTTP 406**: 同一リクエストでも Python クライアントに確率的に 406 を返す（curl では
  常に 200）。レート・問いの内容には依存しない。スロットル + 最大 3 回の再試行で緩和しているが、
  落ちることはある（その場合 `results.arxiv.error` に出る）。
- **OpenAlex の匿名検索**: 提供元側で匿名検索が制限されており、実測では `503 Anonymous search is
  paused` と `429 Rate limit exceeded (Anonymous ...)` の両方が返る。`OPENALEX_API_KEY` を設定するまで
  検索系は失敗する（単一 work の取得はキー無しでも通る）。他 5 ソースは影響を受けず、失敗は
  `results.openalex.error` に隔離される。
- **`freeagent_delegate` は既定で無効**（起動コストが高く、独立した Hermes プロセスを立てるため）。
  `FREEAGENT_ALLOW_AGENT=1` で許可する。
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

---

## 設計判断

実測に基づく実装判断です（開発者向け）。

- **実行時依存ゼロ**。遅延 import するネイティブ拡張は、stdio 起動後に import すると**ツールが
  無応答になる環境がある**ため、必要なものは起動前に import する。
- **protocolVersion はクライアント提示値をそのまま返す**。固定すると新しめのクライアントが
  `tools/list` を取り消し、「60 秒タイムアウト」に見える。
- **stdout へは必ず UTF-8 バイト列**で書く。日本語 Windows では cp932 に落ちて応答が黙って捨てられる。
- **どのツールも例外を外へ漏らさない**。handler だけでなく `render` / `error_advice` の失敗も `structuredContent.error` に変換する。
- **品質統計は観測ごとに永続化**し、成功・失敗カウンタを同じ半減期で減衰する（既定 14 日）。60 日超の古い履歴を prune し、並行保存では snapshot を複製してから原子的に置き換える。
- **知識検索キャッシュは同じ問い合わせを single-flight** でまとめ、エラー結果は保存しない。API 回復後の呼び出しは再取得できる。
- **接続・読み取りタイムアウトを分離**する（既定 10 / 180 秒）。接続不能や timeout では同じ不通先への fallback を続けない。
- **stdio は不正な JSON 値や batch でも停止しない**。JSON-RPC batch の応答は 1 行の response array として返す。
- 討論ラベルは句読点付き「なし」を空として扱い、同じ行の結論へ次ラベルを混ぜない。`deep` 討論は討論後の立場で合意度を算出し、初回回答の確認事項を保持する。
- セッション ID は短時間の並行呼び出しでも衝突しない乱数成分を含む。
- Wikipedia の言語コードはホスト名への埋め込み前に検証し、不正値では外部接続しない。
- クールダウン中の明示モデルが使えないときも代替候補を試す（認証失敗中のプロバイダは自動 fallback から除外）。HTTP 402（クレジット枯渇）はプロバイダ全体を停止せず、そのモデルだけを短時間クールダウンして別候補へ進む。
- `freeagent_panel` の回答数（`answered`）と独立した実モデル数（`independent_sources`）を分けて報告する。fallback によって同じ実モデルが複数枠を埋めても、合意度・確信度・コンセンサスでは 1 票として数える。
- `freeagent_map` は暗黙選択時に品質統計・クールダウンを考慮してモデルを選ぶ。`reduce` は文字列 `"false"` を真と誤認しない。
- **空応答を成功として返さない**。思考トークンで予算を使い切るモデルがあり（実測: `max_tokens=220`
  で 3 体中 2 体が空）、空を回答として渡すとメイン LLM が無回答を回答と誤解する。予算を上げて
  1 回だけ再試行し、それでも空なら明示的なエラーにする。
- **クールダウン中を選択段階で後回し**にする。除外ではなく後回し（空きが足りなければ補充）。選択直後に
  429 が記録されると「全候補がクールダウン中」で 1 体へ縮退し、失敗に見える。
- **合意度は表層の一致であって正しさの確率ではない**。返り値にもその旨を明記する。
- **決定はメイン LLM が行う**。サブの出力は仮説・根拠として返す。
- **モデル一覧を信じない**。実測で NVIDIA は 82 件中 55 件が 404（EOL）、HF は無料 3 件すべてが 403
  （トークン権限）、OpenRouter の `:free` にも提供元都合の 403 がある。`probe` で生存確認し、
  404/410（廃止）と 401/403（権限）だけを除外する。**429・timeout・402 は残す**（生きているが今は
  応えないだけのものを永久に隠さない）。
- **403 を「キー未設定」と決め打たない**。エラーの署名で判定する（`401` だけは無条件で認証）。提供元の
  都合による 403（モデル単位の制限）や CDN の 403 を認証失敗にすると、**プロバイダ全体を 15 分止めて
  生きているモデルまで選抜から消える**（実測）。
- **認証失敗はプロバイダ単位で覚え、自動選抜からだけ外す**（実測: HF の権限不足で 4 体選抜のうち 3 体が
  HF になり、失敗→代替で無駄が積み上がった）。明示指定は常に試すので、キーを直せば即復帰する。
- **選抜はプロバイダを巡回させる**。品質観測が無いモデルは同点になり、素の順序だとモデル ID の
  アルファベット順で 1 プロバイダが枠を独占する（`huggingface/…` が最初に来る）。パネルの意味は
  多様性なので、プロバイダ交互に取り、プロバイダ順は最良モデルの順位で決める（品質順は捨てない）。
- **依頼と参加の差を隠さない**。`モデル:提供元` の経路指定や除外理由（`notes`）を content にも出す
  （実測: 依頼 4 体が 3 体で走り、理由がどこにも出ていなかった）。
- **蓄積するのは作業状態だけ**（品質統計・クールダウン・進行中の相談）。知識は蓄積しない。
- **arXiv は 3 秒間隔で直列化**する（連続アクセスで CDN が 406 を返す実測による）。
- **不通（プロキシ停止・DNS 不達・タイムアウト）では状態を一切書かない**。環境障害は**モデルの成績では
  ない**ので、統計に入れると「落ちていた数分」が全モデルの評価を下げ、**復旧後も選抜が歪む**。トレースにも
  意味のある情報が無い（切り分けは `FREEAGENT_DEBUG_LOG`）。モデルの失敗（429 など）は従来どおり記録する。
- **失敗のたびに「次の一手」を `structuredContent.next_action` で返す**。無効・不通でも利用者のターンは
  続くので、ここで「再試行するな／代替はこれ」を返さないと、存在しないツールを掘り続けるか同じ失敗を
  繰り返してターンと時間を捨てる（旧実装の実測）。**`content` には書かない**（人間が読むチャネル）。
- **自発利用は記述の工夫だけでは足りない**。`description` に競合名と差分を書いても **1/2 で頭打ち**、
  「毎ターン注入される判断規則」＋「競合の汎用面の除外」の併用で **2/2**（実測）。MCP の `instructions` は
  **Hermes では読まれない**（ソース確認済み）ので当てにしない。測定は回答本文ではなく `state.db` の記録で行う。
- **除外パターンはライブの実ツール名に照合する**。`fnmatchcase` の照合で、流布している `ask_*` は実名
  （`ask-all` 等）に一致しない。`cache/mcp_schema_cache.json` は**不完全**（実測 18 件 < ライブ 21 件）で、
  実在するツールを「存在しない」と誤判定するため、照合は `hermes mcp test` を優先する。
- **原子置換の書きかけを掃除する**。`os.replace` の前に `<name>.<pid>.<tid>.tmp` の古い残骸を消す
  （実測: 途中で落ちた書きかけが残り、再起動のたびに増えた。60 秒より古いものだけを対象にする）。

---

## 検証

```bash
python -m compileall -q src/freeagent_bind   # 構文
python scripts/check_integrity.py            # レジストリ・スキーマ・版の整合
python -m unittest discover -s tests         # オフライン回帰テスト
python scripts/smoke_stdio.py                # 実クライアント経路（stdio）
python scripts/check_offline.py              # バックエンド全滅でも例外漏れ・ハング・状態汚染なし
python scripts/measure_adoption.py           # 自発利用率を state.db から測る
python scripts/apply_proactive.py            # 率先して使わせる設定（既定は表示のみ）
python scripts/apply_proactive.py --check    # 除外パターンが実ツール名に一致するか（空振りなら exit 1）
FREEAGENT_PROBE_NET=1 python scripts/smoke_stdio.py   # バックエンド生存も確認
env -u PYTHONPATH PYTHONPATH=src python scripts/probe_providers.py          # 鍵で実推論できるか
env -u PYTHONPATH PYTHONPATH=src python scripts/warmup_models.py --page 25  # 生存確認を永続ストアへ
```

`pip install -e .` は不要（依存ゼロ）。インストールする場合のみ:

```bash
pip install -e .
hermes-freeagent-bind      # entry point
# または
python -m freeagent_bind
```

---

## 出自

旧 `hermes-memex` の**設計と実測知見を継承**しつつ、**実装は新規に書き直した**もの。名称の重複により
旧リポジトリは削除となったため、名前空間（`freeagent_*` / `FREEAGENT_*`）と識別子をすべて新しくした。
コードの丸写しはしていない（コピーではなく、旧実装で実測して裏づけの取れた規約だけを持ち込んでいる）。

- **stdout に UTF-8 の改行区切り JSON-RPC 2.0** を自分で書く（クライアント非依存）
- **単一ファイルのモノリス**（`src/freeagent_bind/server.py`）— 肥大化を前提に §区画で増築する

## ライセンス

MIT。データは各提供元（arXiv / Crossref / OpenAlex / Wikimedia / GitHub）の条件に従うこと。
