# SPEC — hermes-freeagent-bind 内部設計

このファイルは**実装の契約**を書く。README が「何ができるか」、SPEC が「どう作るか」と「なぜそうしたか」。
数値・挙動はすべて**実測に基づく**（推測で書いた箇所は「未検証」と明記する）。

## 0. 非目標

- 推論そのものを持たない（Hermes のプロキシ＝既存の Free モデルへ委譲する）。
- 知識の**蓄積**はしない（蓄積するのは品質統計・クールダウン・進行中の相談・進行中の思考台帳だけ）。
- `freeagent_delegate` を除き、外部データを書き換えない。ローカルの作業状態への書き込みと、推論プロンプト・検索語の外部送信はある（§2）。

## 1. 構成（モノリス）

`src/freeagent_bind/server.py` の 1 ファイル。肥大化を前提に **§区画**で分ける。区画の追加は「行を足す」
ではなく「新§を立てる」で行い、冒頭 docstring の目次も同時に更新する（目次が古いと全体が見えなくなる）。

| 区画 | 内容 |
|---|---|
| §0 | 定数・環境変数・プロバイダ仕様 |
| §1 | ユーティリティ（防御的変換・テキスト類似・原子書き込み） |
| §2 | 永続ストア（§2.1 クールダウン / §2.2 品質統計 / §2.3 トレース / §2.4 相談セッション / §2.5 プロバイダ認証の記憶 / §2.6 思考台帳） |
| §3 | プロバイダとモデル（Free 判定・解決・選抜・並列実行） |
| §4 | サブ LLM 呼び出し（フォールバック・空応答・CoT 検出） |
| §5 | 知識バックエンド9種（既定6＋§5.9 DataCite / §5.10明示許可代替 / §5.11ホスト予算 / §5.12 Europe PMC・OpenAIRE / §5.13引用統合） |
| §6 | ツール実装（11本、§6.11 本文の注入番号・引用認定） |
| §7 | ツール定義（`TOOLS` / `HANDLERS`） |
| §8 | 表示（`render`） |
| §8.5 / §8.6 | 失敗時の「次の一手」（`next_action`） / ハーネス判別（Hermes 以外で起動されたときの警告） |
| §9 | JSON-RPC 2.0 / stdio |

**依存は標準ライブラリのみ**。遅延 import するネイティブ拡張は、stdio 起動後に import すると
ツールが無応答になる環境があるため、必要なら起動前に import する（このサーバーは現状それを要しない）。

## 2. 状態ファイル（`FREEAGENT_STATE_DIR`）

| ファイル | 内容 | 消えてよいか |
|---|---|---|
| `cooldowns.json` | 429/404 で「いつまで使わない」か | 消えてよい（次の 429 で再記録） |
| `model_stats.json` | モデル別の成功・空応答・CoT 混入・切断・エラー観測（種類別） | 消えると品質順が初期化＝死んだモデルを選び直す |
| `traces.jsonl` | 1 行 1 呼び出しのメタデータ（**本文は残さない**。`answer_sha1` のみ） | 消えてよい |
| `sessions.json` | 進行中の相談（問い・ラウンド・メインの回答） | 消えると往復が切れる（TTL 1 時間） |
| `thoughts.json` | 進行中の思考台帳（思考・計画・分岐の状態・改訂の印・仮説の状態・見積りの履歴・検証結果・代替案） | 消えると思考の連鎖が切れる（TTL 2 時間・台帳 32 件・1 台帳 24 思考） |
| `provider_auth.json` | 認証で失敗したプロバイダ（15 分・自動選抜から外す） | 消えてよい（次の 403 で再記録） |

いずれも **tmp へ書いて `os.replace` で原子置換**し、`threading.Lock` で保護する。原子置換は
**書きかけの `<name>.<pid>.<tid>.tmp` を掃除してから**行う（実測: 途中で落ちた書きかけが状態
ディレクトリに残り、再起動のたびに増えた）。掃除は 60 秒より古いものだけを対象にする（並行して
書いている別スレッドの一時ファイルを消さないため。書き込みはロックの内側なので古い残骸は放棄済み）。**一時領域には
置かない**（統計とクールダウンが消えると挙動が巻き戻る）。`note_observation` はメモリ更新後に品質統計を
原子的に保存する。統計はモデル単位で半減期（既定 14 日）により成功・失敗カウンタを同じ比率で減衰し、
60 日より古いモデルと 400 件を超える古い履歴を prune する。保存スナップショットは Lock の内側で複製し、
並行書き込みによる辞書変更を防ぐ。**環境障害（`is_env_failure`）の経路ではどれも書かない**（§10 の 2）。
バックエンド全滅の検査では、推論を伴う呼び出しによってこのディレクトリに**新しいファイルが増えない**。
既存ファイルを削除する契約ではなく、推論を伴わない思考ノートの記録は保存できる。

## 3. 数値引数の契約

- `as_int(value, default, lo, hi)` / `as_float(value, default)` は**例外を出さない**。
- **非有限（inf / nan）は既定値へ落とす**。`"1e999"` → inf → `int(inf)` は OverflowError を投げる
  （実測でこの経路から例外が漏れた）。
- 文字列引数は**文字列だけ**を受ける。数値の `prompt` は呼び出し側の誤りとして空扱い＝エラーで返す
  （意味不明な推論を走らせるより、誤りを早く見せるほうが安全）。

## 4. モデル解決と選抜

### 4.1 プロバイダ（4 つ）

| プロバイダ | base_url | Free 判定 | 一覧の取得 | 実測（2026-09 時点） |
|---|---|---|---|---|
| `nous` | ローカルプロキシ | プロキシの申告 | 要プロキシ起動 | プロキシ停止中は接続エラー |
| `openrouter` | `openrouter.ai/api/v1` | `:free` または pricing が全部 0 | **未認証可** | 458 モデル / Free 21 / 生存 11 |
| `nvidia` | `integrate.api.nvidia.com/v1` | `free_kind="credit"`（全件が無料枠） | **未認証可** | 82 モデル / **55 件が 404=EOL** / 生存 15 |
| `huggingface` | `router.huggingface.co/v1` | **提供元単位**（`providers[].is_free` または pricing 0） | **未認証可** | 137 モデル / Free 3 / 生存 1（`:together` は Cloudflare Error 1010 で要再確認） |

