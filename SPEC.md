# SPEC — hermes-freeagent-bind 内部設計

このファイルは**実装の契約**を書く。README が「何ができるか」、SPEC が「どう作るか」と「なぜそうしたか」。
数値・挙動はすべて**実測に基づく**（推測で書いた箇所は「未検証」と明記する）。

## 0. 非目標

- 推論そのものを持たない（Hermes のプロキシ＝既存の Free モデルへ委譲する）。
- 知識の**蓄積**はしない（蓄積するのは品質統計・クールダウン・進行中の相談だけ）。
- 書き込み系の副作用を持たない（`freeagent_delegate` を除き、全ツールが読み取り専用）。

## 1. 構成（モノリス）

`src/freeagent_bind/server.py` の 1 ファイル。肥大化を前提に **§区画**で分ける。区画の追加は「行を足す」
ではなく「新§を立てる」で行い、冒頭 docstring の目次も同時に更新する（目次が古いと全体が見えなくなる）。

| 区画 | 内容 |
|---|---|
| §0 | 定数・環境変数・プロバイダ仕様 |
| §1 | ユーティリティ（防御的変換・テキスト類似・原子書き込み） |
| §2 | 永続ストア（§2.1 クールダウン / §2.2 品質統計 / §2.3 トレース / §2.4 相談セッション / §2.5 プロバイダ認証の記憶） |
| §3 | プロバイダとモデル（Free 判定・解決・選抜・並列実行） |
| §4 | サブ LLM 呼び出し（フォールバック・空応答・CoT 検出） |
| §5 | 知識バックエンド 6 種（arXiv / Crossref / OpenAlex / Wikipedia / Wikidata / GitHub） |
| §6 | ツール実装（10 本） |
| §7 | ツール定義（`TOOLS` / `HANDLERS`） |
| §8 | 表示（`render`） |
| §9 | JSON-RPC 2.0 / stdio |

**依存は標準ライブラリのみ**。遅延 import するネイティブ拡張は、stdio 起動後に import すると
ツールが無応答になる環境があるため、必要なら起動前に import する（このサーバーは現状それを要しない）。

## 2. 状態ファイル（`FREEAGENT_STATE_DIR`）

| ファイル | 内容 | 消えてよいか |
|---|---|---|
| `cooldowns.json` | 429/404 で「いつまで使わない」か | 消えてよい（次の 429 で再記録） |
| `model_stats.json` | モデル別の成功・空応答・CoT 混入・切断の観測（種類別） | 消えると品質順が初期化＝死んだモデルを選び直す |
| `traces.jsonl` | 1 行 1 呼び出しのメタデータ（**本文は残さない**。`answer_sha1` のみ） | 消えてよい |
| `sessions.json` | 進行中の相談（問い・ラウンド・メインの回答） | 消えると往復が切れる（TTL 1 時間） |
| `provider_auth.json` | 認証で失敗したプロバイダ（15 分・自動選抜から外す） | 消えてよい（次の 403 で再記録） |

いずれも **tmp へ書いて `os.replace` で原子置換**し、`threading.Lock` で保護する。**一時領域には
置かない**（統計とクールダウンが消えると挙動が巻き戻る）。**環境障害（`is_env_failure`）の経路では
どれも書かない**（§10 の 2）。つまりバックエンドが全滅している間、このディレクトリは**空のまま**になる。

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

- `freeagent_models` の `probe: true` は候補を**実際に 1 回呼ぶ**。判定は 4 分類:
  **404/410 → `gone`（除外）** / **401/403 → `auth`（除外）** / **timeout・空応答 → `slow`（残す）** /
  **429・5xx → `error`（残す）**。**生きているが今は応えない**ものを永久に隠さないため。
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

- 外部 HTTP は **(connect, read) のタイムアウト必須**。遮断（接続不可）はホスト単位で記憶して
  fail fast（素の呼び出しは 1 回で分単位に固まり、並列の他呼び出しまで待たされる）。
- 429 は `Retry-After` を尊重してクールダウンへ記録。404/410 は長め（1 時間）、429 は既定 60 秒
  （上限 900 秒）。
- **401/403 はプロバイダ単位で記憶**して自動選抜から外し、原因と直し方を `last_error` に残す
  （空のままだと「すべての候補で失敗しました」しか出ず直しようがない。実測: HF の権限不足がこの形で
  隠れた）。呼び出しが通れば記憶は消す。
- **空応答を成功として返さない**。思考トークンで予算を使い切るモデルがあり（実測: `max_tokens=220`
  で 3 体中 2 体が空）、空を回答として渡すとメイン LLM が無回答を回答と誤解する。予算を上げて
  （`min(max(max_tokens*3, 512), 2048)`）1 回だけ再試行し、なお空なら明示的なエラーにする。
