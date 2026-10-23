# hermes-freeagent-bind 🧠

**Hermes Agent に「別の AI の意見」「出典つきの検索」「考えた手順の記録」を追加する MCP サーバーです。**

普段の会話を担当する AI（メイン LLM）が、必要なときだけ別のモデル（サブ LLM）へ相談したり、
論文・百科事典・GitHub を調べたりします。**最終的な判断はメイン LLM が行います。**

- Python 3.11 以上。サーバーの実行に追加ライブラリは不要です。
- 11 個のツール。推論は Nous / OpenRouter / NVIDIA NIM / Hugging Face / Groq / Cloudflare Workers AI / Gemini API / Vercel AI Gateway / Ollama Cloud / Z.ai（GLM）/ Mistral / ModelScope の 12 プロバイダ、検索は **24 ソース（既定6＋明示指定18）** に対応します。
- Free と判定されたモデル・無料クレジット枠を利用します。**無制限無料ではなく、提供元の利用枠・権限・契約条件が適用されます。**

**対象分野は科学（物理・数学・計算機）とプログラミングです。医学・生物学は対象外です。**

> **まず試すなら**：「出典つきで調べて」は知識検索、「この設計の弱点を別の AI にも聞いて」は相談です。
> 検索や思考ノートの記録は、推論バックエンドがなくても使えます。別の AI に答えさせるには、
> プロキシへの接続、または推論用 API キーが必要です。

## 🧭 この README の読み進め方