- **HF はトップレベルに料金と文脈長を持たない**。`providers[]` の各要素が `pricing` / `context_length` /
  `is_free` / `status` を持つので、`status == "live"` かつ無料の提供元があるときだけ Free と判定する
  （停止中の提供元を数えると「無料で使える」と嘘をつく）。文脈長は提供元の最大値。

### 4.2 一覧は実態と乖離する（`probe`）

- `freeagent_models` の `probe: true` は候補を**実際に呼ぶ**（空応答時は予算を上げた再試行がある）。判定は 5 分類:
  **応答成功 → `alive`** / **404/410 → `gone`（除外）** / **401・認証署名を持つ 403 → `auth`（除外）** /
  **timeout・空応答 → `slow`（残す）** / **429・5xx・402・提供元/CDN の 403 → `error`（残す）**。
  **生きているが今は応えない**ものを永久に隠さないため。
- **403 を全部「権限なし」にしない**（`_is_auth_error` の署名一致で判定。401 だけは無条件で認証）。
  実測で 403 の出所は 3 つある: (a) トークン権限不足（`does not have sufficient permissions…`）＝
  プロバイダ記憶の対象、(b) モデル単位の提供元制限（OpenRouter の `:free`）、(c) **CDN のブロック**
  （HF の `:together` 経由が **Cloudflare Error 1010 "Access denied"**）。(b)(c) を認証失敗にすると
  **プロバイダ全体を 15 分止めて、生きている他モデルまで選抜から消える**。
- エラー文言に `HTTP 401/403` のような**曖昧な表記を書かない**。生存確認はエラー文字列の
  `http 401` を部分一致で見るため、403 の文言が 401 に誤マッチした（実測）。ステータスは実際の値を書く。
- 生存確認は **`allow_fallback=False`** で行う。フォールバックを有効にすると他プロバイダの応答が
  「生存」と誤判定する（実測: HF の 403 が OpenRouter の応答で隠れた）。
- 生存確認のタイムアウトは短く（既定 25 秒）。モデル既定の 180 秒だと 1 件の遅いモデルが探索全体を止める。
- 結果は通常のストアへ流れる（`gone` はクールダウン 1 時間、`auth` はプロバイダ記憶 15 分）。
  `scripts/warmup_models.py` が全プロバイダを一巡して定着させる。

### 4.3 選抜

- 参照は `provider/model`。Free 判定はプロバイダごとの規則（`:free` サフィックス / 価格 0 /
  プロバイダ固有のフラグ）で行い、**一覧を取得できたときだけ**判定する（取得失敗を「Free 0 件」と
  混同しない）。
- モデルは**消える前提**。呼び出しのたびに一覧から解決し、落ちたら代替へ回す。落とす条件は
  `_FALLBACK_STATUS = {402,400,401,403,404,410,429,500,502,503,504}`（`_MAX_ATTEMPTS = 4`）。
  `402`（クレジット枯渇）も**フォールバック対象**。入れないと代替へ回らず即エラーで止まる（実測: HF の月次無料枠が尽きたとき全モデルが 402 になり、パネルが「失敗」に見えた）。402 はキーや権限の問題ではないので**プロバイダ記憶には入れない**（翌月に回復する性質）。
- `select_models(size, requested, prefer, exclude)` の優先順は **明示 > prefer > 品質統計**。
  選んだ根拠（`available` / `status` / `notes`）を返す。
- **クールダウン中は除外せず後回し**（空きが足りないときだけ補充）。除外にすると選択肢が痩せ、
  後回しにしないと「選択直後に 429 → 全候補がクールダウン中 → 1 体へ縮退」が起きる（実測）。
- **選抜はプロバイダを巡回させる**（`diverse_order`）。品質観測が無いモデルは同点になり、素の順序だと
  モデル ID のアルファベット順で 1 プロバイダが枠を独占する（実測: 4 体選抜のうち 3 体が
  `huggingface/…`）。プロバイダ順は最良モデルの順位で決めるので品質順は捨てない。
- **認証で失敗中のプロバイダは自動選抜から外す**（`provider_auth.json`、既定 15 分）。明示 `requested`
  は常に試す＝キーを直せば即復帰する。除外は `notes` に出す（黙って隠さない）。

## 5. 呼び出し（§4）

- 外部 HTTP は **接続タイムアウトと読み取りタイムアウトを分けて設定**する（既定 10 秒 / 180 秒）。接続後は urllib の response socket を読み取りタイムアウトへ切り替える。環境障害（接続不可・timeout）は同じプロバイダ系列の fallback を打ち切り、全候補へ同じ不通を繰り返さない。
  - **実測の限界**: 切り替えは `urlopen` が**応答ヘッダを受け取った後**にしか効かない。ローカルプロキシは上流の生成が終わるまで本文もヘッダも返さないため（実測: TTFB 6.13 秒 = total 6.13 秒）、生成が `CONNECT_TIMEOUT`（10 秒）を超えると `TimeoutError` になり、読み取り 180 秒は使われない（実測: 1200 トークンの要求が 10.0 秒で timeout）。この失敗は `is_env_failure` に該当するため**統計にも残らない**。回避は `max_tokens` を小さく保つ・`FREEAGENT_CONNECT_TIMEOUT` を上げる（遮断ホストへの fail fast が鈍る）。根本対策は `http.client` で接続と読み取りを別々に設定すること（未着手・README の「既知の制約」に記載）。
- 429 は `Retry-After` を尊重してクールダウンへ記録。404/410 は長め（1 時間）、429 は既定 60 秒
  （上限 900 秒）。402（クレジット枯渇）はプロバイダ全体の認証障害として記憶せず、該当モデルだけ既定 60 秒間クールダウンする。
- **401/403 はプロバイダ単位で記憶**して自動選抜から外し、原因と直し方を `last_error` に残す
  （空のままだと「すべての候補で失敗しました」しか出ず直しようがない。実測: HF の権限不足がこの形で
  隠れた）。呼び出しが通れば記憶は消す。
- **空応答を成功として返さない**。思考トークンで予算を使い切るモデルがあり（実測: `max_tokens=220`
  で 3 体中 2 体が空）、空を回答として渡すとメイン LLM が無回答を回答と誤解する。予算を上げて
  （`min(max(max_tokens*3, 512), 2048)`）1 回だけ再試行し、なお空なら明示的なエラーにする。