- **CoT 混入はマーカー方式**で検出する（改行数では判定しない。ラベル付き複数行出力を誤判定した
  実測がある）。用途は統計の減点のみで、回答を捨てる理由にはしない。

## 6. 知識バックエンド（§5）

| ソース | エンドポイント | 認証 | 既知の失敗モード |
|---|---|---|---|
| wikipedia | `{lang}.wikipedia.org/w/api.php` | 不要 | 記事名の揺れ（検索 API で吸収） |
| wikidata | `www.wikidata.org/w/api.php` | 不要 | ラベル欠落（`label` は `title` ではない） |
| arxiv | `https://export.arxiv.org/api/query` | 不要 | **http は 301 の先で 406**。連続アクセスで 406 → **3 秒間隔で直列化** |
| crossref | `api.crossref.org/works` | 不要 | `mailto` 未設定だと polite pool に入れない |
| openalex | `api.openalex.org/works` | **検索は API キー必須** | 匿名検索は提供元が制限中（実測 `503 Anonymous search is paused` / `429 Rate limit exceeded`） |
| github | `api.github.com/search/*` | トークン推奨（コード検索は必須） | 未認証は 10 リクエスト/分 |

- 結果は**必ず `citation`（source / title / url / year / summary）に正規化**する。表示も注入もこの形だけを使う。
- 取得はメモリ TTL キャッシュ + ホスト単位の遮断記憶。**LLM を介さない**（＝幻覚が入らない経路）。
- 1 ソースの失敗で全体を落とさない（`errors` に集約し、成功分だけ返す）。

## 7. ツールの規約（§6–§8）

- 全ツールが `content`（人間向け・日本語）と `structuredContent`（LLM 向け純粋 JSON）を返す。
- **例外を外へ漏らさない**。失敗は `structuredContent.error`。`handle_tool_call` が最後の砦として
  `Exception` を捕まえ、`internal error:` に変換する。
- `description` は**モデルが読む唯一の窓口**なので、先頭に【使う条件】【使わない条件】【競合より優先】
  を置く（これが無いと `delegate_task` や `deliberation` が選ばれる）。
- **`content` に LLM 向けの指示文を書かない**（人間が読むチャネル。指示は description と docstring に置く）。
- 合意度は**表層の類似度**であって正しさの確率ではない。返り値にもその旨を書く。
- `「結論/確信度/メインに確認したい点」`の解析は**1 行 1 ラベルと決め打たない**（実測: 「結論: … 確信度:
  88」のように 1 行に複数ラベルが来て確信度を取りこぼした）。取れない項目は推測せず空にする。
- サブエージェント（`freeagent_agent`）は**最終ステップでツールを封じ、回答を要求する**。封じないと
  全ステップを調査に使い、回答が永久に出ない（実測）。到達しなかった場合は推測で埋めず、
  `steps_exhausted` と収集済みの根拠を返す。

## 8. プロトコル（§9）

- **改行区切り JSON-RPC 2.0** を stdin/stdout で。**stdout へは必ず UTF-8 バイト列**で書く
  （日本語 Windows では cp932 に落ちて応答が黙って捨てられる）。
- `initialize` の `protocolVersion` は**クライアント提示値をそのまま返す**。固定すると新しめの
  クライアントが `tools/list` を取り消し、60 秒タイムアウトに見える。
- 未知メソッドは `-32601`、未知ツールは `isError: true` の結果として返す（JSON-RPC を壊さない）。
- `initialize` の応答に **`instructions`**（`PROACTIVE_INSTRUCTIONS`）を載せる。ただし **Hermes は読まない**
  （ソース確認済み）ので、Hermes での自発利用は `description` と memory / `SOUL.md` の判断規則で作る。

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
- 測定は回答本文ではなく **`state.db` の `messages.tool_calls`** で行う（`scripts/measure_adoption.py`）。
  遅延カタログ経由の呼び出しは `tool_call` として記録され、実名は `arguments.calls[].name` に入る。

## 12. 検証

```bash
python -m compileall -q src/freeagent_bind   # 構文
python scripts/check_integrity.py            # レジストリ・スキーマ・版の整合
python -m unittest discover -s tests         # オフライン回帰（75 件）
python scripts/smoke_stdio.py                # 実クライアント経路
python scripts/check_offline.py              # 全滅時の縮退（例外漏れ・ハング・状態汚染なし）
python scripts/measure_adoption.py           # 自発利用率（state.db を読むだけ）
FREEAGENT_PROBE_NET=1 python scripts/smoke_stdio.py   # バックエンド生存
env -u PYTHONPATH PYTHONPATH=src python scripts/warmup_models.py   # モデルの生存確認を定着
```