- **初めて使う**：[できること](#-できることできないこと) → [設定の4ステップ](#-はじめに設定する) → [会話の例](#-会話での頼み方)。
- **検索先を選びたい**：[知識バックエンド](#-知識バックエンド)。追加ソースは使いたいものだけ指定します。
- **動かない・回答が少ない**：[つまずいたとき](#-つまずいたとき) → [出力の読み方](#-出力の読み方)。
- **設定を調整したい**：[API キー](#-api-キーの設定)・[環境変数](#-環境変数)。詳細な引数や計測記録は折りたたんであります。
- **KANBAN のワーカーに使わせたい**：[KANBAN のワーカーから使うとき](#-kanban-のワーカーから使うとき)。プロファイルへの登録と並列時のプロセス数が要点です。

## 📑 目次

- [🎯 できること・できないこと](#-できることできないこと)
- [🚀 はじめに設定する](#-はじめに設定する)
- [🖧 1 プロセスを共有する（HTTP モード）](#5-複数クライアントで-1-プロセスを共有するhttp-モード任意)
- [💬 会話での頼み方](#-会話での頼み方)
- [🧰 どのツールを使うか](#-どのツールを使うか)
- [🧠 複雑な問題を段階的に考える（freeagent_think）](#-複雑な問題を段階的に考えるfreeagent_think)
- [📊 出力の読み方](#-出力の読み方)
- [🔧 つまずいたとき](#-つまずいたとき)
- [🚧 期待しないこと（実測に基づく）](#-期待しないこと実測に基づく)
- [🔑 API キーの設定](#-api-キーの設定)
- [📚 知識バックエンド](#-知識バックエンド)
- [🤖 自動的に使わせたいとき](#-自動的に使わせたいとき)
- [🤖 KANBAN のワーカーから使うとき](#-kanban-のワーカーから使うとき)
- [🛑 停止・再開する](#-停止再開する)
- [💾 データの保存と外部送信](#-データの保存と外部送信)
- [⚙ 環境変数](#-環境変数)
- [🧩 Hermes 以外で使うとき](#-hermes-以外で使うとき)
- [🧪 検証・開発者向け情報](#-検証開発者向け情報)

## 🎯 できること・できないこと

このサーバーは **MCP（AI に外部の道具を追加する仕組み）**で Hermes に接続します。
利用者が各ツールを直接操作するのではなく、会話を担当する AI が必要に応じて呼び出します。

| この README での呼び方 | 意味 |
|---|---|
| メイン LLM | あなたと会話し、ツールを選び、最終回答をまとめる AI |
| サブ LLM | メインから依頼を受けて答える別の AI モデル |
| 知識検索 | 論文・百科事典・コード登録簿などから出典を取得する機能。検索自体にサブ LLM は使いません |

| やりたいこと | できること | 注意点 |
|---|---|---|
| 設計案の見落としを探す | 同じ問いを複数のモデルに聞き、一致点・対立点を返す | 同じ誤りに賛成することもあります。多数決は正しさの保証ではありません |
| 出典を探す | 論文・百科・構造化データ・GitHub から URL と説明を取得する | 検索結果の関連性・情報の正確さは別途確認が必要です |
| 出典を読ませて回答させる | 取得した説明やアブストラクトをサブ LLM に渡す | 論文の全文を読む機能ではありません。引用番号の検査も内容の正しさは判定しません |
| 多数の文章を分類・要約する | 同じ指示を各要素へ並列適用し、必要なら統合する | `map` は 1 回最大 64 件。超過分は処理されず、先頭 64 件だけになるため、100 件なら分割してください。失敗した要素も確認してください |
| 長い検討の続きを忘れない | 計画・仮説・改訂・分岐を思考ノートへ記録する | ノートは期限つきです。AI の知能が増えるわけではありません |

重要な決定や安全に関わる判断で、サブ LLM の回答をそのまま採用する用途には向きません。
また、別のモデルへの相談は待ち時間を増やします。簡単な質問では使わない方が速いことがあります。

## 🚀 はじめに設定する

必要なものは **Python 3.11 以上・Hermes Agent・このリポジトリのファイル**です。
Hermes の準備がまだなら、先に[公式ドキュメント](https://hermes-agent.nousresearch.com/docs/)でインストールしてください。
以下は **Bash / Git Bash 用**のコマンドです。`<…>` は自分の環境の値に置き換えてください。

### 1. リポジトリを取得する

既に取得済みなら、この手順は不要です。

```bash
git clone https://github.com/loosephoto/hermes-freeagent-bind.git
cd hermes-freeagent-bind
python --version
python -c "import sys; from pathlib import Path; print(sys.executable); print(Path('src/freeagent_bind/server.py').resolve().as_posix())"
```

最後のコマンドで、Python とサーバーファイルの絶対パスが分かります。
`pip install -e .` は不要です。Windows の `args` では `C:/…` のように `/` を使うと、JSON の `\` エスケープを避けられます。

### 2. Hermes に登録する

```bash
hermes config set mcp_servers.freeagent-bind.command '<Python の絶対パス>'
hermes config set mcp_servers.freeagent-bind.args '["<サーバーファイルの絶対パス>"]'
hermes config set mcp_servers.freeagent-bind.connect_timeout 45
hermes config set mcp_servers.freeagent-bind.enabled true
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_HARNESS hermes
```

`FREEAGENT_HARNESS` は「Hermes から起動した」という目印です。なくても動きますが、判別不能のログが出ます。
`hermes mcp add` は対話式なので、対話できない環境では上の `config set` を使ってください。

### 3. 相談も使うなら、推論の接続を用意する

**知識検索と、サブを呼ばない思考ノートだけなら、この手順は不要です。** 相談も使うなら、次のどちらかを用意します。

- **Nous**：Hermes 側で Portal にログイン済みであることを確認し、別のターミナルで `hermes proxy start`。
  未ログインなら `hermes portal` でログインします。プロキシは動かしたままにします。
- **API キーを使う方法**：OpenRouter / NVIDIA / Hugging Face など、使いたい提供元のキーを設定します。
  全部のキーを用意する必要はありません（[入手先と設定方法](#-api-キーの設定)）。

**別の提供元を使うなら、Nous プロキシの起動は必須ではありません。**

### 4. 接続と実際の応答を確認する

```bash
hermes mcp test freeagent-bind
```

`Connected` と **11 tools** が出れば、サーバーの登録・接続は成功です。
**これだけではモデルが回答できることまでは確認していません。** Hermes を再起動してから、会話で次を試してください。

**まず検索を確認します（推論の接続は不要）。**

```text
freeagent_lookup で「大規模言語モデル」を wikipedia と wikidata から調べて。
```

**手順3で相談用の接続を用意した場合だけ、モデルの実応答も確認します。**

```text
freeagent_ask で「接続できました」と一言だけ答えさせて。
```

検索は成功するのに相談だけ失敗する場合は、プロキシ・キー・利用枠を確認します。
設定・キーの変更は実行中のサーバーには自動反映されません。**再起動が確実**です。
最近の Hermes には `/reload-mcp` もあります（[公式 MCP 設定リファレンス](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference#reloading-config)）。

### 5. 複数クライアントで 1 プロセスを共有する（HTTP モード・任意）

**既定は stdio です（1 クライアント = 1 プロセス）。何も指定しなければ従来どおりです。**

KANBAN のワーカーや複数の Bot から同時に使うと、stdio では**クライアントごとに別プロセス**が立ちます。
するとプロセス内に持っているものが分割されます: ホストごとのアクセス間隔、クールダウン、品質統計、
モデル一覧のキャッシュ（600 秒）、同時実行の上限。同じホストへ二重に投げたり、統計が上書きされたりします（**モデル一覧だけは v0.9.0 からディスクにも載ります**＝プロセスを入れ替えても温かく、起動直後の選抜が待たされません。SPEC §4.4）
（`AGENTS.md` 規約 28 が警告している状態そのものです）。HTTP モードでは **1 プロセスを複数クライアントが共有**します。

```bash
# サーバーを HTTP モードで起動する（動かしたままにする）
python src/freeagent_bind/server.py --transport http --port 8765
```

```bash
# Hermes 側は url で登録する（command / args は使わない）
hermes config set mcp_servers.freeagent-bind.url http://127.0.0.1:8765/mcp
hermes config set mcp_servers.freeagent-bind.headers.x-freeagent-harness hermes
```

- 待受は既定で **127.0.0.1 のみ**。`--host 0.0.0.0` にするなら `--token <値>` を必ず付けてください
  （付けないと同一ネットワークの誰でも呼べます）。認証は `Authorization: Bearer <値>` か `x-freeagent-token`。
- **二重起動は意図的に失敗します**（状態が 2 つに割れると気づけないため）。別ポートは `--port` で指定します。
- トランスポートは `--transport` / `FREEAGENT_TRANSPORT`（既定 `stdio`）。残りは `FREEAGENT_HTTP_*`（[環境変数](#-環境変数)）。
- HTTP の起動は待受まで実測 463ms（同じ環境の import 単体は 101ms）。**stdio の起動時間は変わりません**
  （`http.server` は HTTP のときだけ読み込みます）。
- 共有されるのは**レート制限と統計**です。推論の同時実行数の上限（`FREEAGENT_MAX_WORKERS`）は
  プロセス全体で 1 つになるので、クライアントを増やしても並列度は増えません。
- HTTP では `FREEAGENT_HARNESS` の env が渡らないので、目印は**ヘッダ**で付けます（上の `headers`）。
  付け忘れると「ハーネスを判別できません」のログが出ます（動作は変わりません）。

#### Windows で常駐させる（見張りつき・黒いウィンドウを出しません）

手で起動したプロセスはログオフで消えるので、Windows では「ログオン時に起動し、落ちたら戻す」配線を置きます。
**その配線は repo から生成できます**（機械固有のパスは引数で与えます。既定は `%LOCALAPPDATA%\hermes`）。

```bash
python scripts/install_resident_windows.py                          # 表示のみ（1 バイトも書きません）
python scripts/install_resident_windows.py --write --force           # 生成（worker / launcher / ログオン起動 / 切替スクリプト）
python scripts/install_resident_windows.py --write --force --task    # 5 分ごとの見張りタスクも登録
```

| 生成物 | 役割 |
|---|---|
| `<hermes-home>\freeagent-service\run_server.pyw` | コンソールを作らない起動役（`pythonw.exe` から走ります） |
| `<hermes-home>\freeagent-service\Hermes_Freeagent.vbs` | worker。待受済みなら何もせず終了（二重起動の防止） |
| `Startup\Hermes_Freeagent.vbs` | ログオン時に worker を隠して起動 |
| `…\apply_freeagent_http.sh` / `revert_freeagent_stdio.sh` | `hermes config` の切替と戻し |

- **生存確認は WMI（`Win32_Process` のコマンドライン照合）**で行います。子プロセスを作らないので、
  netstat のためのコンソールも生まれません。WMI が使えない環境では `WinHttp` の接続確認に落ちます。
- **サーバー本体は `pythonw.exe`（GUI サブシステム）で起動し、ログはファイルへ直接追記**します。
  `WshShell.Exec("cmd /c netstat …")` や `cmd /c … >> log` は**使いません**。子プロセスにコンソールが付き、
  Windows 11（既定のターミナルが Windows Terminal）では**数分おきに黒いウィンドウが一瞬出ます**
  （実測: 5 分周期の見張りで毎回 1 個。ウィンドウを隠す指定でも表示されました）。
- 見張りは `schtasks` の 5 分間隔で、**生きていれば何もしない**冪等な設計です。落ちていたときだけ起動します。
- 二重起動の判定は、launcher が付ける目印 `freeagent-resident-<port>` と、旧来の
  `server.py … --port <port>` の**両方**に当てます（片方だけだと、起動方式を変えた直後に二重起動します）。
- 既定のポートは 8790 です（`--port` で変更できます。Hermes 側の `url` も同じ値にしてください）。

## 💬 会話での頼み方

引数を覚える必要はありません。最初は用途を普通の言葉で頼み、選ばれなければツール名を添えてください。

**設計のレビュー**

```text
この設計案の弱点を freeagent_panel で 2 つのモデルに聞いて。
共通する指摘と、意見が割れた指摘を分けて。実際に答えたモデル数も示して。
```

**根拠のある調査**

```text
freeagent_lookup で「宇宙エレベータの材料研究」を arxiv と crossref から調べて。
関連する論文を 5 件選び、出典 URL と公開年を示して。検索結果だけで断定しないで。
```

**複数の文章の整理**

```text
freeagent_map で、以下のタイトル 30 件を各 1 行に要約して。
失敗した項目があれば一覧に残して。最後に reduce で全体の傾向を 3 行にまとめて。
```

**複雑な検討を継続する**

```text
freeagent_think で、この設計案を手順に分解してから検討して。
前提が変わったら改訂し、別案は分岐して採用・棄却を記録して。
別のモデルへの反論依頼は、重要なステップだけにして。
```

## 🧰 どのツールを使うか

**まずは4つだけ覚えれば十分です。** 検索結果なら `lookup`、第二意見なら `ask`、
複数の意見なら `panel`、検討の記録なら `think`。出典を渡して回答まで頼むなら `grounded` を使います。

| 目的 | ツール | 使い分け |
|---|---|---|
| モデルを探す・応答できるか確かめる | `freeagent_models` | `probe=true` は実際の推論を行い、利用枠を消費します |
| 1 モデルに下読み・下書きを頼む | `freeagent_ask` | 第二意見を 1 回だけ欲しいとき |
| 同じ問いを複数モデルに聞く | `freeagent_panel` | 一致点・食い違いを知りたいとき |
| 複数の問いを複数モデルに当てる | `freeagent_fanout` | プロンプト×モデルの比較。プロンプトは最大 16 件、組合せは既定で最大 40 呼び出し |
| 出典を読ませてから答えさせる | `freeagent_grounded` | 検索＋サブ LLM の回答 |
| 出典つきの検索結果だけを取る | `freeagent_lookup` | LLM による生成なし。`limit` は各ソースの件数 |
| 同じ指示で多数の要素を処理する | `freeagent_map` | 最大 64 件。`reduce` で追加の統合処理 |
| 前提を更新しながら往復相談する | `freeagent_consult` | 1 回だけなら panel。深い討論は待ち時間が増えます |
| 計画・仮説・改訂・別案を記録する | `freeagent_think` | [思考ノートの使い方](#-複雑な問題を段階的に考えるfreeagent_think) |
| サブが自分で知識検索する | `freeagent_agent` | 読み取り専用の調査ループ。ステップ上限があります |
| Hermes 本体を別プロセスで動かす | `freeagent_delegate` | **既定では無効**。フルツールを使うため、許可すると実作業の副作用もありえます |

複数モデルを使うツールは、`models` を省略すると、資格情報・品質統計・クールダウンなどを考慮して候補を選びます。
**選んだ全モデルの応答を保証するものではありません。** 初回は `size=2` 程度で試すと切り分けやすくなります。

モデルを固定したい場合は、`freeagent_models` が返す `ref`（`provider/model`）を使います。
`ask` / `map` は単数の `model`、`panel` などは配列の `models` です。
HF はモデル ID の末尾に `:提供元` を付けて経路を固定できます。

### 🚦 Free モデルを増やしても 429 を踏まない仕組み

複数モデルを使うツールは、必ず**提供元ごとの同時実行枠**を通ります。枠を超えた呼び出しは
**429 を踏む代わりに順番待ち**し、待ち上限（`FREEAGENT_GATE_WAIT_S`・既定 20 秒）を過ぎたら
その枠を**脱落として返します**（黙って消しません）。

| 何ができるようになったか | 実測（この環境・8 モデル） |
|---|---|
| `models` に 8 件を明示すると **8 件すべて走る**（以前は `size` 既定 3 で 3 件に切り落とされていた） | 旧 3 件 → 新 8 件 |
| `size` の上限 6 → 8（`FREEAGENT_MAX_SUB_MODELS`） | `freeagent_panel(size=8)` が在庫 8 体で走る |
| `models: ["all"]` で在庫の全 Free（対話モデル）を 1 語で展開 | 8 モデルの panel が 1 回で走る |
| 並列度（スレッド）4 → 8。**提供元が違えば同時に走る** | 同一提供元は枠で直列化（枠待ち 2〜4 件を実測） |
| 429 を受けたらその提供元の枠を 1 本減らし、成功が続けば 1 本戻す | 枠 8 → 429 で 7 → 6 へ自動縮小 |
| **日次の上限**（宣言したもの）を使い切る前に止める | `FREEAGENT_QUOTA_LIMITS=openrouter:rpd=1000` なら 1 日 1,000 回を超えない（UTC 境界で戻る） |
| 429 が続いたらクールダウンを延ばす | 60 → 180 → 600 秒（同じ相手を毎分叩かない） |
| 選抜が「観測 1 件の幸運」に負けない | 旧: n=1 成功が n=50・90% を上回った → 新: Beta 事後で下回る |

結果は `structuredContent.concurrency` に載ります（`per_provider` の `limit` / `cap`、`waited_total`、
`reduced_total`、`scope` は「このプロセスの累計」）。`slot_waited` は**その呼び出しで枠待ちした件数**です。

**429 の主因はモデル単位の upstream capacity**（`The requested model is temporarily at capacity upstream`）で、
こちらの同時数を下げても 0 にはなりません。枠は「自分で撒いた 429」を減らす一次防護であって、
提供元の在庫が尽きている場合（例: `This model is no longer free`）は 404 として別に返ります。

### 📊 オーケストレーションを測る（`scripts/measure_quality.py`）

「何人集めるか・どう統合するか」は**正答率でしか比べられません**（速度は `measure_kb.py`、自発率は
`measure_adoption.py` が見ます）。期待値を宣言したケースで、戦略ごとの正答率・呼び出し数・秒を出します。

```bash
python scripts/measure_quality.py                     # 6 ケース・4 戦略・プール 8 体
python scripts/measure_quality.py --cases all --repeat 2
python scripts/measure_quality.py --strategies single,panel --json
```

- 戦略: `single`（1 体）/ `panel`（多数決）/ `moa`（提案 → 統合）/ `cascade`（2 体で一致したら打ち切り）
- 採点は**機械判定**（数値は許容誤差、文字列は受理リストの部分一致）。主観文の採点はしません
- 結果は `<state>/quality.jsonl` に追記（`--no-write` で止まる）。**実呼び出し**なので Free の枠を使います
- **ケースが少ないと天井に当たります**（実測: 4〜6 問では全戦略 100%）。判断は `--cases all --repeat 2` で

## 🧠 複雑な問題を段階的に考える（`freeagent_think`）

**考えた内容を 1 ステップずつ記録する「思考ノート」です。** 手順の分解・過去ステップの書き直し・別案への
寄り道・仮説の検証を、ノートが**番号つきで覚えて**おきます。会話が長くなって文脈が圧縮されても、
Hermes を再起動しても（既定の保存期限内なら）続きから再開できます。

### ✅ できること

| やりたいこと | メイン LLM への頼み方（例） | ノートに残るもの |
|---|---|---|
| **問題を手順に分解する** | 「まず観測・仮説・検証・対策の 4 段に分けて」 | 計画（サブ目標）と達成状況 ✅ / □ |
| **考えが変わったら前のステップを直す** | 「#1 の前提が違ったので改訂して」 | 元のステップは消さずに「改訂済み」と印を付ける |
| **別の案を試す（分岐）** | 「#2 から別案に分岐して検討して」 | 分岐（どこから分かれたか・未決着／採用／棄却／統合） |
| **ステップ数の見込みを変える** | 「思ったより長くなりそう。全体を 8 ステップに」 | 見積りの推移（見積りを超えたら自動で引き上げ） |
| **仮説を立てて確かめる** | 「原因の仮説を立てて」→「確かめた結果、仮説は外れ」 | 仮説ごとの状態（未検証／支持／反証／保留） |
| **別のモデルに反論してもらう** | 「このステップを独立したモデルに検証させて」（`verify`） | 判定（妥当／要修正／根拠不足）・反論・見落とし |
| **別のモデルに別案を出してもらう** | 「他の可能性も別のモデルに挙げさせて」（`propose_alternatives`） | 代替案（1 体あたり最大 3 件） |
| **ノートを読み返す** | 「さっきの思考ノートを見せて」（`view`）／長い台帳は「現行の道筋だけ見せて」（`brief`） | 何も書き込まない |

引数の名前を覚える必要はありません。**やりたいことを普通の言葉で頼めば、メイン LLM が引数に直して
呼び出します**（詳しい引数は[下の一覧](#-引数の一覧)）。

### 🤝 メインとサブの役割分担

**考えるのはメイン、ノートを付けるのはこのツール、サブ（Free モデル）は外部のチェック役**です。サブが
自分でノートを付けることはありません。

| 役割 | 担当 | すること |
|---|---|---|
| 考える（分解・改訂・分岐・仮説） | **メイン LLM** | ステップの中身を書き、どの案を採るかを決める |
| 記録する | **このツール** | 番号・計画・分岐・仮説の状態を覚える。存在しない番号を指定されたら推測で補わずにエラーで返す |
| 反論する（`verify`） | **サブ**（メイン以外のモデルを選んで使う） | そのステップの弱点・見落としを探す（同意を集める役ではない） |
| 別案を出す（`propose_alternatives`） | **サブ**（検証役とも別のモデル） | 今の道筋とは違う可能性を挙げる。採るかどうかはメインが決める |

サブにはノート全体ではなく**今生きている道筋だけ**を見せます（改訂済みのステップ・棄却した分岐は見せない）。
**メインとのモデル重複は利用者側でも確認してください。** サーバーはメインのモデル ID を照合していないため、
同じモデルをメインにも使う場合は `exclude` で外します。サブにもノートを持たせないのは、呼び出しが増えて遅くなることと、メインの前提に引きずられて
「独立したチェック役」でなくなるためです。

### 🔍 使い方の流れ（例: API が急に遅くなった原因を探す）

```
freeagent_think で、API の p99 遅延が 3 倍になった原因を探して。
まず「観測を集める・仮説を立てる・仮説を検証する・対策を決める」に分解して。
原因の仮説を立てたら、独立したモデルに反論させて、他の可能性も別のモデルに挙げさせて。
確かめた結果は仮説に記録して、外れたら別案に分岐して続けて。
```

メイン LLM はおおよそ次の順に呼び出します。

1. 計画を立てる → `計画: 0/4 達成` が表示される
2. 仮説「原因は新しい ORM の N+1 クエリ」を立て、反論と別案を依頼する（下の出力例）
3. 「クエリ数は変わっていない」→ 仮説を**反証**として記録（`仮説: #2 反証`）
4. 別案「コネクションプールの枯渇」へ**分岐**（`分岐: b1（#2 から・未決着）`）
5. 決着したら分岐を**採用／棄却**として記録し、最後に結論をまとめる

### 👀 表示の見方

実際のバックエンドで上の 2 まで進めたときの出力です（一部省略）。

```
思考 #2（記録 2 件 / 分岐 0 / 修正 0） / 見積り総数 4
計画: 0/4 達成
  □ 1. 観測を集める
  □ 2. 仮説を立てる（#2）
  □ 3. 仮説を検証する
  □ 4. 対策を決める
仮説: #2 未検証
  • #1: 観測・仮説・検証・対策の4段に分解する
  • #2 [仮説]: デプロイ直後から悪化。原因は新しい ORM の N+1 クエリ
検証（独立 2/2 体）: 妥当 0 / 要修正 0 / 根拠不足 2 / 確信度平均 65.0
  ◦ nous/poolside/laguna-s-2.1:free: 根拠不足
      反証: 新しい ORM が導入されたタイミングと p99 遅延の悪化タイミングの一致を示す具体的な証拠が…
      見落とし: ORM 以外の要因（インフラリソース変更、データ量増加、キャッシュ無効化…）
代替案（独立 1/2 体・3 件）:
  ◇ nous/upstage/solar-pro4:free: 「N+1」という前提を外し、クエリプラン変更、インデックス欠落、…
  ✗ nous/stealth/space-bunny-alpha: TimeoutError: timed out
```

| 表示 | 意味 |
|---|---|
| `計画: 0/4 達成`、`✅` / `□` | サブ目標の達成状況。`（#2）` はそのサブ目標を扱ったステップの番号 |
| `仮説: #2 未検証` | 仮説の状態。確かめると `支持` / `反証` / `保留` に変わる |
| `[#5 で改訂済み]` | 後のステップで書き直された（消さずに残している）。サブにはもう見せない |
| `分岐: b1（#2 から・未決着）` | 別案の道筋。決着すると `採用` / `棄却` / `統合` になる。`棄却` の分岐はサブに見せない |
| `見積り総数 4（自動で引き上げ）` | 全体で何ステップかかりそうかの見込み。ステップ番号が見込みを超えたので自動で引き上げた |
| `検証（独立 2/2 体）` | 反論役として選んだ 2 体のうち、実際に答えたのが 2 体 |
| `妥当 / 要修正 / 根拠不足` | 反論役の判定。**多数が「妥当」でも正しいとは限りません** |
| `代替案（独立 1/2 体・3 件）` | 別案役 2 体のうち 1 体が答え、3 件の案が出た |
| `✗ モデル名: エラー` | 答えられなかったモデル。**脱落も隠さず表示**します |
| `検証なし（台帳のみ）` | サブを呼んでいない（ノートに書いただけ）。「検証済み」ではありません |
| `【台帳の閲覧（記録なし）】` | 読み返しただけ。何も書き込んでいない |
| `【台帳の閲覧（記録なし・要約）】` | `brief` 付きで読んだ。現行の道筋だけを出し、省いた件数は下の行に表示する |

### ⏱ 速さの目安（過去の実測）

| 使い方 | サブの呼び出し | 時間 |
|---|---|---|
| ノートに書くだけ（既定） | 0 回 | **ほぼ 0 秒** |
| 反論（`verify`）＋別案（`propose_alternatives`） | 2 体 ＋ 2 体 | **約 24 秒**（うち 1 体はタイムアウトで脱落） |

測定した環境での例であり、現在の応答時間を保証しません。

**サブを呼ぶのは頼んだステップだけです。** 全ステップで反論させると 1 ターンが分単位になるので、
「ここが怪しい」という要所に絞るのがおすすめです。

### 🩹 うまくいかないとき

| 症状 | 理由 | どうする |
|---|---|---|
| 「〜は台帳にありません」と返る | 存在しない番号・分岐・仮説・サブ目標を指定した（推測で補わない仕様） | エラーに付く `known_thoughts` / `known_branches` の中から選び直す。このときノートには何も書かれていない |
| 「この思考は台帳に記録していません」と返る | 反論・別案を頼んだが、サブに到達できなかった（プロキシ停止など） | `hermes proxy start` を確認するか、反論・別案なしで書き直す。検証されていない前提を積まないため、書かずに返している |
| 「セッションは見つかりません」と返る | 期限切れ、または別の ID | `view=true` の閲覧はエラー。思考の追加なら新規ノートとして始まるので、返された ID を使う（古い内容は復元しない） |
| 「思考数が上限」と返る | 1 冊あたり 24 ステップまで | 新しいノートに分ける（`FREEAGENT_THOUGHT_MAX_STEPS` で変更可） |

### 📋 引数の一覧

<details>
<summary>上級者向け：思考ノートの引数を開く</summary>

閲覧（`view=true`）以外では `thought` が必要です。通常はメイン LLM が設定します。

| 目的 | 引数 |
|---|---|
| 基本 | `thought`（ステップの内容・必須）、`session_id`（続けるノート。省略すると新規）、`question`（解いている問い） |
| 分解 | `plan: ["…", …]`（最大 12・送り直すと計画の改訂。同じ文言の項目は達成済みのまま）、`subgoal: n`、`subgoal_done: true` |
| 改訂 | `revises_thought: n`（`is_revision: true` は付けても付けなくてもよい） |
| 分岐 | `branch_from_thought: n`（新しい分岐では必須）、`branch_id`（省略すると `b1`、`b2`… を自動で付け、付けたことを表示） |
| 分岐の決着 | `resolve_branch: "b1"` + `branch_status`（`adopted` 採用 / `abandoned` 棄却 / `merged` 統合 / `open` 再開） |
| 仮説 | `kind: "hypothesis"`、検証は `tests_hypothesis: n` + `hypothesis_status`（`supported` 支持 / `refuted` 反証 / `inconclusive` 保留） |
| ステップ数 | `thought_number`（省略すると末尾＋1）、`total_thoughts`（見込み。増やしても減らしてもよい。省略するとノートの値を引き継ぐ）、`next_thought_needed: false`（結論に入る） |
| サブに頼む | `verify: true`（反論）、`propose_alternatives: true`（別案）、`size`（人数・既定 2・最大 4）、`models` / `prefer` / `exclude`、`max_tokens` |
| 読み返す | `view: true` + `session_id`（大きい台帳は `brief: true` を併用すると現行の道筋だけを返す。省いた件数は `omitted`）。`session_id` の代わりに `card` でも読める |
| KANBAN のカードに紐づける | `card: "t_xxxx"`（KANBAN のワーカーが自分のカード ID を入れる。**同じ card で呼び続けると同じノートを継続する**ので `session_id` を覚えていなくてよい。`view: true` + `card` でそのカードの道筋を読み戻せる（ワーカーとは別プロセスのメインセッションからも同じ card で読める＝後から思考を監査できる）。**カードの完了・ブロックは `kanban_complete` / `kanban_block`** で行い、このノートは書き換えない） |

次の一手の提案（「未検証の仮説があります」「未決着の分岐が 2 本あります」など）は、画面の表示ではなく
メイン LLM 向けの `structuredContent.suggestions` に入ります。

</details>

---

## 📊 出力の読み方

**最初は「実際に答えた数」「失敗したモデルやソース」「出典 URL」を確認してください。**
合意度や確信度が高くても、正しいと確認できたわけではありません。

画面用の日本語は `content`、AI が読み取る詳細データは `structuredContent` に入ります。
以下の項目名は、詳細なツール結果を確認するときの手引きです。

### 📇 モデル一覧は「利用候補」であって「生存保証」ではない

| 表示・項目 | 意味 | これだけでは分からないこと |
|---|---|---|
| `✓ openrouter …` / `ready=true` | キーが設定され、一覧を取得できている（nous はキー不要） | キーの有効性、クレジット残量、個別モデルの応答可否 |
| `未設定 → 検索のみ` | 一覧は取れるが、推論用のキーがない | キーを入れた後に実際に回答できるか |
| `到達不可` | 一覧を取得できない。理由を表示する | 「モデルが存在しない」とは限らない |
| `Free候補` / `usable_now`（今すぐ使用可） | キーなどの条件を満たす**サブLLM に使える**候補数。`usable_now` はクールダウン中を除いた数 | **未検証モデルを含むため、実際に答えられる数ではない** |
| `non_chat_candidates` | 埋め込み・ガード/安全分類器・画像/音声生成など、サブLLM に使えないモデルの件数 | 自動選抜からは外れるが、一覧には表示する（実測: Free 102 件中 30 件） |
| `probe=alive` | 生存確認で実際に回答した | 次の呼び出しでも成功するとは限らない |

生存確認は会話で「Free 候補を少数だけ `probe=true` で確認して」と頼めます。
大量に確認すると時間・無料枠を使うので、初回は 2〜4 件程度で十分です。

| 生存確認の判定 | 意味 | 扱い |
|---|---|---|
| `alive` | 実際に回答した | 候補に残す |
| `slow` | タイムアウト・空応答 | 残す。遅いだけかもしれない |
| `gone` | 404 / 410（未有効・廃止など） | 今回の確認結果から除外 |
| `auth` | 401、または認証・権限の署名を持つ 403 | 除外。認証失敗は提供元単位で既定 15 分記憶 |
| `error` | 429 / 5xx / 402、提供元・CDN 由来の 403 など | 残す。「応答確認済み」ではない |

### 🔎 回答・引用・脱落を見る

| 項目・表示 | 読み方 |
|---|---|
| `answered` / `failed` | 回答した数 / 失敗した数。依頼した数と区別してください |
| `independent_sources` | panel が返す、実際に応答した異なる実モデルの数。モデル間の誤りが統計的に独立という意味ではありません |
| `agreement` | 結論の言い回しが似ている度合い。正答率ではありません |
| `confidence_mean` | モデル自身が申告した確信度の平均。精度の測定値ではありません |
| `served_by` / `fell_back` | 実際に答えたモデル / 代替モデルに切り替わったか |
| `cited_ok` | 本文を実際に注入した出典番号を少なくとも1つ引用したか。**文章が出典に裏付けられているかは検査していません** |
| `unsupported_citations` | agent / grounded が返す、本文を実際に注入していない引用番号（予算切れも含む） |
| `✗` / `×` | モデルや検索ソースの失敗。成功分だけで判断できるか確認してください |
| `⏱` / `timed_out` | 知識取得の締め切りに間に合わなかったソース |
| `truncated` | トークン上限で回答が打ち切られた可能性。完全な回答として扱わないでください |

並列相談では、代替先が他の枠や `exclude` に重なる場合、その枠を脱落として返します。
モデルを重複させて依頼数を埋めるより、**実際に得られた意見の数を正直に示す**設計です。

## 🔧 つまずいたとき

まず「登録」「検索」「推論」のどこで失敗しているかを分けます。

| 症状 | 確認・対処 |
|---|---|
| ツールが出てこない | `hermes mcp test freeagent-bind` で登録と接続を確認。設定変更後は Hermes を再起動 |
| 接続テストは成功したが回答できない | 接続テストは 11 ツールの発見まで。`freeagent_ask` で実応答を試し、キー・利用枠・プロキシを確認 |
| 検索はできるが相談だけ失敗する | 推論の接続問題です。キー未設定なら設定。Nous を使うなら `hermes proxy start` |
| `nous` が `WinError 10061` | ローカルプロキシが停止しています。他プロバイダが使えるなら全体は止まりません |
| `401`、認証を示す `403` | キー・トークン権限を確認。HF では **Make calls to Inference Providers** が必要。変更後は再起動 |
| HF の一部だけ `403` / Cloudflare 1010 | キーではなく提供元・CDN の拒否かもしれません。`model:提供元` で別経路を試す |
| HF が `402` | 月次クレジット枯渇。連続再試行せず別プロバイダへ。キーを変えるだけでは直りません |
| NVIDIA が `404` / `410` | 未有効・廃止など。一覧の件数を信用せず、少数を `probe=true` で確認 |
| `429` / クールダウン | 枠や頻度の制限。待つ、`size`・並列度を減らす、別モデルへ。プローブの連打は避ける |
| 10 秒で `TimeoutError` | 接続（TCP / TLS）が上限に届かなかった場合です。生成が長いだけなら、ヘッダ待ちは読み取り上限まで待てます。下の説明を参照 |
| 回答が空 | サーバーはトークン予算を上げて 1 回だけ自動再試行します。問いを短くするか、別モデルへ。予算を増やすと遅延も増えます |
| OpenAlex だけ `503` / `429` | 匿名検索が制限される場合があります。`OPENALEX_API_KEY` を設定。成功した他ソースは利用可能 |
| arXiv が `406` | 提供元の拒否。間隔制御と最大 3 回の再試行でも失敗する場合があります。他ソースで続行 |
| GitHub のコード検索だけ失敗 | `GITHUB_TOKEN` / `GH_TOKEN` が必須（10 req/分。リポジトリ / Issue 検索は 30 req/分で別枠）。不要なら `github_kind="repo"` / `"issue"` |
| GitHub のコード検索が「該当なし」 | 索引は default branch のみ・384KB 未満・直近 1 年に活動のあるリポジトリなどに限られます。「探して出ない」は「存在しない」ではありません。`repo:` `path:` `language:` `filename:` を付けて絞ってください |
| 検索結果が問いと合わない | 言語・検索語を変えてソースを絞る。英語の問いを日本語 Wikipedia へ投げると別の記事が返る場合があります |
| `⏱` が付いた検索ソースがある | 取得は裏で継続。成功してキャッシュに入った後、同じ条件の問いを再実行すると返ることがあります |
| 依頼した数より回答が少ない | 候補不足、推論失敗、代替候補の重複回避など。`selection.notes` とモデルごとのエラーを確認 |
| 思考ノートが見つからない | 期限切れ・ID 違い。閲覧はエラー、思考の追加なら新規開始。古い内容は復元されません |
| AI が自分から使わない | 登録だけで自動利用は保証されません。まず会話でツール名を指定。必要なら[自動利用の設定](#-自動的に使わせたいとき) |
| ログに「ハーネスを判別できません」 | stdio は `FREEAGENT_HARNESS=hermes`、HTTP は設定の `headers` に `x-freeagent-harness: hermes` を入れて再起動。判別不能でも動作は変わりません |
| HTTP モードが起動しない（ポート使用中） | 二重起動は意図的に失敗します。`--port` を変えるか、動いているプロセスを止めます |
| HTTP で `401` | `--token` を設定したのにヘッダを送っていません。`Authorization: Bearer <値>` を付けます |
| HTTP で `404 session not found` | セッションが期限切れです（既定 30 分・`FREEAGENT_HTTP_SESSION_TTL`）。クライアントは initialize からやり直します |
| HTTP で「ハーネスを判別できません」 | HTTP では env が渡りません。設定の `headers` に `x-freeagent-harness: hermes` を入れます |

### ⏳ タイムアウトの考え方（接続と読み取りを分離）

`FREEAGENT_CONNECT_TIMEOUT` は既定 10 秒、`FREEAGENT_READ_TIMEOUT` は既定 180 秒です。
**接続（TCP と TLS ハンドシェイク）の間だけ**が接続上限の対象で、応答ヘッダの待ち時間と本文の読み取りは
読み取り上限まで待てます。生成完了までヘッダを返さない Nous ローカルプロキシでも、
`max_tokens` を増やした長い生成が 10 秒で打ち切られることはありません。

- 接続できないホストへは、これまでどおり接続上限（既定 10 秒）で fail fast します。
- 応答が読み取り上限を超えて止まった回は失敗します。`max_tokens` を下げるか、
  `FREEAGENT_READ_TIMEOUT` を上げてください。
- **`connect_timeout`（MCP 起動時の上限）とは別の設定です。**

実装と回帰テストの詳細は [SPEC.md](SPEC.md) の「5. 呼び出し」にあります。

### 🚫 失敗の後に同じ呼び出しを繰り返さない

失敗応答の `structuredContent.next_action` に、状況（`kind`）・助言（`advice`）・代替手段が入ります。
認証は設定を直し、429 は待つか別モデルへ、接続不可は復旧するまで代替の検索などで続行します。
**接続不可・タイムアウトでは、推論の品質統計・トレース・クールダウンを記録しません。**
ただし通常のモデル側の失敗（429 など）は記録します。思考ノートの記録だけの呼び出しは、推論接続がなくても保存できます。

## 🚧 期待しないこと（実測に基づく）

**この MCP を入れても「超高性能」にはなりません。** メインの能力を置き換えるのではなく、
追加の意見と出典を集め、検討の道筋を残すための補助です。

| 期待されやすいこと | 実際の制約 |
|---|---|
| モデルが多ければ必ず正しい | 同じ誤りを共有する場合があります。小型モデルに限らず、Free 候補の品質はまちまちです |
| 並列なら会話全体が速くなる | 独立処理を並列化できますが、相談を追加すると総時間はむしろ増えることがあります |
| 出典があれば誤りが消える | 検索元の誤り・関連性不足は残り、LLM の解釈・推論も間違いえます |
| 生存確認すれば以後は失敗しない | モデル・権限・利用枠は変わります。過去の応答は次の成功を保証しません |
| 思考ノートが自動で考えてくれる | 中身を書くのはメイン LLM。ノートは保存と参照を担当します |
| 登録すれば必ず自動的に使う | ツール選択はメイン LLM と設定に依存します |
| Internet Archive で日本語を検索できる | IA のメタデータ検索は日本語（CJK）クエリを拒否します（実測 10/10 が `QUERY_NOT_READY`）。英語の語で指定してください |
| Qiita を未認証で連続検索できる | 未認証は 60 回/時です。同じ問いはキャッシュで即返りますが、別の問いは 1 分待ちます（`FREEAGENT_QIITA_TOKEN` で 1,000 回/時・3.6 秒間隔） |
| HTTP にすれば大量並列をさばける | 共有できるのはキャッシュ・レート制限・統計・**提供元ごとの枠**です。枠はプロセス内のカウンタなので、クライアントを増やすと上流への合計はその分増えます（1 プロセスに寄せるほど枠が効きます） |
| HTTP なら安全に外へ出せる | 既定は 127.0.0.1 のみ・トークンなしです。`--host 0.0.0.0` にするときは必ず `--token` を付けてください。TLS は提供しません |
| HTTP で複数マシンから共有できる | 単一ホスト前提です（ループバック）。複数マシン間の調整はしません |
| サーバー起点の通知が必ず届く | SSE（`GET`）を張ったセッションにだけ届きます。張らないクライアントでは、警告は最初のツール結果に載ります（stdio と同じ） |
| 正式名称で引けば必ずその記事が返る | ja.wikipedia の検索順位は語によって大きく崩れます（実測: 「大規模言語モデル」は記事があるのに 20 件中 19 位）。**タイトルが問いと完全一致する記事だけ**を先頭に昇格させる対策を入れてありますが、それ以外の並びは提供元の順位のままです |
| 曖昧な語でも分野を当ててくれる | 分野が複数ある語は外れます（実測: wikidata の `Transformer` は 1 位が「変圧器」で、機械学習の項目は 2 位。**言語を en にしても電気機器が 1 位なので直りません**）。分野を指定したいときは問いに文脈語を足すか、openalex / crossref を使ってください |

**過去の実測例（環境・モデル・時間帯で変動します）**：4 体の panel は 14〜20 秒、
多段討議を含む 1 ターンは 3 分 52 秒でした。思考ノートの記録だけならサブ呼び出しは 0 回、
反論＋別案を付けた回は約 24 秒で、提案者 2 体のうち 1 体がタイムアウトしました。
速度の保証や、現在のモデル全体の性能評価ではありません。

<details>
<summary>開発者向け：過去の起動時間・ツール定義サイズの計測記録</summary>

バックエンド拡張時のローカル実測（2026-10-02、同一Python、各7回の中央値。
起動〜initialize〜tools/list〜終了を含むため、起動単体の保証値ではありません）:

| 測定 | 第3段階HEAD（28f89d1） | 第4段階追加後 | 推論プロバイダ追加後 | CiNii追加後（1f653b6） | 第6段階追加後 | GitHubコード検索の改良後 | 第7段階（医学・生物を外しHN/SWH/Libraries.io追加）後 | 非チャット除外後 | 第8段階（Internet Archive追加）後 | 第9段階（Qiita追加）後 | v0.8.1（記述の整備）後 | v0.8.2（台帳の競合・subgoal契約）後 | v0.8.3（レート予算・429再試行）後 | v0.9.5（提供元ゲート・モデル数上限）後 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ツール数 | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 |
| ツール一覧JSON文字数（ensure_ascii=false・compact） | 13,116 | 13,220 | 13,278 | 13,513 | 14,092 | 14,230 | 14,316 | 14,399 | 14,654 | 14,882 | 15,870 | 15,972 | 15,972 | 17,064 |
| stdioプロセス往復・終了（7回中央値） | 0.2216秒 | 0.2267秒 | 0.2181秒 | 0.1652秒 | 0.1664秒 | 0.1611秒 | 0.2305秒 | 0.0281秒 | 0.2444秒 | 0.2605秒 | 0.2431秒 | 0.2431秒 | 0.2431秒 | 0.2282秒 |

文字数の列は同じ方法で測り直した値です（`8f0df1e` で 13,278 を再現して方法を確認）。
往復時間は実行環境の負荷で変わり、同じコミットでも 0.22 秒 → 0.17 秒の差が出ます。
第6段階で **+579 文字（+4.3%）** 増えました。追加ソース名は `sources` の説明に載るため、
ソースを増やすほどメイン LLM の入力は重くなります（既定6ソースは変えていません）。
推論プロバイダの追加（Vercel AI Gateway / Ollama Cloud）はツール定義を変えないため、
文字数は 14,092 のままでした。GitHub コード検索の改良で `github_kind` の説明が伸び、
**+138 文字（+1.0%）** の 14,230 になりました。
第7段階では生命・医学系 9 ソースを外し HN / Software Heritage / Libraries.io を足したため、
説明は入れ替わりで **+86 文字（+0.6%）** の 14,316 になりました（ツール数は 11 のまま）。
非チャットモデルの除外では `freeagent_models` の説明が伸びて **+83 文字（+0.6%）** の 14,399 に
なりました。第8段階では `sources` の説明に `ia`（Internet Archive）を足したため
**+255 文字（+1.8%）** の 14,654 になりました（ツール数は 11 のまま）。**文字数の列は決定的**なので
列どうしを比較できますが、往復時間は実行環境の負荷で
大きく変わるため、今回の 0.0281 秒は実行環境が空いていた値で、上の列とは直接比較できません。
第8段階の 0.2444 秒は第7段階（0.2305 秒）と同じ計測方法で測った値です。
第9段階では `sources` の説明に `qiita`（Qiita の日本語技術記事）を足したため
**+228 文字（+1.6%）** の 14,882 になりました（ツール数は 11 のまま）。第9段階の 0.2605 秒も
第8段階（0.2444 秒）と同じ計測方法で測った値です（HEAD の 14,654 文字は同じ方法で再現して確認）。

v0.8.1 では**説明の整備**（規約 5 の 3 マーカーを全ツールに揃え、重複を削除）と
`lang` / `provider` / `sources` の説明の事実追及だけを行った。ツールは 11 のまま。
文字数は **+988 文字（+6.6%）** の 15,870 になった（内訳: 3 マーカーの統一・`provider` 一覧の
生成化・`lang` と `sources` の説明の精緻化）。**往復時間は 0.2431 秒**で第9段階（0.2605 秒）と同程度。

v0.8.2 は**台帳の複数プロセス競合の修正**と**`subgoal` の契約の修正**だけを行った。
ツールは 11 のまま。文字数は **+102 文字（+0.6%）** の 15,972（`subgoal` の説明が
「plan 項目の id」と「位置」の両方を明記するぶんだけ）。往復時間は 0.2431 秒（変わっていない）。

v0.8.3 は**レート予算・429 の再試行・inflight 待ちの上限**の修正。
**ツール定義は変わらない**（文字数 15,972 のまま・往復時間も 0.2431 秒のまま）。

v0.9.5 は**提供元別の同時実行ゲート**と**モデル数の上限の引き上げ**（SPEC §5.1）。
ツールは 11 のまま。文字数は **+873 文字（+5.4%）** の 17,064（`models` の `["all"]`、`size` の上限、
枠待ちの説明を `description` に足したぶん。**ツールを増やさずに能力を足す**方針なので、増分は説明に寄る）。
往復時間は 0.2282 秒。**8 モデルの panel で 429 が 0〜1 件**（枠 6）・回答 6〜7/8・壁 6.0〜6.3 秒。

v0.11.0 は**オーケストレーションの改善**（SPEC §5.2）: 選抜を Beta 事後（kind を n で重み付け・
決定的）に、**クォータ窓**（宣言した上限を UTC 日境界で守る）を追加、**429 のクールダウンを
はしご**（60→180→600 秒）に、**正答率の測定器** `scripts/measure_quality.py` を追加。
**ツール定義は変えていない**（11 のまま・文字数 17,064 のまま・往復 0.2282 秒）。
`structuredContent` には `quota` が増える（宣言が無ければ `null`）。

v0.9.0 は**モデル一覧の取得の高速化**（並列・締め切り・プロセス間キャッシュ・SPEC §4.4）。
ツール定義は変わらない（文字数 15,972 のまま）ので上表の列は増やしていない。実測（2026-10-21・この環境）:

| 計測 | 変更前 | 変更後 |
|---|---:|---:|
| `all_models()`（冷・直列 → 並列） | 0.842〜1.135 秒 | **0.693 秒** |
| 別プロセスから `all_models()`（ディスクが温かい） | 0.7965 秒 | **0.0050 秒** |
| 新しいプロセスで最初の選抜まで（1 回目 / 2 回目 / 3 回目） | 0.311 / 0.504 / 0.746 秒 | 0.297 / **0.0025** / **0.0024** 秒 |

v0.9.1 は**wikidata の接続の再利用（keep-alive）**（SPEC §6.1.1）。`kb_wikidata` は api.php を
最大 3 回直列に呼ぶため、呼び出しごとに TCP + TLS を張り直していた。**専用の道 `kb_json_keepalive`
（§5.2.1）**を新設し、1 本の接続で 3 回を通す（共用経路 `kb_http` には混ぜない＝リダイレクト追従・
プロキシ env・読み取り途中の打ち切りと相互作用させない）。ツール定義は変わらない（文字数 15,972 の
まま）。実測（2026-10-21・この環境）:

| 計測 | 変更前 | 変更後 |
|---|---:|---:|
| `kb_wikidata`（`Transformer`・ja・3 件） | 2.365 秒 | **1.912 秒**（**−0.453 秒・19%**） |
| 新規接続の本数（3 呼び出し） | 3 | **1** |

`FREEAGENT_KB_KEEPALIVE=0` または `FREEAGENT_KB_POOL_MAX=0` で無効化（共用経路へ委譲）。
在庫は origin ごとに `FREEAGENT_KB_POOL_MAX` 本（既定 2）・空きは `FREEAGENT_KB_POOL_IDLE` 秒（既定 120）で
捨てる。プロキシ環境では常に無効。回帰は `tests/test_kb_wikidata.py`（17 件・ネットワーク不要）。

v0.9.2 は**テストコードの品質のみ**（`tests/test_resident_installer.py` の `ResourceWarning` を解消。
`open(...).read()` を `with open(...) as fh:` に直した）。**サーバーの挙動は v0.9.1 と同じ**で、
ツール定義も変わらない（文字数 15,972 のまま）。


検索APIの追加は推論モデルの能力を増やしません。ツールスキーマの増加もメインLLMの入力負担になります。

HTTP トランスポート（v0.6.0）は**ツール定義を変えない**ため、ツール一覧JSONは 14,882 文字のまま、
stdio の起動も変わっていません（同じ環境で HEAD 103.3ms → 100.7ms、各 7 回中央値。差はノイズ）。
HTTP の待受までは 463ms（4 回のうち初回を除く中央値）でした。

</details>

## 🔑 API キーの設定

**キーは「推論（サブ LLM）用」と「知識検索用」の 2 系統に分かれます。** 使う提供元だけ設定してください。
キーがあること、モデル一覧が見えること、実際に推論できることは別です。

### 📝 どこに書くか（Hermes Agent の `.env`）

**キーは Hermes Agent の `.env` に書きます。** Windows では `%LOCALAPPDATA%\hermes\.env`、
1 行 1 つ `KEY=VALUE` の書式です。

| 使い方 | キーの置き場所 |
|---|---|
| **常駐 HTTP**（`install_resident_windows.py` で配線した場合・推奨） | 上記の `.env`。launcher（`run_server.pyw`）が起動時に読みます。常駐サーバー固有の確認フラグだけを `%LOCALAPPDATA%\hermes\freeagent-service\freeagent.env` に分けても構いません（同じ書式） |
| **stdio**（既定） | Hermes の MCP `env`（`hermes config set mcp_servers.freeagent-bind.env.<変数名> …`）。**この場合はサーバーが `.env` を自動で読みません** |

- 反映には**常駐サーバーの再起動**が要ります（見張りタスクは生存確認だけで、動いている
  プロセスの環境は読み直しません）。
- **値の後ろにインラインコメントを書かないでください**（`KEY=1  # 説明` は値に混ざり、確認フラグが
  効きません）。説明は行を分けて書きます。
- 手順の詳細は [📤 キーの渡し方](#-キーの渡し方) にあります。

#### キー名の一覧（推論＝サブ LLM）

| 変数名 | 別名 | 提供元 |
|---|---|---|
| `OPENROUTER_API_KEY` | — | OpenRouter |
| `NVIDIA_API_KEY` | — | NVIDIA NIM |
| `HF_TOKEN` | `HUGGINGFACE_API_KEY` / `HUGGINGFACEHUB_API_TOKEN` | Hugging Face |
| `GEMINI_API_KEY` | `GOOGLE_API_KEY` | Gemini Developer API |
| `GLM_API_KEY` | `ZAI_API_KEY` / `ZHIPUAI_API_KEY` | Z.ai（Zhipu GLM） |
| `OLLAMA_API_KEY` | — | Ollama Cloud |
| `GROQ_API_KEY` | — | Groq |
| `CLOUDFLARE_API_TOKEN` ＋ `CLOUDFLARE_ACCOUNT_ID` | — | Cloudflare Workers AI（ID は 32 桁 hex） |
| `AI_GATEWAY_API_KEY` | — | Vercel AI Gateway |
| `MISTRAL_API_KEY` | — | Mistral La Plateforme |
| `MODELSCOPE_API_KEY` | `MODELSCOPE_API_TOKEN` | ModelScope |

```bash
# Hermes Agent の .env に置く行（使う提供元の行だけ値と一緒に残し、他は消すかコメントアウト）
OPENROUTER_API_KEY=
NVIDIA_API_KEY=
HF_TOKEN=
GEMINI_API_KEY=
GLM_API_KEY=
OLLAMA_API_KEY=
GROQ_API_KEY=
CLOUDFLARE_API_TOKEN=
CLOUDFLARE_ACCOUNT_ID=
AI_GATEWAY_API_KEY=
MISTRAL_API_KEY=
MODELSCOPE_API_KEY=
```

**キーだけでは有効にならない提供元があります**（契約プラン・データ条件の利用者申告）。
プラン内容を確認してから、次の行を `=1` で有効にしてください
（未設定＝無効。詳細は下の表と [環境変数](#-環境変数)）。

```bash
FREEAGENT_GROQ_FREE_TIER=
FREEAGENT_CLOUDFLARE_FREE_PLAN=
FREEAGENT_GEMINI_FREE_TIER=
FREEAGENT_GEMINI_UNPAID_DATA_ACK=
FREEAGENT_VERCEL_FREE_TIER=
FREEAGENT_OLLAMA_FREE_PLAN=
FREEAGENT_ZAI_FREE_TIER=
FREEAGENT_MISTRAL_FREE_PLAN=
FREEAGENT_MODELSCOPE_FREE_PLAN=
```

#### キー名の一覧（知識検索）

| 変数名 | 別名 | 対象 |
|---|---|---|
| `OPENALEX_API_KEY` | — | openalex |
| `GITHUB_TOKEN` | `GH_TOKEN` | github（コード検索は必須） |
| `FREEAGENT_MAILTO` | — | crossref / openalex / datacite（キーではなく連絡先） |
| `FREEAGENT_CINII_APPID` | — | cinii（このソースを使うなら必須） |
| `FREEAGENT_LIBRARIESIO_KEY` | — | librariesio（このソースを使うなら必須） |
| `FREEAGENT_QIITA_TOKEN` | — | qiita（任意。60 → 1,000 回/時） |

```bash
OPENALEX_API_KEY=
GITHUB_TOKEN=
FREEAGENT_MAILTO=
FREEAGENT_CINII_APPID=
FREEAGENT_LIBRARIESIO_KEY=
FREEAGENT_QIITA_TOKEN=
```

接続先を差し替えたいときだけ `FREEAGENT_<提供元>_BASE_URL`（例 `FREEAGENT_ZAI_BASE_URL`）も使えます。

### 🖥 推論バックエンド（サブ LLM）の接続

**全社の設定は不要です。** Nous のプロキシか、既に利用できる提供元を1つ選んで始めてください。

別の AI に答えさせる機能（`ask` / `panel` / `consult` / `grounded` / `agent` / `map` / `fanout`、
`think` の `verify` / `propose_alternatives`）で使います。**12 プロバイダのうち、使うものを 1 つ以上**用意します。

| プロバイダ | 必要なもの | 入手先・注意点 |
|---|---|---|
| **Nous**（キー不要） | `hermes proxy start` で起動するローカルプロキシ | `hermes portal` でログイン。Portal の利用枠が必要。プロキシ停止中はこのプロバイダだけ使えません |
| **OpenRouter** | `OPENROUTER_API_KEY` | [API Keys](https://openrouter.ai/settings/keys)。`:free` モデルでもキーは必須。レート制限・モデル単位の拒否があります |
| **NVIDIA NIM** | `NVIDIA_API_KEY` | [NVIDIA Build](https://build.nvidia.com)。一覧に未有効・廃止モデルが混じるため生存確認を推奨 |
| **Hugging Face** | `HF_TOKEN` | [Access Tokens](https://huggingface.co/settings/tokens)。**Make calls to Inference Providers** 権限と月次クレジット残が必要 |

<details>
<summary>追加の8プロバイダ：無料プラン・無料枠の確認が必要な接続先</summary>

API キーに加えて、契約プランやデータの扱いを確認したことを示す環境変数が必要です。

| プロバイダ | 必要なもの | 入手先・注意点 |
|---|---|---|
| **Groq** | `GROQ_API_KEY` と `FREEAGENT_GROQ_FREE_TIER=1` | [API Keys](https://console.groq.com/keys)。Free tier契約を利用者が確認した場合だけ有効。Free対象モデルIDを限定し、Developer tierは従量課金なのでプラン変更時は確認変数を解除してください |
| **Cloudflare Workers AI** | `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`、`FREEAGENT_CLOUDFLARE_FREE_PLAN=1` | [API Tokens](https://dash.cloudflare.com/profile/api-tokens)。Workers **Free** planの利用者向け。10,000 Neurons/日の共有枠を超えるとFreeでは停止しますが、Workers Paidでは超過分が課金されます。CloudflareはCustomer Contentをモデル学習・サービス改善に使わないとしています（別途ストレージ連携時は保存の可能性あり）。Paid planなら確認変数を設定しないでください |
| **Gemini Developer API** | `GEMINI_API_KEY`（別名 `GOOGLE_API_KEY`）と2つの確認変数 | [Google AI Studio API key](https://aistudio.google.com/apikey)。Free/unpaid tierの入力・出力は製品改善や人手レビューに使われる場合があります。**機密・個人情報は送らず**、Free tierとデータ用途を確認した場合だけ有効にしてください |
| **Vercel AI Gateway** | `AI_GATEWAY_API_KEY` と `FREEAGENT_VERCEL_FREE_TIER=1` | [AI Gateway](https://vercel.com/ai-gateway)。Free tierは**月 $5 のクレジット**で、使えるのはFree Tier対象モデルだけです（実測15件を許可リスト化）。**クレジットを購入するとpaid tierに移り、月次の無料クレジットは消えます** |
| **Z.ai（GLM）** | `GLM_API_KEY`（別名 `ZAI_API_KEY` / `ZHIPUAI_API_KEY`）と `FREEAGENT_ZAI_FREE_TIER=1` | [Z.ai](https://z.ai)。公式価格表で **$0** の `glm-4.7-flash` / `glm-4.5-flash` / `glm-4.6v-flash` だけを許可リストで許可します。**提供元の一覧 API はこの 3 件を返しません**（有料の旗艦 11 件だけ。実測）ので、Free の在庫は許可リストから補います（`synthesize_free_ids`）。有料モデルは残高が無いと 429 を返します（実測）。**Flash 系は思考モデル**なので `max_tokens` に余裕を持たせてください（実測: `glm-4.5-flash` は 256 で思考 202 トークンの後に本文。空応答はサーバーが上限を上げて 1 回だけ再試行します） |
| **Mistral La Plateforme** | `MISTRAL_API_KEY` と `FREEAGENT_MISTRAL_FREE_PLAN=1` | [La Plateforme](https://console.mistral.ai)。無料の **Experiment plan**（月 約10億トークン・電話確認・RPM 低め）を使う場合の申告です。API は Free/paid を返さないため**クレジット枠**として扱い、対象は `probe` で確認します。**並列 fan-out には向かず、長文・少数リクエスト向き**です |
| **ModelScope** | `MODELSCOPE_API_KEY`（別名 `MODELSCOPE_API_TOKEN`）と `FREEAGENT_MODELSCOPE_FREE_PLAN=1` | [ModelScope](https://modelscope.cn)。API-Inference の無料枠（公称 2,000 リクエスト/日・日次回復）の申告です。Qwen / DeepSeek / GLM / Step などが載ります（一覧は未認証でも読めます・実測 37 件）。キー発行に実名・SMS 確認が要る場合があります |
| **Ollama Cloud** | `OLLAMA_API_KEY` と `FREEAGENT_OLLAMA_FREE_PLAN=1` | [API keys](https://ollama.com/settings/keys)。Free planはstarterモデル向けの月次クレジットで、**対象範囲は提供元が公表していません**（`probe`で実際に応答するモデルを確認）。同時1リクエスト。プロンプト/応答はログ・学習されないと提供元が明記しています |

- Groq / Cloudflare / Gemini / Vercel / Ollama / Z.ai / Mistral / ModelScope は、確認変数がそろうまでモデル一覧取得も推論も行いません。これは契約プランをAPIから検証する仕組みではなく、利用者の明示確認です。プランやBilling設定を変更したら、確認変数を解除して再確認してください。
- Groq / Cloudflare / Gemini / Vercel / Z.ai はコード側のFreeモデル許可リストに含まれるIDだけを呼び出し、明示指定でも有料・未許可モデルを拒否します。
- Z.ai は**提供元の一覧 API が Free の ID を返さない**ため（実測: 有料の旗艦だけ 11 件）、公表された Free の ID を在庫として補います（許可リスト方式 + `synthesize_free_ids`）。他の許可リスト方式（Groq / Cloudflare / Gemini / Vercel）は一覧が ID を返すので補いません。Mistral / ModelScope は Free 判定を API が返さないのでクレジット枠＋プラン確認で扱い、対象は `probe` で確認します。
- Ollama は無料対象の範囲が非公開でAPIから判定できないため許可リストを作らず、クレジット枠として扱います。**対象外のモデルは提供元が 402 を返し**、フォールバックします（モデルの失敗としては記録しません）。Vercel も同様に、無料対象はカタログのごく一部です。Free枠・モデルの提供条件は変更されるため、最新の提供元ページも確認してください。
</details>

全プロバイダ未設定でも、知識検索と思考ノート（サブを呼ばない範囲）は使えます。

### 🔐 知識検索（検索ソース）のキー

検索は**基本的にキー不要**です。OpenAlex と GitHub は設定を推奨し、
**CiNii と Libraries.io は利用者自身のキーが必須**です。GitHub のコード検索にもトークンが必要です。

| 変数 | 対象ソース | 効果・注意点 |
|---|---|---|
| `OPENALEX_API_KEY` | openalex | 匿名検索が提供元側で止められることがあり（実測 503/429）、キーで回避。[API 設定](https://openalex.org/settings/api) |
| `GITHUB_TOKEN` / `GH_TOKEN` | github | コード検索は必須。枠はエンドポイント別（認証あり: リポジトリ / Issue 30 req/分・コード 10 req/分。未認証はどちらも 10 req/分） |
| `FREEAGENT_MAILTO` | crossref / openalex / datacite | キーではなく連絡先。polite pool に入り安定します |
| `FREEAGENT_CINII_APPID` | cinii | **このソースを使う場合は必須**。CiNii Research の[利用登録](https://support.nii.ac.jp/ja/cinii/api/developer)で取得したアプリケーションID。未設定なら cinii は HTTP を出さず、登録先を案内します |
| `FREEAGENT_LIBRARIESIO_KEY` | librariesio | **このソースを使う場合は必須**。[Libraries.io](https://libraries.io/account)の無料アカウントで取得する API キー。未設定なら librariesio は HTTP を出さず、登録先を案内します（60 req/分） |
| `FREEAGENT_QIITA_TOKEN` | qiita | **任意**（キーが無くても 60 回/時で動きます）。[個人用アクセストークン](https://qiita.com/settings/tokens/new)を設定すると 1,000 回/時に上がり、アクセス間隔が 60 秒 → 3.6 秒になります |

wikipedia / wikidata / arxiv / doaj / npm / crates / openaire / zenodo / ror /
osv / ietf / inspirehep / oeis / hfhub / hn / swh / ia / qiita はキー不要・匿名で使えます。
**cinii と librariesio だけは例外**で、利用者自身の appid / API キーが必要です
（利用目的に当たるかの判断は利用者。SPEC §6.7 / §6.10）。

### 📤 キーの渡し方

**stdio（既定）では、このサーバーは `.env` を自動で読みません。** Hermes の MCP `env` へ渡してください。
最近の Hermes は [環境変数参照](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference#environment-variable-references)
を解決できるので、キーを Hermes のアクティブプロファイルの秘密情報として保存済みなら、値を重複保存せず参照を設定できます。

#### 常駐 HTTP のとき（MCP `env` は届きません）

常駐 HTTP（上の「Windows で常駐させる」）では、サーバーは**ログオン時に起動される別プロセス**
なので MCP `env` は届きません。launcher（`run_server.pyw`）が起動時に次の 2 つを読みます
（**すでに環境にある値は上書きしません**。値はログに出ず、名前だけが出ます）。

| ファイル | 何を書くか |
|---|---|
| `%LOCALAPPDATA%\hermes\.env` | Hermes が使っているキー（`OPENROUTER_API_KEY` / `NVIDIA_API_KEY` / `HF_TOKEN` / `GEMINI_API_KEY` / `OLLAMA_API_KEY` / `GLM_API_KEY` / `GH_TOKEN` …） |
| `%LOCALAPPDATA%\hermes\freeagent-service\freeagent.env` | 常駐サーバー固有の確認フラグ（`FREEAGENT_*`）。`install_resident_windows.py --write` が**無いときだけ**作ります |

- 反映には**常駐サーバーの再起動**が要ります（見張りタスクは生存確認だけで、動いている
  プロセスの環境は読み直しません）。
- 読み込むファイルを差し替えたいときは `FREEAGENT_ENV_FILE` を設定します。
- **値の後ろにインラインコメントを書かないでください**（`KEY=1  # 説明` は値に混ざり、
  確認フラグが有効になりません。launcher はコメントを落としますが、他の読み手では落ちません）。
  説明は行を分けて書きます。

```bash
# Bash / Git Bash 用。シングルクォートでシェルによる ${...} 展開を防ぐ。
# 秘密情報は Hermes 側に別途保存済みであることが前提。参照だけではキーは作られません。
hermes config set mcp_servers.freeagent-bind.env.OPENROUTER_API_KEY '${OPENROUTER_API_KEY}'
hermes config set mcp_servers.freeagent-bind.env.NVIDIA_API_KEY '${NVIDIA_API_KEY}'
hermes config set mcp_servers.freeagent-bind.env.HF_TOKEN '${HF_TOKEN}'
```

<details>
<summary>追加プロバイダと検索キーの設定例を開く</summary>

**使う提供元の行だけ設定してください。** 以下の `1` は利用者による条件確認です。
確認していないプランやデータ条件を、例のまま有効化しないでください。

```bash
# キーだけでは有効化されず、プラン/データ条件の確認後に確認変数を設定します。
hermes config set mcp_servers.freeagent-bind.env.GROQ_API_KEY '${GROQ_API_KEY}'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_GROQ_FREE_TIER '1'
hermes config set mcp_servers.freeagent-bind.env.CLOUDFLARE_API_TOKEN '${CLOUDFLARE_API_TOKEN}'
hermes config set mcp_servers.freeagent-bind.env.CLOUDFLARE_ACCOUNT_ID '${CLOUDFLARE_ACCOUNT_ID}'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_CLOUDFLARE_FREE_PLAN '1'
hermes config set mcp_servers.freeagent-bind.env.GEMINI_API_KEY '${GEMINI_API_KEY}'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_GEMINI_FREE_TIER '1'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_GEMINI_UNPAID_DATA_ACK '1'
hermes config set mcp_servers.freeagent-bind.env.AI_GATEWAY_API_KEY '${AI_GATEWAY_API_KEY}'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_VERCEL_FREE_TIER '1'
hermes config set mcp_servers.freeagent-bind.env.OLLAMA_API_KEY '${OLLAMA_API_KEY}'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_OLLAMA_FREE_PLAN '1'
hermes config set mcp_servers.freeagent-bind.env.OPENALEX_API_KEY '${OPENALEX_API_KEY}'
hermes config set mcp_servers.freeagent-bind.env.GITHUB_TOKEN '${GITHUB_TOKEN}'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_MAILTO 'you@example.com'
# CiNii を使う場合のみ。値は自分の appid（他人と共有しない）
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_CINII_APPID '<appid>'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_LIBRARIESIO_KEY '${FREEAGENT_LIBRARIESIO_KEY}'
# Qiita のトークン（任意。未認証でも 60 回/時で動くが、入れると 1,000 回/時）
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_QIITA_TOKEN '${FREEAGENT_QIITA_TOKEN}'
```

</details>

キーを直接 `env` に設定する方法もありますが、設定ファイルとシェル履歴に残る可能性があります。
**キーをチャットに貼ったり、リポジトリへコミットしたりしないでください。** 変更後は Hermes を再起動します。
HF は `HUGGINGFACE_API_KEY` / `HUGGINGFACEHUB_API_TOKEN` も読みます。

### 🔬 モデルを確認する（上級者向け）

まず `freeagent_models` で検索し、少数を生存確認してから、返った ref を使用します。
以下は各ツールへ渡す引数の例です（そのまま端末で実行するコマンドではありません）。

```jsonc
// freeagent_models: NVIDIA の Free 候補を 2 件だけ実際に呼ぶ
{"provider": "nvidia", "free_only": true, "probe": true, "limit": 2, "probe_limit": 2}
// freeagent_ask: 上の結果の ref を単数の model へ渡す
{"prompt": "接続確認。ひと言だけ答えて。", "model": "<結果の ref>"}
```

確認結果は品質統計・クールダウン・認証の記憶に反映されます。
タイムアウト・429・空応答などの候補は残すため、自動選抜で再び試される場合があります。
404/410 のクールダウンは既定 1 時間、認証の記憶は既定 15 分です。

## 📚 知識バックエンド

**検索先は24ソースです。何も指定しなければ既定の6ソースだけを使います。**
`sources` を指定すると、指定したソースだけを検索します（既定の6ソースに追加されるのではありません）。

- **論文を探す**：arxiv / crossref / openalex。日本語文献の書誌なら cinii。
- **パッケージを探す**：npm / crates / librariesio。脆弱性なら osv。
- **標準・専門情報を調べる**：ietf（RFC）、inspirehep（素粒子物理）、oeis（整数列）、hfhub（MLモデル）。
- **研究データや機関を探す**：datacite / zenodo / ror。
- **開発者の議論・アーカイブを探す**：hn / swh。
- **当時の一次資料（マニュアル・書籍・ソフト）を探す**：ia（Internet Archive）。**query は英語の語で**指定します。

本文ではなく書誌情報だけの結果もあります。**取得した説明や抄録を読む機能であり、原典の全文を取得する機能ではありません。**

### 場面によって取り方が違います（根拠の充填率）

**書誌を広く集めたい場面**（`freeagent_lookup`）と、**本文の根拠が要る場面**（`freeagent_grounded`）は
別の取り方をします。根拠の要る場面では同じクエリでも**引用の中身が変わります**
（根拠が要る場面だけ引用の中身が変わるのが自然な設計です）。

| 場面 | crossref | Hacker News |
|---|---|---|
| `freeagent_lookup`（書誌を広く） | 絞り込まない | story だけ |
| `freeagent_grounded`（本文根拠が要る） | **`has-abstract:true` で絞る**（充填率 27% → 100%） | **議論のコメントも探す**（story の本文はほぼ空 → 0% → 100%） |

> Hacker News の story は**リンク投稿**なので本文が無く、根拠になりません。
> 議論の中身は**コメント**に書いてあります（`freeagent_grounded` は comment も検索します）。

### 📗 既定の6ソース

| ソース名 | 取得するもの | 注意点 |
|---|---|---|
| `wikipedia` | 検索結果と記事の導入部 | **言語は問いから自動**（日本語・中国語・韓国語なら `ja`、それ以外は `en`。`lang` で明示も可）。**正式名称がそのままタイトルの記事は先頭に昇格**します（日本語の複合語は検索順位が崩れるため。SPEC §6.13 / §6.14） |
| `wikidata` | QID・ラベル・説明・構造化データ | ラベルがない項目もあります |
| `arxiv` | プレプリントのメタデータ・アブストラクト | 3 秒間隔で直列化。プレプリントは査読済みとはなりません。**英語の語で指定**（日本語クエリは 0 件） |
| `crossref` | DOI・論文のメタデータ・公開されているアブストラクト | アブストラクトがない論文もあります（実測の充填率は平均 27%）。`freeagent_grounded` は**抄録の有る論文に絞る**ので根拠が尽きません（SPEC §6.18） |
| `openalex` | 論文のメタデータ・被引用数・アブストラクト | 匿名検索の制限あり。API キーを推奨 |
| `github` | リポジトリ / Issue / コードの検索結果。コード検索は一致箇所の**断片**（`snippet`）も返す | コード検索はトークン必須（10 req/分）。**ライセンス情報を含まない**ので取り込み前に確認 |

### 📘 明示指定で使う18ソース

通常の検索には自動で追加されません。表の名前を会話で指定してください。

| ソース名 | 取得するもの | 注意点 |
|---|---|---|
| `datacite`（明示指定） | DOIメタデータ・抄録、研究データ | `datacite_kind`: `all` / `arxiv` / `dataset`。各語を引用したAND検索＋関連度順 |
| `openaire`（明示指定） | Graph V3の論文書誌・抄録・掲載先 | 匿名API。60.1秒間隔で制御。抄録欠落あり、OpenAIREのクレジットを表示 |
| `zenodo`（明示指定） | 研究データ・ソフトウェア・論文等の公開メタデータ | 説明・注記を取得。全文/ファイルは取得しない。メタデータCC0とファイル条件を分離 |
| `ror`（明示指定） | 研究機関の候補・種別・国・都市・設立年 | v2の構造化情報。論文検索ではなく機関情報の補完。機関を自動同定しない |
| `doaj`（明示指定） | オープンアクセス誌の記事メタデータ・抄録 | 記事メタデータはCC0。OA記事限定。OpenAlexが不安定なときの科学系検索の受け皿 |
| `npm`（明示指定） | npm パッケージの検索結果（名前・説明・版・リンク） | 説明は登録者の自己申告で、品質・安全性の審査結果ではありません |
| `crates`（明示指定） | crates.io のクレート検索結果（名前・説明・版・DL数） | 同上。Rust パッケージ。GitHub の検索枠を消費しません |
| `cinii`（明示指定・**appid 必須**） | CiNii Research の日本語文献の書誌（タイトル・著者・資料名・巻号頁・発行年・DOI/NAID/NCID/ISBN・種別） | **利用者自身の appid** を `FREEAGENT_CINII_APPID` に設定したときだけ動きます（未設定なら登録先を案内して HTTP を出しません）。**抄録は返らない**ので本文根拠には使わず、書誌のみです |
| `osv`（明示指定） | パッケージの既知脆弱性（OSV スキーマ。ID・概要・別名 CVE・深刻度・影響パッケージ） | キー不要・公称レート無し。パッケージ名は PyPI / npm / crates.io / Go / Maven / RubyGems / NuGet / Packagist / Hex / Pub の順に照合し、Maven は `groupId:artifactId` の形が必要。`CVE-` / `GHSA-` / `PYSEC-` 等のIDなら直引き |
| `ietf`（明示指定） | RFC / Internet-Draft の書誌と抄録（RFC 番号・標準レベル・ページ数・版） | キー不要。`RFC 9110` / `rfc9110` / `9110` は name 直引き、`draft-...` は名前部分一致、それ以外は**タイトルの 1 語部分一致**（複数語は最長語で引いて全語一致を優先）。全文検索ではありません。文書は英語 |
| `inspirehep`（明示指定） | 素粒子物理の文献（表題・抄録・DOI・arXiv ID・被引用数） | データは CC0。DOI があれば DOI を出典 URL にします |
| `oeis`（明示指定） | 整数列（A 番号・名前・先頭項・キーワード） | CC BY-SA 4.0 で**出典表示が必須**。数列そのものでも語でも引けます |
| `hfhub`（明示指定） | Hugging Face Hub のモデル（ID・タスク・ライブラリ・DL 数・likes・ライセンスタグ） | モデルごとにライセンス・品質が異なります。summary は `structured_record` |
| `hn`（明示指定） | Hacker News の投稿（表題・本文・点数・コメント数・投稿者） | Algolia HN Search 経由。**帰属表示が必須**（"Search by Algolia"）。リンク投稿は本文が無く `metadata_only`。引用 URL は HN の投稿で、記事 URL は `extra.article_url` に残します |
| `swh`（明示指定） | Software Heritage のリポジトリ（origin）の URL・取得方式 | 公開コードの長期アーカイブ。**コード本文は取得しません**。summary は `structured_record`。API 利用規約はポイントアクセス可・大量抽出不可 |
| `librariesio`（明示指定・**キー必須**） | 19+ のパッケージ登録簿を横断した検索（名前・説明・版・DL数・ライセンス） | **利用者自身の無料 API キー**を `FREEAGENT_LIBRARIESIO_KEY` に設定したときだけ動きます（未設定なら登録先を案内して HTTP を出しません）。説明は登録者の自己申告 |
| `ia`（明示指定） | Internet Archive のアイテム（歴史的な計算機マニュアル・書籍・ソフト等のタイトル・作成者・年・メディア種別・DL数） | **query は英語の語で指定**します。日本語（CJK）クエリは IA 側が拒否するため、HTTP を出さずにエラーを返します（実測 10/10）。`title:(語 AND 語)` に組み直して引きます。summary は**目録・アップロード者の注記**で抄録ではありません。本文は取得しません |
| `qiita`（明示指定） | Qiita の技術記事（タイトル・本文の抜粋・タグ・Likes・Stock・コメント数・投稿者・更新日時） | キー不要。検索構文（`tag:Python` / `user:foo` / `created:>2026-01-01` / `stocks:>50`）が使えます。**未認証は 60 回/時**なので、同じ問いはキャッシュで即返りますが**別の問いは 1 分待ち**ます。`FREEAGENT_QIITA_TOKEN` を設定すると 1,000 回/時・間隔 3.6 秒。記事の著作権は投稿者に帰属します |

`sources` を省略すると**従来の6ソースのみ**、指定すると指定したソースだけを検索します。
追加18ソースを毎回自動送信することはありません。未知の名前は報告し、同じ名前の重複指定は1回にまとめます。
`limit` は**各ソースの上限**です。全体の結果は重複除去などで減るので、合計件数を保証する値ではありません。
本文という場合も、取得できた説明・導入部・アブストラクトを指します。原典の全文とは限りません。

### 💡 目的別の頼み方

以下は会話へ入力する例です。最初に「`freeagent_lookup` で」と添えると、検索だけを頼めます。

| 目的 | 頼み方の例 | ソース名 |
|---|---|---|
| 論文検索を広げる | 「量子計算の論文を DataCite と OpenAIRE から探して」 | `datacite`, `openaire` |
| オープンアクセス論文を探す | 「DOAJ で Transformer の論文を探して」 | `doaj` |
| 研究データを探す | 「DataCite の dataset モードと Zenodo でグラフェンの研究データを探して」 | `datacite`, `zenodo` |
| 研究機関を調べる | 「ROR で CERN の機関候補を探して」 | `ror` |
| 日本語文献を探す | 「CiNii で圧電材料の文献を探して。書誌情報だけでよい」 | `cinii`（appid 必須） |
| パッケージを探す | 「npm と crates.io で JSON スキーマ検証のパッケージを探して」 | `npm`, `crates` |
| 登録簿を横断検索する | 「Libraries.io で JSON スキーマ検証のパッケージを探して」 | `librariesio`（キー必須） |
| 既知の脆弱性を調べる | 「OSV で jinja2 の既知脆弱性を調べて」 | `osv` |
| 標準文書を調べる | 「IETF で RFC 9110 の概要を調べて」 | `ietf` |
| 物理・数学・ML を調べる | 「INSPIRE-HEP でヒッグス粒子の論文を」「OEIS で Fibonacci 数列を」「HF Hub で Llama を探して」 | `inspirehep`, `oeis`, `hfhub` |
| 開発者の議論を探す | 「Hacker News で Rust の所有権についての議論を探して」 | `hn` |
| アーカイブの存在を調べる | 「Software Heritage に cargo のリポジトリがあるか調べて」 | `swh` |
| 当時の一次資料を探す | 「Internet Archive で FORTRAN のマニュアルを探して」 | `ia`（**英語の語で**） |
| 日本語の技術記事を探す | 「Qiita で Python の非同期処理の記事を探して」 | `qiita` |

**CiNii の利用目的は学術研究／非営利の情報利活用に限定され、appid の貸与・譲渡は禁止です。**
自分の利用が条件を満たすか確認してから設定してください
（[利用細則](https://support.nii.ac.jp/sites/default/files/cinii/webapi-term.pdf)・[登録先](https://support.nii.ac.jp/ja/cinii/api/developer)）。
Zenodo は非軍事用途に限ります。各ソースの帰属表示と利用条件も守ってください。

<details>
<summary>上級者向け：検索引数・代替取得・引用の扱い</summary>

ツールへ渡す引数の例（端末コマンドではありません）:

```jsonc
// DataCite経由でarXivのメタデータを取得
{"query":"language model hallucination","sources":["datacite"],"datacite_kind":"arxiv","limit":2}
// 研究データを検索（Dataset型でも品質を保証しません）
{"query":"graphene","sources":["datacite"],"datacite_kind":"dataset","limit":2}
// 分野横断検索
{"query":"quantum computing","sources":["openaire"],"limit":2}
// 公開研究成果のメタデータ（ファイルは取得しません）
{"query":"graphene","sources":["zenodo"],"limit":2}
// 研究機関の候補（論文抄録ではありません）
{"query":"CERN","sources":["ror"],"limit":2}
// オープンアクセス論文の検索（記事メタデータCC0）
{"query":"transformer attention mechanism","sources":["doaj"],"limit":2}
// プログラミング: npm / crates.io のパッケージ検索（説明は登録者の自己申告）
{"query":"json schema validator","sources":["npm"],"limit":2}
{"query":"async http client","sources":["crates"],"limit":2}
// 日本語文献の書誌（利用者自身の appid が必要。抄録は返りません）
{"query":"圧電材料","sources":["cinii"],"limit":2}
// 脆弱性（パッケージ名は生態系を自動照合。Maven は groupId:artifactId）
{"query":"jinja2","sources":["osv"],"limit":2}
// 標準文書（RFC 番号・draft 名は直引き、それ以外はタイトルの1語一致）
{"query":"RFC 9110","sources":["ietf"],"limit":2}
// 物理・数学・ML
{"query":"higgs boson","sources":["inspirehep"],"limit":2}
{"query":"Fibonacci","sources":["oeis"],"limit":2}
{"query":"llama","sources":["hfhub"],"limit":2}
// プログラミング特化（HN は議論、SWH はリポジトリ、librariesio はキー必須の横断検索）
{"query":"rust ownership","sources":["hn"],"limit":2}
{"query":"kubernetes","sources":["swh"],"limit":2}
{"query":"json schema validator","sources":["librariesio"],"limit":2}
// 当時の一次資料（Internet Archive。**query は英語の語で**）
{"query":"FORTRAN manual","sources":["ia"],"limit":2}
// 自然語arXiv検索の失敗・遅延時にのみ、DataCiteの追加利用を明示許可
{"query":"language model hallucination","sources":["arxiv"],"fallback":true,"limit":2}
```

`fallback` は既定 `false`。`true`（JSON真偽値）の場合のみ、arXivの429・5xx・接続障害等なら直ちに、
応答待ちが既定2秒続けばDataCiteのarXiv限定検索を開始します。**全体8秒の締切は延長しません**。
締切を過ぎてから新たな代替取得は開始しません。元のarXiv取得は裏で継続し、成功すればそのキャッシュを温めます。

自然語の簡易検索だけが代替対象です。`ti:` / `cat:`等の検索式、引用符・括弧・AND/OR/NOT、新旧形式のarXiv IDや版指定は
意味を勝手に変えず代替対象外にします。「該当なし」も障害と混同しません。
DataCiteへの直接指定は自然語の各語をAND検索します。arXivと検索順位・更新反映・特定版の内容が同等とは保証しません。

成功した代替は `arxiv → datacite（代替）` と実取得元を表示し、`results.arxiv.fallback.primary_error`に元の失敗を残します。
両方が失敗した場合は `fallback_attempt.error` に代替の失敗も残します。

Zenodoの`summary_kind=metadata_description`は説明・注記の抜粋で、ファイル本文ではありません。
`license=CC0-1.0`はメタデータ、`file_license`/`access_right`は別のファイル条件です。メール欄は返さず、引用符付き/アドレスリテラルを含む説明等のメール表記も省略します。
未対応の`@`表記が残ればその値を返しません。script/style等の非本文とHTMLコメントは除外し、除外だけの結果には本文番号を付けません。
RORの`summary_kind=structured_metadata`は、種別・国・都市・状態・設立年の実属性を整形したものです。
論文の抄録を作る処理ではなく、`established`を出版年にしないため`year`は空です。候補の順位を本人同定・機関同定の確定と解釈しません。

本文が無い結果は、既存・追加ソースを問わず `metadata_only=true`。書誌探索には表示しますが、groundedの本文根拠には注入しません。
agentでも本文根拠の引用番号を振らず、書誌は`bibliography`に分離します。
DOI・URLの両方を別名として照合して引用を統合し、より長い本文と`providers`（配信元一覧）を残します。
選択した本文の取得元は`summary_source`、各提供元のライセンス・種別は`provider_metadata`、クレジットは`attributions`へ保持します。
URL別名の連鎖全体とDOI対応を先に調べ、曖昧な書誌を入力順で特定版へ割り当てません。
agentの番号登録・返却でもDOIを優先し、同じURLの異なる版を落としません。
本文の選択とは別に`doi`と`aliases`を保持するので、結果を再統合しても識別子が失われません。

本文予算で読ませられなかった出典は、取得できていても引用成功とは認定しません。
`injected_citations`が注入済み番号、groundedの`evidence_citation_count`が実際の本文根拠数です。
agentは後のステップで未注入の出典を読ませた場合だけ、その番号を有効にします。
**同じ論文の別配信元は独立した裏付けではありません**。版の異なるDOIは勝手に統合しません。

</details>

<details>
<summary>検索の実出力・過去の速度計測・API利用条件を確認する</summary>

第3段階の実stdio出力（2026-10-01、`CERN`・各2件。下記は実取得の表示）:

```text
出典 4 件（zenodo, ror）
  ✓ zenodo (2 件・2.3 秒)
      データ提供: Zenodo（メタデータCC0・ファイル条件は別） https://zenodo.org/
      LISA promotional material — https://zenodo.org/records/13998413
      2023 CERN openlab Annual Report — https://zenodo.org/records/14289083
  ✓ ror (2 件・0.9 秒)
      データ提供: ROR（CC0・研究機関候補） https://ror.org/
      European Organization for Nuclear Research — https://ror.org/01ggx4157
      Research Infrastructure for Experiments at CERN — https://ror.org/01t0a1151
```

この回は取得4件のうち3件に根拠テキストがあり、締切脱落はありませんでした。
機関候補が2件出ても、両者を同じ機関と決める結果ではありません。
Zenodoも説明のない結果には、ファイルを読んだかのような本文を補いません。

実stdio経路での第1/第2段階の出力（2026-10-06、`large language model`・各2件、題名の行は省略）:

```text
出典 4 件（datacite, openaire）
  ✓ datacite (2 件・1.7 秒)
  ✓ openaire (2 件・1.9 秒)
      データ提供: OpenAIRE（CC-BY） https://graph.openaire.eu/
```

この回は取得4件中3件に本文があり、締切脱落・代替切替は発生しませんでした。
データセット検索も実stdioで2件・1.59秒、本文2件を確認しました。
第3段階の実stdio検証（同日、`CERN`、各2件）ではZenodoが2.33秒、RORが0.87秒、取得4件中3件に根拠テキストがありました。
これは第1/第2段階の速度とは異なる問いのスポット計測です。
第4段階の実stdio検証（2026-10-02）: DOAJ 0.38秒（`transformer attention mechanism`・抄録2件）、
npm 0.34秒（`json schema validator`・説明2件）、crates.io 0.78秒（`async http client`・説明2件）。
第6段階の実stdio検証（2026-10-05、各2件）: osv 0.92秒 /
ietf 0.63秒（`RFC 9110`）/ inspirehep 0.86秒 / oeis 1.29秒 / hfhub 0.28秒。
第7段階の実stdio検証（2026-10-05、各2件）: hn 0.47〜0.5秒（`rust ownership`）/ swh 1.2秒（`kubernetes`）。
第8段階の実stdio検証（2026-10-13、各2件）: ia 0.8秒（`FORTRAN manual`・根拠2件）。
librariesio はキー未設定なら検査・計測せずに外します。脱落したソースは取得が裏で続き、
同じ問いの次回はキャッシュから返ります。
**代替APIにも速度のムラがあります。一時点の成功は24時間の安定性の保証ではありません**。

| 症状 | 理由・対処 |
|---|---|
| 追加ソースが検索されない | `sources`に明示指定してください。既定6ソースは変えていません |
| 「ローカルのアクセス間隔制御」 | 同じホストの予算待ちです。DataCiteは0.61秒、OpenAIREは60.1秒、Zenodoは2.01秒、RORは6.1秒、DOAJは0.51秒、npmは1秒、crates.ioは1.01秒、librariesioは1秒間隔。第6段階は osv 0.5秒（エンドポイント単位）、ietf / inspirehep / oeis / hfhub は1秒。第7段階は hn 0.4秒、swh 1秒。第8段階は ia 1秒（`archive.org/advancedsearch` を metadata API と別の予算に分けています）。待機せずエラーを返すため表示された秒数後に再試行 |
| OpenAIREを別プロセスでも使う | 間隔制御はプロセス内のみ。同一IPの別MCP/CLIを含め提供元の枠を共有するため、並行実行を避ける。今回のOpenAIRE認証枠の拡張は未実装 |
| 書誌はあるがgroundedの根拠がない | 抄録無しの追加ソースは本文を生成で補いません。別ソースを明示指定 |
| arXivの代替が起動しない | `fallback=true`、自然語検索、締切内、障害/遅延という条件を確認 |

動作確認と時間帯別計測:

```bash
python scripts/probe_knowledge_stdio.py
python scripts/probe_knowledge_stdio.py --sources datacite --datacite-kind dataset --query graphene
python scripts/probe_knowledge_stdio.py --sources zenodo ror --query CERN
python scripts/probe_knowledge_stdio.py --sources doaj --query "machine learning"
python scripts/probe_knowledge_stdio.py --sources npm --query "json schema validator"
python scripts/probe_knowledge_stdio.py --sources crates --query "async http client"
python scripts/probe_knowledge_stdio.py --sources osv --query jinja2
python scripts/probe_knowledge_stdio.py --sources ietf --query "RFC 9110"
python scripts/probe_knowledge_stdio.py --sources hn --query "rust ownership"
python scripts/probe_knowledge_stdio.py --sources swh --query kubernetes
python scripts/probe_knowledge_stdio.py --sources librariesio --query "json schema"
python scripts/probe_knowledge_stdio.py --sources ia --query "FORTRAN manual"
python scripts/probe_knowledge_stdio.py --sources oeis --query Fibonacci
python scripts/measure_kb.py --sources zenodo ror
python scripts/measure_kb.py --sources doaj npm crates
python scripts/measure_kb.py --sources datacite openaire
python scripts/measure_kb.py --sources osv ietf inspirehep oeis hfhub
python scripts/measure_kb.py --sources hn swh librariesio
python scripts/measure_kb.py --sources ia
python scripts/measure_kb.py --sources datacite --datacite-kind dataset
python scripts/measure_kb.py --report
python scripts/measure_relevance.py
python scripts/measure_relevance.py --report
```

`measure_relevance.py` は**関連性**（速さではなく中身）を測ります。固定のクエリ集合 ×「トップ 1 に含まれる
べき語」で的中を数え、`relevance.jsonl` に追記します。**所要時間の計測では見つからない種類の不具合**
（実測: ja.wikipedia は 0.6 秒で正常に見えていたが「大規模言語モデル」で無関係な記事を返していた）を
見つけるためのもので、上流の順位は変わるため**外れても失敗にはしません**。

計測はタイトル・本文を保存せず、時間・成否・件数・本文がある件数・モードだけを追記します。
既存の時刻別計測も続けられます。OpenAIREを含む別プロセスの連続実行は60秒以上空けてください。

利用条件・仕様: [DataCite](https://support.datacite.org/docs/rest-api) /
[OpenAIRE](https://graph.openaire.eu/docs/apis/terms) /
[Zenodo](https://about.zenodo.org/terms/)（非軍事用途のみ・ファイル条件は別） /
[ROR](https://ror.org/terms/)（IDs/metadataはCC0） /
[DOAJ](https://doaj.org/terms/)（記事メタデータCC0・レートは全ルート2req/s） /
[npm](https://www.npmjs.com/policies/open-source-terms)（Public APIsによる複製を明示許可） /
[crates.io](https://crates.io/policies)（Crawler Policy: 1req/s・識別UA必須） /
[CiNii Research](https://support.nii.ac.jp/sites/default/files/cinii/webapi-term.pdf)（**利用者自身のappidが必要**・利用目的は学術研究/非営利に限定・appidの貸与譲渡は禁止・2秒間隔に自制。§6.7） /
[OSV](https://osv.dev/docs/)（OpenSSF OSV スキーマの集約。各脆弱性DBの条件に従う） /
[IETF Datatracker](https://datatracker.ietf.org/api/)（公開の読み取り専用 API） /
[INSPIRE-HEP](https://inspirehep.net/)（CC0） /
[OEIS](https://oeis.org/wiki/The_OEIS_End-User_License_Agreement)（CC BY-SA 4.0・出典表示必須） /
[Hugging Face](https://huggingface.co/terms-of-service)（モデルごとの条件） /
[Hacker News Search（Algolia）](https://hn.algolia.com/api)（**帰属表示が必須**・10,000 req/h/IP） /
[Software Heritage API](https://www.softwareheritage.org/legal/api-terms-of-use/)（ポイントアクセス可・大量抽出不可） /
[Libraries.io](https://libraries.io/terms)（**利用者自身の無料APIキーが必要**・60 req/min。§6.10） /
[Internet Archive](https://archive.org/developers/bots.html)（読み取りはキー不要。`robots.txt` の Disallow は `/control/` と `/report/` のみ。AI エージェントを含む自動アクセスに説明的 User-Agent と間隔・429 の尊重を IA が明示。**日本語クエリは不可**。§6.11）。

リンク先全文・パッケージ本体の利用条件はメタデータの条件とは別です。

</details>

### ⏰ 遅いソースは締め切りで区切る

選択したソースを同時に引き、**8 秒以内に間に合った分**を返します。
遅れたソースは `⏱` / `timed_out` に残します。取得は裏で続き、**成功した結果だけ**が
同じサーバープロセス内の 30 分キャッシュに入ります。再起動するとキャッシュは消えます。
2 回目でも、取得が未完了・失敗・条件が違う場合は速くなるとは限りません。

実際の動作確認時の出力（`query="large language model"`、`lang="ja"`、`limit=1`。
2026-09-30。題名の行を省略）:

```text
出典 5 件（wikipedia, wikidata, arxiv, crossref, openalex, github）
  ✓ wikipedia (1 件・0.6 秒)
  ✓ wikidata (1 件・2.2 秒)
  ✓ arxiv (1 件・5.9 秒)
  ✓ crossref (1 件・1.0 秒)
  × openalex・0.2 秒: HTTP 429: {"error":"Rate limit exceeded","message":"Anonymous search is temporarily rate-l
  ✓ github (1 件・0.3 秒)
```

この回は締め切りによる脱落はありませんでした。**6 ソース中 5 ソースが成功**し、OpenAlex は制限で失敗しました。
Wikipedia は「データモデル」を返したので、取得成功でも問いとの関連性は別途確認が必要です。

## 🤖 自動的に使わせたいとき

**この手順は任意です。** 登録するだけで、毎回使われるわけではありません。
まず普通の会話やツール名指定で試し、必要なら判断規則を追加します。

```bash
python scripts/apply_proactive.py
```

既定は表示だけです。表示する判断規則の出典はこのスクリプトに一本化しています。
思考ノートを複雑な問題で使い、反論依頼は要所に絞り、簡単な質問や雑談では使わない方針です。

- `--write-snippet '<アクティブプロファイルの SOUL.md の絶対パス>'`：判断規則を追加・更新します。
  既存のマーカーブロックだけを差し替え、他の記述を保ちます。別プロファイルのファイルを指定しないでください。
- `--apply`：**Hermes の設定を書き換えます**。該当する競合サーバーの汎用ツールの除外と、
  自サーバーのハーネス目印を設定します。単なるテストではありません。先に表示内容を確認してください。
- `--check`：除外パターンがライブの実ツール名に一致するか確認します。
- `python scripts/measure_adoption.py --sessions 20`：保存済みセッションでの利用割合を調べます。
  依頼文から「必要場面」を近似判定し、**必要場面だけの採用率**と**見逃し**（必要なのに使わなかった場面）も出します。
  **判定は言い回しによる近似で、必要場面の完全な判定ではありません。**
- `python scripts/measure_adoption.py --min-needed-rate 0.5`：必要場面の採用率を下限としてゲートにできます。
  `--include-tool-free` を付けると、ツールを 1 つも呼ばなかったセッションも分母に入ります。

変更後は再起動して確認します。過去のツール名を含まない依頼 2 件では 2/2 の利用を観測しましたが、
少数の観測であり、すべての環境での自動利用を保証しません。
詳しい測定方法・競合除外の注意点は [docs/proactive-usage.md](docs/proactive-usage.md) を参照してください。

## 🤖 KANBAN のワーカーから使うとき

**KANBAN（Hermes のタスクボード）のワーカーにこのサーバーを使わせたい人向けです。**
普通の会話で使うだけなら読み飛ばしてください。以下の挙動はすべて実測に基づきます。

### 前提: ワーカーが使うプロファイルに登録されていること

ワーカーは**カードの担当プロファイル**として起動し、そのプロファイルの `mcp_servers` だけを見ます。
メインのプロファイルに登録してあっても、ワーカーのプロファイルに無ければツールは見えません。

```bash
# プロファイルごとに登録の有無を確認する
hermes -p default  config get mcp_servers.freeagent-bind
hermes -p reviewer config get mcp_servers.freeagent-bind
```

登録の仕方は [2. Hermes に登録する](#2-hermes-に登録する) と同じです。
`--clone-all` で作ったプロファイルなら、登録内容もそのまま複製されています。

### ワーカーが呼ぶ形（実測）

ワーカーは普通のセッションと同じ 11 ツールを呼べます。実測した最小の例:

```
freeagent_lookup(query="大規模言語モデル", sources=["wikipedia"], limit=3)
  → items 3 件 / llm_used=false / 先頭は「大規模言語モデル」(ja.wikipedia)

freeagent_think(prompt="…", thought_number=1, total_thoughts=3, card="t_a7558ce8")
  → session_id=t92171190316844fc45b9293 / 返り値の card が t_a7558ce8 と一致
```

- **`card` を渡すと思考台帳がカードに紐づきます。** ワーカーは環境変数 `HERMES_KANBAN_TASK` でカード ID を知りますが、
  **MCP の子プロセスにはその環境変数が渡りません**。サーバー側からカードを引くことはできないので、
  カード ID は**呼び出し側（ワーカー）が `card` 引数で渡します**。紐づけは
  `%LOCALAPPDATA%\hermes-freeagent-bind\thoughts.json` の `chains[<session_id>]["card"]` で後から目視でき、
  メインセッションがワーカーの思考を監査できます。
- **ワーカーはボードを書き換えられません。** MCP の子プロセスには書き込みフェンスが付き、ボードへの書き込みは拒否されます
  （読み取りは通ります）。だから「カードを更新する」ツールは作っていません。
- `freeagent_think` の `view=true` の応答に `steps` はありません。道筋は `ledger.latest`（本文）と
  `ledger.active_path`（番号の配列）、件数は `ledger.steps_recorded` です。

### 並列ワーカーとプロセスの数（ここが一番効きます）

**1 ワーカー = 1 プロセスではありません。** stdio で登録していると、ワーカーごとに MCP サーバーの子プロセスが 1 つ増えます。
ロックはプロセス内だけなので、`FREEAGENT_MAX_WORKERS` も合算されません。

> 上流への同時実行数 ≒ プロセス数 × Σ（提供元ごとの枠）= プロセス数 ×（`nous` 6 ＋ 他は 4 …）

**提供元が違えば枠は別々に数えます**（別ホストなので上流の制限も別）。同じ提供元へ集中する分だけが
枠で直列化されます。`FREEAGENT_MAX_WORKERS`（既定 8）は**スレッドの本数**で、上流への同時数は
提供元の枠が決めます。

並列ワーカーを回すなら [5. 複数クライアントで 1 プロセスを共有する（HTTP モード・任意）](#5-複数クライアントで-1-プロセスを共有するhttp-モード任意) に切り替えてください。
実測では、常駐 HTTP に切り替えた後は**ワーカーを回しても `freeagent_bind` のプロセスは 1 つも増えませんでした**
（8790 に接続しているのは gateway・`hermes serve`・各ワーカーで、知識キャッシュも共有されます）。
stdio のままだと並列数だけプロセスが増え、レート予算と台帳のロックが分散します。

### ワーカーに結果を残させるときの注意（実測）

- **`kanban_complete` の `metadata` は完了イベントに載りません。** 検証したい値はワーカーの **summary 本文**に書かせてください。
- **カード本文に「期待」と「満たさなければ `kanban_block` して実値を書く」を入れておく**と、ワーカーの summary が
  そのまま検証記録になります（実測で有効）。手順には具体的な引数（`thought_number=1`、`card="<カード ID>"` など）を書きます。

### 期待しないこと

- ワーカーの同時実行を**プロセスをまたいで調停することはありません**。枠（`FREEAGENT_PROVIDER_LIMITS`）は
  プロセス内のカウンタなので、プロセスを増やすと上流への合計はその分増えます。減らすときは
  HTTP モードで 1 プロセスに寄せるか、`FREEAGENT_PROVIDER_LIMITS` を下げてください。
- ワーカーの思考台帳は**メインセッションとは別**です。`card` を渡さないと、後からどのカードの思考か分からなくなります。
- **stdio でワーカーとメインを併用すると、台帳の書き込みはプロセス間で直列化**されます
  （ファイルロックを取るため、同じカードへ同時に書くと片方が少し待ちます）。
  台帳の**読み**は各プロセスが独立なので、更新された内容は次の呼び出しで反映されます。
  常駐 HTTP（1 プロセス共有）に切り替えていれば待ち時間はありません。

## 🛑 停止・再開する

```bash
# 停止（設定は残す）
hermes config set mcp_servers.freeagent-bind.enabled false
# 再開
hermes config set mcp_servers.freeagent-bind.enabled true
```

変更後に Hermes を再起動してください。**停止のためにキーを削除したり、わざと接続を壊したりする必要はありません。**
無効化するとサーバーは起動されず、ツールも登録されないため、このサーバーから失敗時の助言は返りません。
別の検索などへ切り替えるかはメイン LLM が判断します。

判断規則も不要になったら、アクティブプロファイルのファイルからこのブロックだけ外せます。

```bash
python scripts/apply_proactive.py --remove-snippet '<アクティブプロファイルの SOUL.md の絶対パス>'
```

保存済みの統計・セッションは無効化だけでは削除しません。
競合ツールの除外設定も自動では戻りません。`--apply` で除外した場合は、不要になった除外を別途見直してください。

## 💾 データの保存と外部送信

**「読み取り専用」は外部サイトやリポジトリを書き換えないという意味です。ローカルへの保存はあります。**

- 保存先は `FREEAGENT_STATE_DIR`。Windows の既定は `%LOCALAPPDATA%\hermes-freeagent-bind`。
  一時領域ではなく、残してよい場所にしてください。
- 品質統計・クールダウン・認証失敗の記憶・呼び出しメタデータを保存します。
- **相談セッションには問いや回答、思考ノートには検討内容が保存されます。**
  相談の保存期限は既定 1 時間、ノートは 2 時間。期限は取得時の判定で、時刻ぴったりのファイル消去は保証しません。
- 検索結果のキャッシュはメモリのみ。検索内容を知識データベースとして永続化する機能ではありません。
- 推論のプロンプトや根拠は選ばれたモデルの提供元へ、検索語は指定した検索サービスへ送ります。
  **機密・個人情報を渡す前に、提供元の利用条件と保存方針を確認してください。**
- `freeagent_delegate` はフルツール付き Hermes を別プロセスで起動する例外です。
  `FREEAGENT_ALLOW_AGENT=1` で許可する前に、作業の副作用を理解してください。
- `FREEAGENT_DEBUG_LOG` は stdio の送受信を記録する診断用です。問い・回答などが含まれるので、
  普段は無効のままにし、ログを公開する前に内容を確認してください。

## ⚙ 環境変数

<details>
<summary>上級者向け：環境変数の全一覧を開く</summary>

**通常は既定のままで使えます。** 変えるのは、各プロバイダのキーと `FREEAGENT_STATE_DIR`
（置き場を移したいとき）くらいです。

| 変数 | 既定 | 意味 |
|---|---|---|
| `FREEAGENT_STATE_DIR` | `%LOCALAPPDATA%\hermes-freeagent-bind` | 蓄積ストア（統計・クールダウン・相談セッション）。**一時領域に置かない** |
| `FREEAGENT_DEFAULT_MODEL` | 空（自動選抜） | 既定モデル（`provider/model`） |
| `FREEAGENT_MAX_WORKERS` | 8 | 推論の並列度＝同時に走らせる本数（1〜32）。**429 の抑止は提供元の枠が担う**（下の 3 行） |
| `FREEAGENT_MAX_SUB_MODELS` | 8 | `size` の上限（1 呼び出しで参加させるモデル数・1〜32） |
| `FREEAGENT_MAX_MODELS_PER_CALL` | 16 | `models` 配列・`models: ["all"]` の展開上限（1〜40） |
| `FREEAGENT_PROVIDER_LIMIT` | 4 | 提供元ごとの同時実行枠の既定（実測済みの `nous` は 6） |
| `FREEAGENT_PROVIDER_LIMITS` | 空 | 提供元別の枠を明示（例: `openrouter=2,huggingface=1`）。未知の名前は無視 |
| `FREEAGENT_GATE_WAIT_S` | 20 | 枠が空くのを待つ上限（秒・1〜120）。過ぎたら**脱落として返す** |
| `FREEAGENT_GATE_RESTORE_AFTER` | 3 | 連続成功がこの回数に達したら枠を 1 本戻す |
| `FREEAGENT_QUOTA_LIMITS` | 空 | 提供元の上限を**宣言**する（例: `openrouter:rpd=1000+nous:rpm=60`。単位は `rpm` / `rpd` / `tpm` / `tpd`）。宣言した提供元だけ使用量を記録し、超える前に止める |
| `FREEAGENT_QUOTA` | 1 | クォータ窓を有効にする（`0` で宣言を無視・何も記録しない） |
| `FREEAGENT_MAX_CALLS_PER_RUN` | 40 | fanout の組合せ上限（map 全体の制限ではありません） |
| `FREEAGENT_KB_DEADLINE` | 8 | 知識取得全体の締め切り（秒・1〜120）。遅れたソースは `⏱` で脱落表示 |
| `FREEAGENT_KB_LATENCY_PATH` | `kb_latency.jsonl` | `measure_kb.py` の記録先 |
| `FREEAGENT_RELEVANCE_PATH` | `relevance.jsonl` | `measure_relevance.py` の記録先 |
| `FREEAGENT_HARNESS` | 空 | 起動元の目印。Hermes から使うなら `hermes`（[Hermes 以外で使うとき](#-hermes-以外で使うとき)） |
| `FREEAGENT_HARNESS_WARN` | 1 | `0` で Hermes 以外のときの警告を止める（判定結果は `structuredContent.harness` に残る） |
| `FREEAGENT_RANK` | 1 | 品質統計による並べ替えを使う |
| `FREEAGENT_PROVIDER_ORDER` | `nous,openrouter,nvidia,huggingface,groq,cloudflare,gemini,vercel,ollama,zai,mistral,modelscope` | 一覧に出すプロバイダと優先順。オプション8社は契約確認変数が無い間は通信しない |
| `FREEAGENT_AUTH_TTL` | 900 | 認証失敗を覚えて自動選抜から外す時間（秒） |
| `FREEAGENT_PROBE_TIMEOUT` | 25.0 | 生存確認 1 件の読み取りタイムアウト（秒） |
| `FREEAGENT_PROBE_WORKERS` | 8 | 生存確認の並列度（1〜8） |
| `FREEAGENT_SESSIONS` | 1 | 相談セッションを永続化する |
| `FREEAGENT_SESSION_TTL` | 3600 | 相談セッションの寿命（秒） |
| `FREEAGENT_THOUGHTS` | 1 | 思考台帳（`freeagent_think`）を永続化する |
| `FREEAGENT_THOUGHT_TTL` / `_MAX` | 7200 / 32 | 台帳の寿命（秒）・保持する台帳の数 |
| `FREEAGENT_THOUGHT_MAX_STEPS` | 24 | 1 台帳あたりの思考数上限（超過は黙って捨てずエラー） |
| `FREEAGENT_THOUGHT_CHARS` | 2000 | 1 思考あたりに保存する文字数（超過は切り詰め） |
| `FREEAGENT_ALLOW_AGENT` | 0 | `freeagent_delegate`（Hermes 本体の起動）を許可 |
| `FREEAGENT_ARXIV_INTERVAL` | 3.0 | arXiv の最小呼び出し間隔（秒） |
| `FREEAGENT_CINII_APPID` | 空 | CiNii Research のアプリケーションID（**利用者自身が取得**）。未設定なら cinii は HTTP を出さず案内を返す（SPEC §6.7） |
| `FREEAGENT_EVIDENCE_ITEM_CHARS` / `_TOTAL_CHARS` | 360 / 3200 | 根拠本文をサブLLMへ注入する 1 件あたり・全体の上限（0 で本文を入れない） |
| `FREEAGENT_EMPTY_TOKEN_FLOOR` / `_CAP` | 512 / 2048 | 空応答時の予算引き上げ幅 |
| `FREEAGENT_USER_AGENT` | `hermes-freeagent-bind/0.1 (+…/hermes-freeagent-bind)` | 知識 API に名乗る UA（連絡先入りが望ましい。crates.io は識別可能な UA が必須） |
| `FREEAGENT_CONNECT_TIMEOUT` / `FREEAGENT_READ_TIMEOUT` | 10.0 / 180.0 | 外部 HTTP の (connect, read) タイムアウト（秒） |
| `FREEAGENT_KB_TTL` / `FREEAGENT_KB_TIMEOUT` | 1800.0 / 20.0 | 知識取得のキャッシュ TTL・読み取りタイムアウト（秒） |
| `FREEAGENT_KB_KEEPALIVE` / `FREEAGENT_KB_POOL_MAX` / `FREEAGENT_KB_POOL_IDLE` | 1 / 2 / 120.0 | wikidata の api.php で**接続を使い回す**（§5.2.1）。`FREEAGENT_KB_KEEPALIVE=0` または `FREEAGENT_KB_POOL_MAX=0` で無効化（共用経路へ委譲）。プールは origin ごとに最大 `POOL_MAX` 本・空きは `POOL_IDLE` 秒で捨てる（プロキシ環境では常に無効） |
| `FREEAGENT_KB_429_RETRIES` / `FREEAGENT_KB_429_MAX_WAIT` | 2 / 2.0 | 429 の再試行回数と `Retry-After` を尊重して待つ上限（秒）。crossref の 429 は共有プールのTransientな拒否で、**再試行で必ず通る**（実測） |
| `FREEAGENT_KB_RATE_WAIT_MAX` / `FREEAGENT_KB_RATE_WAIT_MARGIN` | 8.0（= `KB_DEADLINE`）/ 1.0 | ホスト予算の待ちを許す上限（秒）と、応答時間が残るよう確保する余白。待ちが上限を超えるときは待たずに 429 を返す |
| `FREEAGENT_CATALOG_DEADLINE` | 10.0 | モデル一覧を**並列に投げて打ち切る**までの締め切り（秒・1 以上）。間に合わなかったプロバイダは前回の成功値で埋める（SPEC §4.4） |
| `FREEAGENT_CATALOG_CACHE` | 1 | `0` でモデル一覧のプロセス間キャッシュ（`model_catalog.json`）を無効化する |
| `FREEAGENT_KB_INFLIGHT_WAIT_MAX` | 8.0（= `KB_DEADLINE`） | 同一キーの取得を待つ上限（秒）。超えたら待たずに自分で取得する（利用者を待たせない） |
| `FREEAGENT_CROSSREF_INTERVAL` | 0.1 | Crossref のローカル最小間隔（秒）。429 は間隔ではなく**再試行**で対処する（実測: 間隔では直らない） |
| `FREEAGENT_KB_HEDGE_DELAY` / `FREEAGENT_KB_HEDGE_MIN_REMAINING` | 0.6 / 0.05 | 代替取得（arXiv→DataCite）を始めるまでの待ちと、締め切り直前に始めないための下限（秒） |
| `FREEAGENT_EVIDENCE_ITEM_CHARS` / `FREEAGENT_EVIDENCE_TOTAL_CHARS` | 360 / 3200 | 根拠本文の 1 件あたり・全体の注入上限（文字） |
| `FREEAGENT_SESSION_MAX` | 64 | 相談セッションの保持数上限 |
| `FREEAGENT_STATS_HALFLIFE_DAYS` | 30 | 品質統計の減衰半減期（日） |
| `FREEAGENT_TRACE_MAX_BYTES` | 262144 | トレースファイルの最大サイズ（バイト） |
| `FREEAGENT_EMPTY_TOKEN_CAP` | 2048 | 空応答時の予算引き上げの上限（トークン） |
| `FREEAGENT_TRACE` / `FREEAGENT_STATS` / `FREEAGENT_COOLDOWN` | 1 | トレース・品質統計・クールダウンの記録 |
| `FREEAGENT_DEBUG_LOG` | 空 | 指定パスへ stdio の送受信を 1 行ずつ追記（クライアント互換の切り分け用） |
| `FREEAGENT_HERMES_BIN` | `hermes`（`which` で探索） | `freeagent_delegate` に使う実行ファイル |
| `FREEAGENT_TRANSPORT` | `stdio` | `http` で HTTP モード（1 プロセス共有）。`--transport` が優先 |
| `FREEAGENT_HTTP_HOST` / `FREEAGENT_HTTP_PORT` | `127.0.0.1` / `8765` | HTTP の待受（`--host` / `--port` が優先） |
| `FREEAGENT_HTTP_PATH` | `/mcp` | HTTP のパス（`--path` が優先） |
| `FREEAGENT_HTTP_TOKEN` | 空 | 設定したときだけ認証を要求（`Authorization: Bearer` か `x-freeagent-token`） |
| `FREEAGENT_HTTP_SESSION_TTL` | 1800 | HTTP セッションの寿命（秒）。失効後は 404 になり、クライアントは initialize からやり直す |
| `FREEAGENT_HTTP_KEEPALIVE` | 15.0 | SSE のキープアライブ間隔（秒） |
| `FREEAGENT_HTTP_MAX_BODY` | 4194304 | POST 本文の上限（バイト。超えると 413） |
| `FREEAGENT_HTTP_MAX_SESSIONS` | 64 | 同時セッション数の上限（超えたら古い順に落とす） |

**推論バックエンド（サブ LLM）のキー**

| 変数 | 既定 | 意味 |
|---|---|---|
| `OPENROUTER_API_KEY` | 空 | OpenRouter の推論。無料 `:free` SKU でも必須（一覧は未認証でも取れる） |
| `NVIDIA_API_KEY` | 空 | NVIDIA NIM の推論（一覧は未認証でも取れる。生存確認が必須） |
| `HF_TOKEN`（別名 `HUGGINGFACE_API_KEY` / `HUGGINGFACEHUB_API_TOKEN`） | 空 | Hugging Face の推論。**Inference Providers 権限**が必要 |
| `GROQ_API_KEY` | 空 | Groqの推論。Freeモデルallowlistは3 IDに固定。`FREEAGENT_GROQ_FREE_TIER=1`でFree tierを明示確認 |
| `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` | 空 | Workers AIの推論とモデル検索。Workers AI Read権限が必要。Free plan確認は`FREEAGENT_CLOUDFLARE_FREE_PLAN=1` |
| `GEMINI_API_KEY`（別名 `GOOGLE_API_KEY`） | 空 | Gemini Developer API。Free対象の2モデルのみ。Free tier確認とデータ利用同意を個別に設定 |
| `AI_GATEWAY_API_KEY` | 空 | Vercel AI Gatewayの推論。Free Tier対象の15モデルのみ許可。Free tier確認は`FREEAGENT_VERCEL_FREE_TIER=1` |
| `OLLAMA_API_KEY` | 空 | Ollama Cloudの推論。無料対象は非公開のため`probe`で確認。Free plan確認は`FREEAGENT_OLLAMA_FREE_PLAN=1` |
| `GLM_API_KEY`（別名 `ZAI_API_KEY` / `ZHIPUAI_API_KEY`） | 空 | Z.ai（GLM）の推論。Freeは`glm-4.7-flash` / `glm-4.5-flash` / `glm-4.6v-flash`の3 IDのみ。Free tier確認は`FREEAGENT_ZAI_FREE_TIER=1` |
| `MISTRAL_API_KEY` | 空 | Mistral La Plateformeの推論。Experiment planの確認は`FREEAGENT_MISTRAL_FREE_PLAN=1`。対象モデルは`probe`で確認 |
| `MODELSCOPE_API_KEY`（別名 `MODELSCOPE_API_TOKEN`） | 空 | ModelScope API-Inferenceの推論。無料枠の確認は`FREEAGENT_MODELSCOPE_FREE_PLAN=1`。対象モデルは`probe`で確認 |
| `FREEAGENT_GROQ_FREE_TIER` | 未設定（無効） | GroqアカウントがFree tierであることを明示確認。Developer tierは従量課金 |
| `FREEAGENT_CLOUDFLARE_FREE_PLAN` | 未設定（無効） | Workers Free planであることを明示確認。Workers Paidでは日次割当超過分が課金される |
| `FREEAGENT_GEMINI_FREE_TIER` | 未設定（無効） | GeminiプロジェクトのFree/unpaid tierを明示確認 |
| `FREEAGENT_GEMINI_UNPAID_DATA_ACK` | 未設定（無効） | Gemini Free tierの入力・出力がGoogleの製品改善や人手レビューに使われる可能性を理解した確認 |
| `FREEAGENT_VERCEL_FREE_TIER` | 未設定（無効） | Vercel AI GatewayがFree tier（クレジット未購入）であることを明示確認。クレジット購入でpaid tierに移る |
| `FREEAGENT_OLLAMA_FREE_PLAN` | 未設定（無効） | Ollama CloudがFree planであることを明示確認。クレジット購入で全モデルが課金対象になる |
| `FREEAGENT_ZAI_FREE_TIER` | 未設定（無効） | Z.aiがFree tierであることを明示確認。有料モデルは残高が無いと429を返す |
| `FREEAGENT_MISTRAL_FREE_PLAN` | 未設定（無効） | MistralのExperiment plan（無料枠）であることを明示確認 |
| `FREEAGENT_MODELSCOPE_FREE_PLAN` | 未設定（無効） | ModelScopeの無料枠であることを明示確認 |
| `FREEAGENT_API_KEY` | `proxy-attaches-real-credentials` | `nous` プロキシ用のダミー（実資格情報はプロキシが付与） |

**知識検索（検索ソース）のキー・連絡先**

| 変数 | 既定 | 意味 |
|---|---|---|
| `OPENALEX_API_KEY` | 空 | OpenAlex の検索。無いと匿名検索が停止されうる |
| `GITHUB_TOKEN` / `GH_TOKEN` | 空 | GitHub のレート制限緩和（コード検索は必須） |
| `FREEAGENT_MAILTO` | 空 | Crossref / OpenAlex / DataCite の polite pool 用メールアドレス（キーではない） |
| `FREEAGENT_LIBRARIESIO_KEY` | 空 | Libraries.io の検索。利用者自身の API キーが必須 |
| `FREEAGENT_QIITA_TOKEN` | 空 | Qiita の検索枠を 60 回/時 → 1,000 回/時に上げる（任意） |

**接続先の上書き（通常は触らない）**

| 変数 | 既定 | 意味 |
|---|---|---|
| `FREEAGENT_BASE_URL` | `http://127.0.0.1:8645/v1` | `nous` の接続先 |
| `FREEAGENT_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter の接続先 |
| `FREEAGENT_NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | NVIDIA NIM の接続先 |
| `FREEAGENT_HF_BASE_URL` | `https://router.huggingface.co/v1` | HF Inference Providers の接続先 |
| `FREEAGENT_GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Groqの接続先（通常は変更不要） |
| `FREEAGENT_GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai` | Gemini APIのOpenAI互換接続先（Beta） |
| `FREEAGENT_VERCEL_BASE_URL` | `https://ai-gateway.vercel.sh/v1` | Vercel AI GatewayのOpenAI互換接続先 |
| `FREEAGENT_OLLAMA_BASE_URL` | `https://ollama.com/v1` | Ollama CloudのOpenAI互換接続先 |
| `FREEAGENT_ZAI_BASE_URL` | `https://api.z.ai/api/paas/v4` | Z.aiのOpenAI互換接続先 |
| `FREEAGENT_MISTRAL_BASE_URL` | `https://api.mistral.ai/v1` | Mistral La Plateformeの接続先 |
| `FREEAGENT_MODELSCOPE_BASE_URL` | `https://api-inference.modelscope.cn/v1` | ModelScope API-Inferenceの接続先 |

**蓄積ストアのパス上書き**（既定は `FREEAGENT_STATE_DIR` 配下。**作業状態・統計が消えない場所**に置く）

| 変数 | 既定のファイル名 |
|---|---|
| `FREEAGENT_COOLDOWN_PATH` | `cooldowns.json`（404/410 は 1 時間、429 は `Retry-After`） |
| `FREEAGENT_AUTH_PATH` | `provider_auth.json`（プロバイダ単位の認証失敗・`FREEAGENT_AUTH_TTL` 秒） |
| `FREEAGENT_STATS_PATH` | `model_stats.json`（品質統計） |
| `FREEAGENT_TRACE_PATH` | `traces.jsonl`（トレース） |
| `FREEAGENT_SESSIONS_PATH` | `sessions.json`（相談セッション） |
| `FREEAGENT_THOUGHTS_PATH` | `thoughts.json`（思考台帳・TTL と上限つき。**知識は蓄積しない**） |

`model_catalog.json`（**モデル一覧のプロセス間キャッシュ**・SPEC §4.4）は `FREEAGENT_STATE_DIR` 直下に固定で、パスを上書きする変数はありません（`FREEAGENT_CATALOG_CACHE=0` で無効化）。

---

</details>

## 🧩 Hermes 以外で使うとき

Claude Code などの MCP クライアントからも stdio で使えます。ただし既定の `nous` は Hermes のプロキシが必要です。
Hermes がない環境では API キーを使う提供元を選びます（[必要なキー・条件](#-api-キーの設定)）。
`SOUL.md`・`apply_proactive.py` による自動利用設定は Hermes 専用です。

起動元は `FREEAGENT_HARNESS` とクライアントの名乗りから判定します。
Hermes 以外と判定しても止めずに警告し、判別不能なら stderr に案内を 1 行出します。
警告はプロセスごと・各チャネル 1 回。`FREEAGENT_HARNESS_WARN=0` で表示を止められます。
判定結果は `structuredContent.harness` やモデル一覧で確認できます。

## 🧪 検証・開発者向け情報

実装の契約・設計理由は [SPEC.md](SPEC.md)、変更時の手順は [AGENTS.md](AGENTS.md) にあります。
以下はリポジトリのルートで実行します。`env` の例は Bash / Git Bash 用です。

```bash
python -m compileall -q src/freeagent_bind
python scripts/check_integrity.py
python -m unittest discover -s tests
python scripts/smoke_stdio.py
python scripts/check_offline.py
python scripts/measure_adoption.py
python scripts/measure_kb.py --report
python scripts/apply_proactive.py
python scripts/apply_proactive.py --check
```

追加のネットワーク検証:

```bash
FREEAGENT_PROBE_NET=1 python scripts/smoke_stdio.py
# 以下はシェルの環境変数でキーを渡す。MCP env のキーは自動では使われません。
env -u PYTHONPATH PYTHONPATH=src python scripts/probe_providers.py
env -u PYTHONPATH PYTHONPATH=src python scripts/warmup_models.py --page 25
```

- `smoke_stdio.py` は接続・一覧・エラー経路の確認です。`FREEAGENT_PROBE_NET=1` でもモデル一覧取得までで、
  **終了コード 0 でも Free 候補が 0 の場合があります。モデルの実回答の保証ではありません。**
- `probe_providers.py` は推論を試しますがフォールバック可能です。要求モデルと実際に答えたモデルを確認してください。
- `warmup_models.py` は実推論を行い、品質統計・クールダウンなどを保存します。**無料枠を消費します。**
  終了コード 0 だけで判断せず、応答件数・スキップ・失敗を確認してください。
- シェルの環境と MCP 子プロセスの環境は別です。シェルでキー無しでも、MCP `env` 経由では推論できる場合があります。
- `env -u PYTHONPATH PYTHONPATH=src` は Hermes 側の import パス混入を避けるための指定です。

検索 API の時間帯別の計測（Windows の定期実行登録は任意）:

```bash
python scripts/measure_kb.py --report       # 既存記録を表示するだけ
python scripts/measure_kb.py                # 実際に検索して所要時間を記録
python scripts/measure_kb.py --schedule 24 # 1 時間ごとに 24 回測定するタスクを登録
python scripts/measure_kb.py --unschedule  # 登録したタスクを解除
```

記録は所要秒・成否・件数などで、本文は保存しません。Hermes の外で動くため MCP `env` は反映されません。

パッケージとしてインストールしたい場合だけ `pip install -e .` を使い、
`hermes-freeagent-bind` または `python -m freeagent_bind` で起動できます。

## 📄 ライセンス

MIT。データは各提供元（arXiv / Crossref / OpenAlex / Wikimedia / GitHub、明示指定の DataCite / OpenAIRE / Zenodo / ROR / DOAJ / npm / crates.io / CiNii / OSV / IETF / INSPIRE-HEP / OEIS / Hugging Face / Hacker News / Software Heritage / Libraries.io / Internet Archive）の条件に従って利用し、回答には出典を表示してください。対象は科学（物理・数学・計算機）とプログラミングまでで、医学・生物学は対象外です。