- **並列呼び出しの予備候補は重複させない**（`_ModelClaims`）。`ask_many` 1 回の中で、各枠の本来のモデルと
  `avoid`（利用者の `exclude`、think の代替案では同じ呼び出しの検証者）を最初から「使用中」にし、フォールバックで
  新たに取るモデルはロック内で 1 回だけ確保する。取れなければその候補を飛ばし、尽きたら
  `avoided_duplicates` 付きのエラー（脱落）にする。同じモデルで 2 枠を埋めて「独立 2 体」に見せない
  （実測: 代替案の提案者の枠を、同じ呼び出しの検証者と同じモデルが埋めた）。対象は panel / consult / 討論 /
  grounded / think。`ask_map`（fanout / map）は枠ごとに依頼が違い独立性を主張しないので対象外。
- **CoT 混入はマーカー方式**で検出する（改行数では判定しない。ラベル付き複数行出力を誤判定した
  実測がある）。用途は統計の減点のみで、回答を捨てる理由にはしない。

## 6. 知識バックエンド（§5）

| ソース | エンドポイント | 認証 | 既知の失敗モード |
|---|---|---|---|
| wikipedia | `{lang}.wikipedia.org/w/api.php` | 不要 | 記事名の揺れ（検索 API で吸収） |
| wikidata | `www.wikidata.org/w/api.php` | 不要 | ラベル欠落（`label` は `title` ではない） |
| arxiv | `https://export.arxiv.org/api/query` | 不要 | **http は 301 の先で 406**。連続アクセスで 406 → **3 秒間隔で直列化** |
| crossref | `api.crossref.org/works` | 不要 | `mailto` 未設定だと polite pool に入れない |
| openalex | `api.openalex.org/works` | **常用では無料APIキーを推奨（匿名基本利用も可）** | 匿名検索は提供元が制限中（実測 `503 Anonymous search is paused` / `429 Rate limit exceeded`） |
| github | `api.github.com/search/*` | トークン推奨（コード検索は必須） | 未認証は 10 リクエスト/分 |
| datacite | `api.datacite.org/dois` | 不要、mailto任意 | モード間でホスト予算共有。抄録・登録反映の欠落あり |
| openaire | `api.openaire.eu/graph/v3/research-products` | 今回は匿名のみ | 公式60/h、メタデータCC-BY表示、本文欠落あり |
| europepmc | `www.ebi.ac.uk/europepmc/webservices/rest/search` | 不要 | core検索、全文利用条件は別、生命科学系 |
| zenodo | `zenodo.org/api/records/` | 公開検索は匿名 | 説明メタデータCC0（メール除外）、非軍事用途、ファイル条件は別 |
| ror | `api.ror.org/v2/organizations` | 今回は匿名 | 機関属性CC0、検索候補であって機関同定の確定ではない |

- 結果は**必ず `citation`（source / title / url / year / summary）に正規化**する。表示も注入もこの形だけを使う。
- 取得はメモリ TTL キャッシュ + ホスト単位の遮断記憶。**同一キーは single-flight** で同時取得を 1 回にまとめ、エラー応答はキャッシュしない（回復後の再取得を妨げない）。キャッシュ値は複製して返し、呼び出し側の変更が共有状態に伝播しない。**LLM を介さない**（＝幻覚が入らない経路）。
- Wikipedia の言語コードはホスト名へ埋め込む前に検証し、許可形式以外はネットワークへ送らない。検索結果と要約は `generator=search` + `extracts` の 1 API 呼び出しで取得する。
- **citation には本文（`summary`）を必ず入れる**。実測: 全バックエンドで `summary` が空のまま返っており、
  `grounded` が注入する【根拠】がタイトルと URL だけになっていた＝サブLLMは根拠を読めず、記憶で答えて
  `[n]` を飾りで付けていた。Crossref の `abstract`（JATS タグ除去）と OpenAlex の
  `abstract_inverted_index`（語→位置の配列を復元）も本文として使う。
- 注入する本文は 1 件あたり `FREEAGENT_EVIDENCE_ITEM_CHARS`（既定 360）、全体で
  `FREEAGENT_EVIDENCE_TOTAL_CHARS`（既定 3200）に収める。入れすぎると小型 Free モデルが予算を
  使い切って空応答・切断になる（逆効果）。
- `sources`省略時は`DEFAULT_SOURCES`（従来6種）だけ。追加5種は明示指定。重複指定を除き、無効名だけなら全ソースへ送らない。
- `fallback=true`のJSON真偽値だけが、自然語arXiv検索をDataCiteへ追加送信する明示許可。文字列`"true"`等は許可にしない。
- 1 ソースの失敗で全体を落とさない（`errors` に集約し、成功分だけ返す）。

### 6.1 締め切りつきの並列取得（§5.8）

- `knowledge_lookup` は**ソースごとに専用スレッド**を立て、全体の締め切り `KB_DEADLINE`（既定 8 秒・
  `FREEAGENT_KB_DEADLINE`・1〜120）まで待つ。旧実装は `run_parallel` で全ソース完了を待ち、並列数も
  `min(ソース数, MAX_WORKERS=4)` だったため、6 ソースでは 2 つが待ち行列に入っていた（コードで確認）。
  ソースはすべて別ホストなので、同時に引いてもホスト単位のリクエスト数は増えない。
- 締め切りに間に合わなかったソースは `{"timed_out": true, "error": …}` の結果にし、`errors` と
  トップレベルの `timed_out`（ソース名の配列）に載せる。**黙って消さない**。
- 締め切り後も裏の取得は `KB_TIMEOUT` まで続く（daemon スレッド）。各ソースは `_kb_cached` を通るので、
  完了すれば同じ問いの次回はキャッシュから即座に返る。締め切りは待ち時間の上限であって、取得の中止ではない。
- `timings`（ソース → 所要秒。締め切り超えは `null`）と `deadline_s` を常に返す。表示は `✓ src (n 件・x.x 秒)` /
  `⏱ src`。知識取得は状態ファイルを書かない（規約 21 の検査対象外にならない）。
- 平常時の実測（2026-09-30 01:59〜02:0x）: 6 ソースとも 0.3〜4.5 秒、全体 1.9 秒。同時間帯に OpenAlex の
  匿名検索が 429（`Anonymous search is temporarily rate-limited`）。候補の実測（J-STAGE 0.07〜0.44 秒・
  CiNii Research 0.1〜0.3 秒・Stack Exchange 0.2〜0.4 秒・OpenAlex の arXiv 絞り込み 0.8〜2.5 秒ほか。
  Semantic Scholar は匿名で 3/3 が 429、dblp はボット判定の HTML を HTTP 200 で返す）は追加の判断材料として
  残し、追加は時間帯別の計測結果を見てから決める。新規5種は当面明示指定のみ（§6.3/§6.4）。

### 6.2 時間帯別の計測（`scripts/measure_kb.py`）

- 1 巡で全ソースを並列に 1 回ずつ引き、1 ソース 1 行を `FREEAGENT_KB_LATENCY_PATH`（既定
  `FREEAGENT_STATE_DIR/kb_latency.jsonl`・最大 20,000 行・超えたら古い行から tmp + `os.replace` で捨てる）へ追記。
  記録は `ts` / `hour`（日本時間）/ `source` / `elapsed_s` / `ok` / `items` / `error`（160 字）/ `mailto` /
  `openalex_key` / `env_failure` に加え `datacite_kind` / `summaries`（本文がある件数）を記録し、**タイトル・本文は残さない**。
  `--sources`で対象、`--datacite-kind`でモードを指定。省略時は対応11ソースを測る。RORだけは機関名の6問を巡回し、論文キーワードの空振りと混同しない。巡回ごとにキャッシュとホスト遮断記憶を消すが、プロセス内レート制御は消さない。
- 全ソースが接続系の失敗なら `env_failure=true`（こちらのネットワーク障害）とし、`--report` の集計から外す。
- `--schedule N` は Windows のタスク（`pythonw`＝窓を出さない）を 1 時間おきに登録し、残り回数を
  `kb_latency.jsonl.schedule.json` で数えて、最後の 1 回でタスクを自分で消す（`/ED` `/ET` は HOURLY との
  組み合わせで意味が曖昧なので使わない）。`schtasks` の出力はコンソールのコードページ（`oem`）で読む。

### 6.3 任意ソースと代替の契約（§5.9〜§5.13）

- DataCiteは`datacite_kind=all/arxiv/dataset`。自然語の各語をJSON引用符でエスケープしAND結合、`sort=relevance`を指定。
  arxivは`client-id=arxiv.content`（旧資料の型がTextでも落とさない）、datasetは`resource-type-id=dataset`。
  Abstract種別の実本文だけを採用。キャッシュキーはモード・検索語・件数。年/本文の欠落もキーとして返す。
- Europe PMCは`resultType=core`・JSON・`pageSize`。実抄録、元source/id、プレプリント種別・原文ライセンスを保持。
  OpenAIREはGraph V3の`search`・publication限定。DOI/instance URL/idをURLへ正規化、descriptionsを本文にする。
  メタデータのOpenAIRE CC-BYクレジットと原文licenseを区別。今回は匿名のみで認証枠の拡張は未実装。
- 新規3種は`_kb_new_cached`→既存single-flight/cache。上流の不正型・解析例外はsource付きerror。
  空/不正配列を成功扱いせず、失敗はキャッシュしない。本文が無い正常書誌は`metadata_only=true`。
  lookupには表示するが、既存ソースを含め本文なしの引用は`_kb_has_evidence`でgrounded/evidence注入・agent番号登録から除外する。
  agentの書誌は`bibliography`に分離する。生成で本文を埋めない。
- `_kb_rate_acquire`はホスト単位にLock内で採番。待機・未来の予約をせず、アクセスできない場合はローカルHTTP429相当を返す。
  DataCite0.61秒、Europe PMC1秒、OpenAIRE60.1秒。DataCiteのモードは同じ予算。
  メモリ内・1プロセスの制御であり、他プロセス/IP上の他アプリとは共有しない。提供元のRetry-After/遮断記憶も従来通り。
- 明示許可代替は`_kb_source_result`。arXivの429/5xx/接続系/遮断なら直ちに、応答待ちなら`KB_HEDGE_DELAY`（既定2秒）後にDataCiteへ。
  設定は`FREEAGENT_KB_HEDGE_DELAY`（非有限は2へ、0.05〜30秒）。全体締切は延長しない。
  `_kb_gather`が同じ`deadline_at`を渡し、呼び出し元と代替worker内の取得開始直前の両方で期限を確認する。
  スレッド開始がスケジューリングで遅れた場合も新規代替を抑止する。既存取得は裏で継続。
  colon/引用符/括弧/AND・OR・NOT/新旧形式のarXiv ID等は互換不可として代替しない。「該当なし」や即時認証/不正要求も代替しない。
- 代替で`sources`（要求先）は変えない。`results.arxiv.source`と`citations[].source`は実取得元datacite。
  `fallback.requested_source/served_by/primary_error`で切替と主系の状態を明示。代替失敗は`fallback_attempt.error`にも残す。
- 引用はDOI（case-insensitive、版suffixは保持）とURL完全一致の両別名でグループ化する。DOI欠落側もURLで統合し、
  全入力のDOI/URL別名を連結成分として先に索引化し、URL-onlyの連鎖で複数DOIに到達する曖昧性も判定する。
  未知同士は共通URLで未知グループとして統合できるが、明示DOIの異なる版同士はURL一致だけで統合しない。
  複数DOIに結びつく曖昧なURL-only引用は入力順によらず割り当てない。本文選択と独立に確定DOIと`aliases`を保持し再統合できる。
  長い本文を残し`providers`に取得元一覧、`summary_source`に選択本文の取得元、`provider_metadata`に提供元別license/種別、
  `attributions`に全クレジットを保持。本文がない統合引用のmetadata_onlyを再計算する。
  キャッシュの辞書を変更せずコピーする。配信元数を独立した事実の証明にしない。本文の意味的同一性は未検証。
- `probe_knowledge_stdio.py`は実APIをstdio経由で検査。モデル推論は行わず、両チャネル・入力スキーマ・各ソースの結果・引用形を検証。
  `validate_lookup`で引用値・HTTP(S) URL・実取得元・代替許可も検査する。OpenAIREのdescriptions/instances.urlsは配列型を検証し、
  辞書キーや文字列各文字を本文/URLにしない。新規ソースの全引用URLも共通境界で検査する。
  `--sources`・`--datacite-kind`・`--fallback`で経路を選べる。MCP再起動前でも新しい子プロセスの実装を検証できる。
### 6.4 第3段階の公開メタデータ（§5.14/§5.15）

- `zenodo`と`ror`は`SOURCES`/`KB_BACKENDS`へ追加するが`DEFAULT_SOURCES`は変更しない。lookup/grounded/agentは既存経路で明示選択できる。
- Zenodoは公開`/api/records/`の`q`（自然語各語を引用してAND）・`sort=bestmatch`・`size<=10`。
  `hits.hits`を正規化し、`description`と`notes`のテキストを根拠として使う。`summary_kind=metadata_description`。
  HTMLParserでscript/style/template/noscript/headとコメントを本文から除外し、文字参照を復元する。
  メール欄をコピーせず、引用符付きローカル部・アドレスリテラル等を含むメールらしい文字列を削除する。残る`@`は値全体を省略する。
  省略の代替ラベルを本文として注入せず、実テキストがなければmetadata_onlyとする。ファイル/全文/制限付きコンテンツは取得しない。
  説明メタデータの`license=CC0-1.0`と`file_license`/`access_right`を分離し、版のDOIを使う（concept DOIで潰さない）。
- RORはv2の`query`で機関候補を探す。`ror_display`の名称とROR IDのCrockford文字集合・末尾数字・公式MOD 97-10チェックサムを確認し、提供された種別・国・都市・状態・設立年だけを整形する。
  `summary_kind=structured_metadata`は構造化属性の根拠で論文抄録ではない。設立年は`established`、出版年`year`は空。
  複数候補を返し、順位から同定しない（`search_candidates=true`）。属性がなければ`metadata_only=true`で本文番号は付けない。
- 共通のコピー付きsingle-flight/失敗非キャッシュ/HTTP遮断記憶/8秒gatherを使い、各ソース1リクエストだけ。
  Zenodo 2.01秒（公式検索30/min）、ROR 6.1秒（将来の匿名50/5minを考慮）のプロセス内ホスト予算。
  ROR client ID登録は現時点で一時停止・識別有無の制限は未導入。登録必須と断定せず、利用条件の変更を再確認する。
- 引用統合の`provider_metadata`は`file_license`/`access_right`/`summary_kind`も保持し、長い別ソース本文を選んでもライセンスを混同しない。
  stdio probeは根拠種別・CC0メタデータ・ROR年・Zenodoファイル条件のキーを検査する。
- 元候補は保留: J-STAGE（商用承認・主コンテンツ・24時間保存等）、Stack Exchange（現行AUPのAI開発/テスト向け自動取得に事前書面許諾）、
  CiNii（登録appid・用途承認・抄録権利）、CORE（検索/API組込み相談）。匿名200/CCライセンス/既定offは許諾の代わりにしない。
- 条件: ROR IDs/metadata CC0 https://ror.org/terms/ 、Zenodo非軍事用途/メタデータCC0（メール例外） https://about.zenodo.org/terms/ https://about.zenodo.org/policies/ 。
  原典ファイルの条件は別。ソース間の登録範囲・検索順位・内容の同等性や24時間の応答安定性は保証しない。

## 7. ツールの規約（§6–§8）

- 全ツールが `content`（人間向け・日本語）と `structuredContent`（LLM 向け純粋 JSON）を返す。
- **例外を外へ漏らさない**。失敗は `structuredContent.error`。`handle_tool_call` が最後の砦として
  `Exception` を捕まえ、`internal error:` に変換する。render / error_advice も個別に保護し、表示や助言の失敗で JSON-RPC 応答が消えないようにする。
- `description` は**モデルが読む唯一の窓口**なので、先頭に【使う条件】【使わない条件】【競合より優先】
  を置く（これが無いと `delegate_task` や `deliberation` が選ばれる）。
- **`content` に LLM 向けの指示文を書かない**（人間が読むチャネル。指示は description と docstring に置く）。
- 合意度は**表層の類似度**であって正しさの確率ではない。返り値にもその旨を書く。`freeagent_panel` の合意度・平均確信度・consensus は `served_by` で実モデルを重複排除し、`independent_sources` に独立ソース数を返す。fallback で同じ実モデルが複数枠を埋めても複数票にしない。
- `「結論/確信度/メインに確認したい点」`の解析は**1 行 1 ラベルと決め打たない**（実測: 「結論: … 確信度:
  88」のように 1 行に複数ラベルが来て確信度を取りこぼした）。取れない項目は推測せず空にする。
- ラベル値に同じ行の次ラベルを混ぜない。整数の `1` は確信度 1、小数の `0.8` は 80 に換算し、「なし。」等の句読点付き否定は問い返しに数えない。
- `freeagent_consult` の deep 討論では討論ラウンドの立場を最終結論として合意度を計算し、初回ラウンドのメインへの質問を保持する。同じ質問は一つにまとめ、「未解決: なし」は未解決意見に数えない。
- 代替モデルへ fallback するとき、指定モデルがクールダウン中でも残り候補を試し、認証失敗中プロバイダは代替先から外す。
- セッション ID は同一秒・同一プロセス内でも衝突しない乱数成分を持つ。
- サブエージェント（`freeagent_agent`）は根拠に**通し番号**を振り、ツール結果を番号つきの本文で注入する。
  回答中の `[n]` は番号と照合し、根拠に無い番号は `unsupported_citations`、有効な引用がない回答は
  `cited_ok: false` として返す。`freeagent_grounded` の `cited_ok` も有効な番号の有無だけであり、
  **本文と根拠の意味的な整合性は検証しない**。groundedも`unsupported_citations`を返し、
  有効番号と無効番号が混在しても、本文注入済みの有効番号があれば`cited_ok: true`になる。
- サブエージェント（`freeagent_agent`）は**最終ステップでツールを封じ、回答を要求する**。封じないと
  全ステップを調査に使い、回答が永久に出ない（実測）。到達しなかった場合は推測で埋めず、
  `steps_exhausted` と収集済みの根拠を返す。

### 7.1 本文予算と引用認定（§6.11）

- `_evidence_window`は実際に本文を注入した`numbers`とテキストを同時に作る。本文予算がない項目には見出しも番号も注入しない。
- groundedは`injected_citations`に対して引用を照合し、`not_injected_citations`と`evidence_citation_count`を返す。予算0なら推論を呼ばない。
- agentは取得本文候補のregistryと注入済み番号の集合を分離し、未注入候補は次のlookup時に同じ番号で再注入できる。
  認定は注入済み集合だけ。番号登録とagent結果集約は`_kb_citation_key`（provider＋DOI優先、DOIなし時だけURL）を共用し版を落とさない。
  返すregistryを先頭8件に切らず、返却番号と出典配列の対応を保つ。
- `_cited_numbers(..., allowed=...)`で存在するだけの出典や予算切れ番号を成功にしない。書誌は別の`bibliography`に置く。

## 7.5 思考台帳（`freeagent_think`・§2.6 / §6.9）

分解 → 修正 → 分岐 → 仮説検証を 1 ステップずつ積む。**思考の中身はメインが書く**ので、このツールの
付加価値は「積んだ思考を忘れない（文脈圧縮・再起動をまたぐ）」ことと「生成者以外のモデルに反証させる」
ことの 2 点だけである（サーバーはメインのモデル ID を照合しないため、メインとの重複は `exclude` 等で利用者側も回避する。思考メモ帳系ツールの移植では知能は増えない。実測: 台帳のみの 1 ステップは
**0.0 秒・サブ呼び出し 0 回**）。

- **検証は opt-in**（`verify=true`）。既定は台帳のみでネットワークに触れない。全ステップに検証を付けると
  1 ターンが分単位になる（実測: 4 体パネルで 14〜20 秒、多段討議で 3〜7 分）。
- 検証者は**生成者と別モデル**（メインが書いた思考をサブが反証する）で、役割は同意ではなく**反証の探索**
  （`判定` / `反証` / `見落とし` / `確信度` の 4 行）。判定は `妥当` / `要修正` / `根拠不足` の 3 値で、
  「なし」は反証として数えない。
- **同じ検証者を繰り返さない**: 台帳が検証済みモデルを覚え、次のステップの自動選抜から除外する
  （同じモデルに固定すると、独立した目が 1 つしかない状態に戻る）。
- **環境障害では書かない**（規約 21）。`verify=true` で全検証者が環境障害で落ちた場合、その思考は台帳に
  **記録せず**エラーを返す（検証されていない前提の上に次の思考を積まない）。モデル側の失敗（429 など）は
  従来どおり記録し、`verification.failed_rows` と `answered: 0` で隠さず返す。
- **採番はロック内**で行う（`thought_merge`）。読みと書きを分けると、並列に呼ばれた 2 つの思考が同じ番号を
  採番して片方が上書きで消える（規約 14）。`thought_number` を省略したときだけ自動採番する。
- 上限は `THOUGHT_MAX_STEPS`（既定 24）。**黙って捨てずエラーで返す**。同じ番号の再送は置き換える。
- **思考の総数（`total_thoughts`）は見積り**で、進めるうちに増減してよい（動的調整）。台帳は直近の見積りを
  保持し、記録数がその値に達したら `suggestions` で増減を促す（調整するのはメインで、台帳は判断しない）。
- 表示（`content`）は人間向けの事実だけ（思考の末尾・計画の進捗・分岐の状態・仮説の状態・判定の内訳・反証・
  代替案）。次の一手の助言は `structuredContent.suggestions` に置く（`content` に LLM 向け指示を書かない）。

### 7.5.1 構造（§2.7 / §6.10）

台帳は思考の列に加えて**構造**を持つ。適用は `thought_merge` のロック内（`_thought_apply_ops`）、引数の検証は
**サブ呼び出しの前**（`_think_structure`）に行う。

| 構造 | 引数 | 保存先 | 規則 |
|---|---|---|---|
| 分解 | `plan[]` / `subgoal` / `subgoal_done` | 台帳メタ `plan`（`id` / `text` / `done_at`） | 最大 `THOUGHT_PLAN_MAX`=12（超過はエラー）。再送は計画の改訂で、**同じ文面の項目は達成済みを引き継ぐ**。`subgoal` は計画の範囲内のみ |
| 改訂 | `revises_thought`（`is_revision` 単独はエラー） | 元ステップの `superseded_by` | 元は**消さない**。対象は既存かつ自分以外。改訂の改訂は `notes` に出して許す |
| 分岐 | `branch_from_thought` / `branch_id` | 台帳メタ `branch_meta`（`from` / `status` / `opened_at` / `resolved_at`） | 新しい分岐は分岐元が必須。既存の分岐は分岐元を引き継ぐ（食い違う指定は `notes`）。`branch_id` 省略時のみ `b<k>` を自動割当（`notes` に出す） |
| 決着 | `resolve_branch` + `branch_status` | `branch_meta[bid].status` | 片方だけはエラー。`abandoned` の分岐は `active_path` から外れる。決着済みの分岐への追記は状態を変えず `notes` |
| 仮説 | `kind=hypothesis` | ステップの `hypothesis_status`（初期 `open`） | 同じ番号で書き直しても `hypothesis_status` / `tested_by` は失わない |
| 仮説の検証 | `tests_hypothesis` + `hypothesis_status` | 仮説側の `tested_by` / `hypothesis_status` | 対象は `kind=hypothesis` のみ。検証ステップ自身は `kind=test`（既定で推定） |
| 見積り総数 | `total_thoughts` | ステップの `total_thoughts`、台帳メタ `total_history` | 省略時は台帳の値を引き継ぐ。**番号が見積りを超えたら番号まで引き上げ**、`total_auto_adjusted` と `notes` に出す |
| 代替案 | `propose_alternatives` | ステップの `alternatives` | 検証者とも別のモデル（台帳の使用済みモデルを除外）。`THINK_ALT_SYSTEM` で「代替: …」を最大 3 行。フォールバックも検証者に落とさない（`avoid`）。`truncated`（上限で打ち切り）なら**最終行の案を捨てる**（文の途中で切れた案を完全な案として残さない。実測「代替: 親プロセスのコマン」）。既定の上限は 400 のまま（上げると proxy 経由で 10 秒の接続タイムアウトを超えやすい。§5）。全員が環境障害なら**書かない** |
| 閲覧 | `view=true` + `session_id` | — | 何も書かない。サブも呼ばない（`verify` が付いていても） |

- **推測で繋がない**: 参照先が無い操作はエラーで返し（`known_thoughts` / `known_branches` を添える）、
  台帳を書かない。誤った番号のまま積むと以後の `active_path` が静かに壊れる。
- 検証者・提案者のプロンプトは**現行の道筋**（`superseded_by` 無し・`abandoned` 分岐以外）だけを載せ、
  改訂なら改訂前の文、仮説の検証なら対象の仮説を添える。印は統合時＝検証の後に付くため、**今回の
  呼び出しで改訂する元の思考・決着させる分岐も**プロンプト側で先に反映する（`_think_preview_row`）。
- 並列呼び出しで `branch_id` を省略した新しい分岐が同時に来ると、自動割当が同じ ID になり得る
  （割当はロック外）。明示の `branch_id` を推奨する。

## 7.6 利用者向け説明と実装の境界

README は利用開始・結果の読み方・障害時の対処を先に置き、実装上の設計理由はこの SPEC に置く。
次の既存挙動を、保証されている機能として過大に説明しない。

- `provider_ready` / 一覧の `usable` は資格情報の存在などの判定で、キーの有効性を検証しない。
  `usable_now` は ready な Free 候補からクールダウン中を除いた数であり、未検証モデル・認証失敗記憶中の
  候補も含みうる。自動選抜は別途認証記憶を考慮する。「実応答できるモデル数」と同一視しない。
- `tool_map` は `items[:64]`、`tool_fanout` は `prompts[:16]` で入力を切り詰める。
  `MAX_CALLS_PER_RUN` は fanout の組合せ上限であり、map 全体に適用される上限ではない。
  上限を超える入力は利用者側で分割する。現在の切り詰めに追加警告はない。
- 知識検索は LLM による生成を介さないが、提供元の誤り・古さ・検索の関連性不足は防げない。
  `summary` は導入部・説明・取得可能なアブストラクトなどであり、原典の全文とは限らない。
  締め切り後の結果が次回すぐ返るのは、同じプロセス・同じ検索条件で取得が成功しキャッシュに入った場合のみ。
- `enabled=false` ではサーバーは起動されず、本サーバーの `next_action` は返らない。
  代替へ切り替える判断はホスト側の規則・LLM に依存する。停止方法としてキー削除・故意の接続断は推奨しない。
- MCP `env` とシェルの環境は別で、単体スクリプトは MCP `env` を自動で読み込まない。
  `smoke_stdio.py` のネットワークモードは一覧までの検査で、実推論の成功を検査しない。
  `warmup_models.py` の終了コード 0 も生存モデルが 1 件以上ある保証ではない。
- `measure_adoption.py` の割合は対象セッション全体での利用割合で、必要場面に限定した自発率ではない。
- 設定は自動監視しない。反映は Hermes の再起動を案内する。最近の Hermes の `/reload-mcp` は
  [公式設定リファレンス](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference#reloading-config) に案内がある。
- キーの説明はサーバー自身の `.env` 自動読み込み（なし）と、Hermes の秘密情報参照（クライアント機能）を区別する。
  `${VAR}` の参照例は Bash / Git Bash のシングルクォートで示し、未設定参照は資格情報の用意にならないと明記する。
- 保存期限は取得時などに判定するもので、指定時刻ぴったりのファイル消去を保証しない。
  相談・思考の本文、外部送信、stdio 診断ログを利用者に案内する。

## 8. プロトコル（§9）

- **改行区切り JSON-RPC 2.0** を stdin/stdout で。JSON-RPC batch は 1 行の response array として返し、不正な JSON 値や空 batch は `-32600`、構文エラーは `-32700` を返してサーバーを継続する。**stdout へは必ず UTF-8 バイト列**で書く（日本語 Windows では cp932 に落ちて応答が黙って捨てられる）。
- `initialize` の `protocolVersion` は**クライアント提示値をそのまま返す**。固定すると新しめの
  クライアントが `tools/list` を取り消し、60 秒タイムアウトに見える。
- 未知メソッドは `-32601`、未知ツールは `isError: true` の結果として返す（JSON-RPC を壊さない）。
- `initialize` の応答に **`instructions`**（`PROACTIVE_INSTRUCTIONS`）を載せる。ただし **Hermes は読まない**
  （ソース確認済み）ので、Hermes での自発利用は `description` と memory / `SOUL.md` の判断規則で作る。
- `capabilities.logging` を宣言する（`notifications/message` を送るなら MUST。MCP 2025-11-25 Logging）。
  `logging/setLevel` は `{}` を返し、不正なレベルは `-32602`。設定レベルが warning より上なら警告通知を送らない。

### 8.1 ハーネス判別（§8.6）

- **実測**: Hermes の `clientInfo` は MCP Python SDK 既定の `{"name": "mcp", "version": "0.1.0"}` で固有でない。
  `HERMES_*` は子に渡らない（許可リスト方式。`tools/mcp_tool_config.py`）。設定の `env:` ブロックはそのまま渡る。
  親プロセス名は Windows 11 で `wmic` が無く取れず、PowerShell / CIM は起動が秒単位で遅いので使わない。
- 判定（`detect_harness`・文字列比較だけ）: `FREEAGENT_HARNESS` が `hermes`（大小無視）→ **hermes**、それ以外の値 →
  **other**。目印が無ければ `clientInfo.name` に `hermes` を含む → hermes、`mcp` 以外の名前 → other、`mcp` か
  名前なし → **unknown**。
- 出し方（各 1 プロセス 1 回・`_HARNESS_LOCK` 内で判定）:
  - other: `instructions` 先頭に【注意】 / stderr 1 行 / `notifications/initialized` 受信後に `notifications/message`
    （level=warning・logger=`freeagent-bind.harness`） / **最初の `tools/call` だけ** content 先頭に `⚠️` 1 行と
    `structuredContent.harness`
  - unknown: stderr 1 行（目印の入れ方）と最初の結果の `structuredContent.harness` だけ
  - hermes: 何も出さない
- **動作は止めない**。`FREEAGENT_HARNESS_WARN=0` で警告を止める（判定は `structuredContent.harness` と
  `freeagent_models` の `harness` に残す）。`initialize` 前の `tools/call` では何もしない。
- `apply_proactive.py --apply` は自分の節（args に `freeagent_bind` を含む）の `env.FREEAGENT_HARNESS` が `hermes`
  でなければ設定コマンドを加える（`--check` は表示だけ。exit コードには影響しない）。

## 9. 拡張の手順（新しいツールを足すとき）

1. §6 に `tool_xxx(args) -> dict` を足す（例外を出さない。引数は `as_*` で変換）。
2. §7 の `TOOLS` に `name` / `description`（【使う条件】を含む）/ `inputSchema` を足す。
3. §7 の `HANDLERS` に登録する。
4. §8 の `render` に人間向け表示を足す（**空 dict でも落ちないこと**）。
5. `tests/` に回帰テストを足し、`scripts/check_integrity.py` と `scripts/smoke_stdio.py` を通す。
6. README のツール表と、この SPEC の該当節を更新する（同一変更内で）。

## 10. OFF・不通時の縮退（副作用を残さない）

無効化（`enabled false`）やバックエンド全滅でも**利用者のターンは続く**。ここで副作用を残すと、
利用者からは「MCP を入れたら壊れた」と見える。契約は 3 つ。

1. **例外を漏らさず、ハングしない。** 全ツールが `isError` か正常応答で返り、各呼び出しは
   (connect, read) タイムアウトで打ち切られる（TCP が blackhole したホストへ素の呼び出しを投げると
   分単位で固まり、並列で走っている他の呼び出しまで待たされる）。
2. **不通では状態を書かない**（`is_env_failure` → `observe_call` が no-op）。環境障害はモデルの成績では
   ないので、統計に入れると復旧後も選抜が歪む。トレース・クールダウンも同様に書かない。
   **モデルの失敗（429・404・空応答など）は従来どおり記録する**（no-op にするのは環境障害だけ）。
3. **失敗には次の一手を返す**（`structuredContent.next_action`）。`kind` は `unknown_tool` /
   `unavailable_backend` / `auth` / `rate_limited` / `cooling` / `empty_answer` / `error` の 7 種で、
   `advice` と（該当時）`fallback_tools` / `check` / `reenable` を伴う。**`content` には書かない**。
   判断規則（memory / `SOUL.md`）側にも「無効なら代替で完遂し、実際に応答した独立ソースの件数を明記する」
   を置く。これが無いと、OFF のまま存在しないツールを掘り続ける（旧実装の実測）。

検証: `python scripts/check_offline.py`（死んだポートとキー無しで全ツールを呼び、
例外漏れ 0・20 秒以内・`next_action` あり・**状態ディレクトリにファイルが増えない**ことを確認する）。

## 11. 自発利用の設計（率先して使わせる）

- モデルが見る唯一の窓口は **`description`**。共通サフィックス（§7.1）で【競合より優先】【並列】
  【無効・不通のとき】を全ツールに一括付与する（手書きで散らすとドリフトする）。
- **実効レバーは記述だけでは足りない**（実測 1/2 で頭打ち）。効くのは「毎ターン注入される判断規則」と
  「競合の汎用面の除外」の併用（実測 2/2）。文面の単一の出典は `scripts/apply_proactive.py` の `SNIPPET`。
- **除外パターンはライブの実ツール名に照合する**（`fnmatchcase`。glob でなければ完全一致）。
  `ask_*`（アンダースコア）は実名（`ask-all` / `consensus-step` …）に一致しない。**schema キャッシュは
  不完全**（実測 18 件 < ライブ 21 件）なので照合に使わない（`hermes mcp test` を優先）。
  `apply_proactive.py` は照合結果を表示し、設定済みの除外が空振りなら exit 1 にする。
- 測定は回答本文ではなく **`state.db` の `messages.tool_calls`** で行う（`scripts/measure_adoption.py`）。
  遅延カタログ経由の呼び出しは `tool_call` として記録され、実名は `arguments.calls[].name` に入る。
- **思考台帳の常用**: 判断規則（`SNIPPET`）・`freeagent_think` の description 先頭【常用】・
  `PROACTIVE_INSTRUCTIONS` の 3 箇所で「2 段以上の推論が要る問題では、考え始める前に `freeagent_think` を開き、
  plan で分解・revises_thought で改訂・branch_from_thought で分岐・total_thoughts で見積り調整・
  kind=hypothesis と tests_hypothesis で仮説の生成と検証を積む」を指示する。1 問 1 答・単純な事実確認・雑談は
  対象外（全問で開くと 1 ターンが無駄に伸びる）。台帳側でも補強する: 計画なしの 1 ステップ目と、仮説・分岐・
  改訂が 1 つも無い 3 ステップ目に**だけ** `suggestions` で促す（毎ステップ出すと雑音になる）。
- **「有効な間だけ」効かせる**: 判断規則は `<!-- freeagent-bind: proactive-usage -->` 〜
  `<!-- /freeagent-bind: proactive-usage -->` のブロックで SOUL.md に置く。`--write-snippet` は**既存の
  ブロックを最新の文面に差し替え**（旧形式＝終端マーカー無しも可。ブロック外は触らない。改行コードは元の
  ファイルに合わせる）、`--remove-snippet` はブロックだけを外す（MCP を外したときに規則が空振りし続けない）。
  規則自体にも「無効・不通なら存在しないツールを探さず代替で完遂」を含める。

## 12. 検証

```bash
python -m compileall -q src/freeagent_bind   # 構文
python scripts/check_integrity.py            # レジストリ・スキーマ・版の整合
python -m unittest discover -s tests         # オフライン回帰（件数は実行結果を参照）
python scripts/smoke_stdio.py                # 実クライアント経路
python scripts/check_offline.py              # 全滅時の縮退（例外漏れ・ハング・状態汚染なし）
python scripts/measure_adoption.py           # 自発利用率（state.db を読むだけ）
FREEAGENT_PROBE_NET=1 python scripts/smoke_stdio.py   # バックエンド生存
env -u PYTHONPATH PYTHONPATH=src python scripts/warmup_models.py   # モデルの生存確認を定着
```