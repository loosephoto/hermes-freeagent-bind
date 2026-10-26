# SPEC — hermes-freeagent-bind 内部設計

このファイルは**実装の契約**を書く。README が「何ができるか」、SPEC が「どう作るか」と「なぜそうしたか」。
数値・挙動はすべて**実測に基づく**（推測で書いた箇所は「未検証」と明記する）。

## 0. 非目標

- 推論そのものを持たない（Hermes のプロキシ＝既存の Free モデルへ委譲する）。
- 知識の**蓄積**はしない（蓄積するのは品質統計・クールダウン・進行中の相談・進行中の思考台帳だけ）。
- `freeagent_delegate` を除き、外部データを書き換えない。ローカルの作業状態への書き込みと、推論プロンプト・検索語の外部送信はある（§2）。

## 1. 構成（モジュラーモノリス）

本体は `src/freeagent_bind/server.py`（§0〜§4・§5 の横断部・§6〜§10）。**データの一覧は 2 つの
パッケージに出している**: 知識ソース 24 種は `sources/<name>.py`（1 ソース 1 ファイル）、
プロバイダ 18 社は `providers/<name>.py`（1 社 1 ファイル）。`sources/__init__.py` のレジストリが
`SOURCES` / `KB_BACKENDS` を import 順に 1 回だけ組み立て、`providers/__init__.py` が
`PROVIDER_SPECS` と `PROVIDER_ORDER` の既定順を作る（既定順の手書き列挙は持たない）。区画の追加は「行を足す」ではなく
「新§を立てる」で行い、冒頭 docstring の目次も同時に更新する（目次が古いと全体が見えなくなる）。

- **ソース追加は 3 手**: (1) `sources/<name>.py` を作る (2) `sources/__init__.py` の import に足す
  (3) 既定には入れない（規約 28）。`server.py` の `SOURCES` / `KB_BACKENDS` 行はもう存在しない。
- **ソースは共有ヘルパーを `S.<name>` の属性参照で呼ぶ**。テスト 472 箇所が `patch.object(server, ...)`
  で server の名前（`kb_json` 105・`provider_http` 40・`_kb_rate_acquire` 22 …）を差し替えるので、
  直接名で呼ぶと差し替えが届かず「テストは緑のまま本物を叩く」状態になる。`server.py` 側の互換は
  `globals().update(sources.EXPORTS)` が担う（定数・専用ヘルパーも含む）。
- **直接実行を壊さない**: `python server.py` / `runpy.run_path(server.py)` は冒頭の互換シムが
  パッケージ版の `main()` を呼ぶ（配布物は今も stdio スクリプト 1 本）。

| 区画 | 内容 |
|---|---|
| §0 | 定数・環境変数・プロバイダ仕様 |
| §1 | ユーティリティ（防御的変換・テキスト類似・原子書き込み） |
| §2 | 永続ストア（§2.1 クールダウン / §2.2 品質統計 / §2.3 トレース / §2.4 相談セッション / §2.5 プロバイダ認証の記憶 / §2.6 思考台帳） |
| §3 | プロバイダとモデル（Free 判定・解決・選抜・並列実行） |
| §4 | サブ LLM 呼び出し（フォールバック・空応答・CoT 検出） |
| §5 | 知識バックエンド24種（**実装は `sources/<name>.py`**。ここには横断検索・締め切り・引用統合・ホスト予算だけを残す）＝既定6＋§5.9 DataCite / §5.10明示許可代替 / §5.11ホスト予算 / §5.12 OpenAIRE / §5.13引用統合 / §5.14 Zenodo / §5.15 ROR / §5.16 DOAJ・npm・crates.io / §5.17 CiNii（appid必須） / §5.18 OSV・IETF / §5.19 科学（医学・生物学は対象外） / §5.20 HN・Software Heritage・Libraries.io（キー必須） / §5.21 Internet Archive（明示指定のみ） / §5.22 Qiita（明示指定のみ）） |
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
| `model_catalog.json` | プロバイダ別のモデル一覧（TTL 600 秒・§4.4） | 消えてよい（次の選抜で取り直す。**プロセスをまたいで温かくするためだけ**のキャッシュ） |

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

### 4.1 プロバイダ（18 つ）

| プロバイダ | base_url | Free 判定 | 一覧の取得 | 実測（2026-09 時点） |
|---|---|---|---|---|
| `nous` | ローカルプロキシ | プロキシの申告 | 要プロキシ起動 | プロキシ停止中は接続エラー |
| `openrouter` | `openrouter.ai/api/v1` | `:free` または pricing が全部 0 | **未認証可** | 458 モデル / Free 21 / 生存 11 |
| `nvidia` | `integrate.api.nvidia.com/v1` | `free_kind="credit"`（全件が無料枠） | **未認証可** | 82 モデル / **55 件が 404=EOL** / 生存 15 |
| `huggingface` | `router.huggingface.co/v1` | **提供元単位**（`providers[].is_free` または pricing 0） | **未認証可** | 137 モデル / Free 3 / 生存 1（`:together` は Cloudflare Error 1010 で要再確認） |
| `groq` | `api.groq.com/openai/v1` | Free plan確認 + 3 ID許可リスト | API key必須 (`/models`) | 未実測（資格情報なし）。Free limitsは組織/モデル別 |
| `cloudflare` | `api.cloudflare.com/client/v4/accounts/{id}/ai` | Workers Free確認 + 2 ID許可リスト | API token必須。`/models/search?format=openrouter` | 未実測（資格情報なし）。Free割当 10,000 Neurons/日 |
| `gemini` | `generativelanguage.googleapis.com/v1beta/openai` | Unpaid確認・データ利用確認 + 2 ID許可リスト | API key必須 (`/models`) | 未実測（資格情報なし）。OpenAI互換はBeta |
| `vercel` | `ai-gateway.vercel.sh/v1` | Free tier確認 + 15 ID許可リスト | API key必須 (`/models`) | カタログ **406 件**（未認証可・各行に pricing）。Free Tier 対象は **15 件**を実測採取 |
| `ollama` | `ollama.com/v1` | Free plan確認 + `free_kind="credit"` | API key必須 (`/models`) | カタログ **17 件**（未認証可・pricing なし）。starter の範囲は非公開 |
| `zai` | `api.z.ai/api/paas/v4` | Free tier確認 + 3 ID許可リスト | API key必須 (`/models`) | 一覧は**有料の旗艦 11 件だけ**を返し Free の 3 ID を含まない（実測 2026-10-23）→ 許可リストの ID を在庫として補う（`synthesize_free_ids`。既定は無効）。Free の 3 ID は推論 200、有料は 429 `Insufficient balance` |
| `mistral` | `api.mistral.ai/v1` | Experiment plan確認 + `free_kind="credit"` | API key必須（未認証は 401） | カタログは認証必須のため未実測。Experiment plan は月 約10億トークン・電話確認・RPM 低め |
| `modelscope` | `api-inference.modelscope.cn/v1` | 無料枠確認 + `free_kind="credit"` | **未認証可** | カタログ **37 件**（pricing なし・日次枠で回復）。Qwen / DeepSeek / GLM / Step など |
| `cerebras` | `api.cerebras.ai/v1` | Free trial（$5・カード登録・30 日）確認 + `free_kind="credit"` | API key必須（未認証 401 `Wrong API Key`） | 未実測（資格情報なし）。クレジット失効後は従量課金 |
| `sambanova` | `api.sambanova.ai/v1` | 支払い方法なし＝Free tier の確認 + `free_kind="credit"` | **未認証可** | カタログ **5 件**（未認証 200・実測 0.2 秒）。Free tier は 20 RPM / 20 RPD（公式レート制限表） |
| `novita` | `api.novita.ai/v3/openai` | 無料クレジット（$0.5〜$1・期間限定）の確認 + `free_kind="credit"` | **未認証可** | カタログ **95 件**（未認証 200・実測 2.0 秒）。クレジットを使い切ると課金 |
| `siliconflow` | `api.siliconflow.com/v1` | 無料モデルの確認 + `free_kind="credit"` | API key必須（未認証 401 `Token is invalid`） | 未実測（資格情報なし）。無料モデルはレート制限固定。中国版は `api.siliconflow.cn/v1` |
| `longcat` | `api.longcat.chat/openai/v1` | 公開ベータ無料枠（公称 50 万トークン/日）の確認 + `free_kind="credit"` | API key必須（未認証 401） | 未実測（資格情報なし）。Meituan の OpenAI 互換エンドポイント |
| `opencode` | `opencode.ai/zen/v1` | `-free` 接尾辞の許可リスト **8 件**（一覧に pricing が無いため） | API key必須（未認証 401） | 一覧 **40 件**（キーで 200）・`-free` は 8 件。**この環境のキーは推論 403 `Model access is disabled`**（キー権利の問題。無料モデルも同じ） |

### 4.1.1 追加プロバイダの Free / 課金防護

**v0.12.0 で 6 社を追加**（`cerebras` / `sambanova` / `novita` / `siliconflow` / `longcat` / `opencode`）。
候補は GitHub の `freellmapi`（MIT）の provider 目録から取り、**無認証 `GET /models` で生存を実測**した
（200＋JSON＝公開カタログ / 401・403＝存在してキーが要る / 404・例外＝廃止）。
**非公式リレー（`xkiro` / `navy` / `ainative` / `llm7` / `kilo` / `routeway` / `requesty` / `unorouter` 等）は
採用しない** — 利用条件と再配布の許諾が確認できず、規約 33（非公式・無文書の経路は採らない）に反する。
5 社は `free_kind="credit"`（プラン/クレジットの確認変数が必須）、`opencode` だけは `-free` 接尾辞の
許可リスト方式（一覧に pricing が無いため）。追加は `PROVIDER_ORDER` の**末尾**（オプトイン）。


- Groq / Cloudflare / Gemini / Vercel / Ollama / Z.ai / Mistral / ModelScope はAPIからアカウントの課金tierを確実に判定できないため、必要なAPI資格情報に加えて専用の確認envを必須にする。確認envは文字列 `1` / `true` / `yes` / `on` のいずれかのみ有効。値は利用者の申告であり、実際の契約をAPI検証するものではない。プラン/Billing設定を変更したら確認envを外す。
- 許可リスト方式のプロバイダ（Groq / Cloudflare / Gemini / Vercel / Z.ai）はモデル一覧のpricingが0でもFreeとはみなさず、`free_model_ids` の完全一致だけをFreeにする。`call_model` の明示model指定も同じ許可リストで拒否し、catalogに出た有料/未知モデルへ直接HTTPしない。
- Groqは `GROQ_API_KEY` と `FREEAGENT_GROQ_FREE_TIER=1` が揃った時だけ一覧/推論を有効化。許可IDは `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`。Free planの上限はモデル・組織ごとに変わるためアカウントのLimitsを確認し、Developer planは従量課金であることを警告する。
- Cloudflareは `CLOUDFLARE_API_TOKEN`、32桁hexの`CLOUDFLARE_ACCOUNT_ID`、Workers AI Read権限、`FREEAGENT_CLOUDFLARE_FREE_PLAN=1` が揃った時のみ利用。チャットは `/v1/chat/completions`、モデル一覧はアカウントREST APIの`format=openrouter`を使う（互換APIの`/models`ではない）。Free許可IDは `@cf/openai/gpt-oss-20b` と `@cf/zai-org/glm-4.7-flash`。Workers Freeは10,000 Neurons/日超過後に処理停止、Workers Paidは超過分を課金するため、Free plan確認を外さない。CloudflareはCustomer Contentをモデル学習/サービス改善に使わないと明記する（ストレージサービス連携時は保存される可能性）。
- Geminiは `GEMINI_API_KEY`（別名 `GOOGLE_API_KEY`）、`FREEAGENT_GEMINI_FREE_TIER=1`、`FREEAGENT_GEMINI_UNPAID_DATA_ACK=1` が全て揃った時のみ利用。許可IDは `gemini-3.8-flash` と `gemini-3.7-flash`。Free/unpaid tierでは入力・出力がGoogle製品改善に利用され、人手レビューされる場合があるため、機密・個人情報を送らない。
- Vercel AI Gatewayは `AI_GATEWAY_API_KEY` と `FREEAGENT_VERCEL_FREE_TIER=1` が揃った時のみ利用。Free tierは**月 $5 のクレジット**で、使えるのは **Free Tier 対象モデルだけ**（カタログ 406 件すべてではない）。対象は公式モデル一覧の `freeTier` フィルタから **15 件を実測採取**して許可リストにした（2026-10）。**$0 価格のモデルとは一致しない**（実測: 15 件中 $0 は 3 件のみで、残りは有料価格だがクレジット対象）。対象は増減するため `probe` で生存を確かめ、外れた ID は許可リストから外す。クレジットを購入すると paid tier に移り月次の無料クレジットは適用されない。
- Ollama Cloudは `OLLAMA_API_KEY` と `FREEAGENT_OLLAMA_FREE_PLAN=1` が揃った時のみ利用。Free planは **starter モデル向けの月次クレジット**で、**どの ID が starter かは提供元が公表しておらず**、一覧 17 件も pricing を持たない（実測）ため許可リストを作れない。よって `free_kind="credit"`（クレジット枠）として扱い、**無料対象はカタログの一部**であることを明記し、`probe` で実際に応答するモデルを確認してから使う。対象外のモデルは 402 を返すが、402 はフォールバック対象でプロバイダ記憶には入れない（§3）。同時 1 リクエスト。提供元はプロンプト/応答をログ・学習しないと明記している。
- Z.aiは `GLM_API_KEY`（別名 `ZAI_API_KEY` / `ZHIPUAI_API_KEY`）と `FREEAGENT_ZAI_FREE_TIER=1` が揃った時のみ利用。許可IDは `glm-4.7-flash`, `glm-4.5-flash`, `glm-4.6v-flash`（公式価格表で $0）。**提供元の一覧 API はこの 3 件を返さない**（実測 2026-10-23: 有料の旗艦 11 件だけ）ため、許可リストの ID を在庫として補う（`synthesize_free_ids` + `_with_allowlisted_rows`）。実測: 3 件は推論 200、有料の `glm-5.3-flash` / `glm-4.5` は 429 `{"code":"1113","message":"Insufficient balance or no resource package"}`。
- Mistral La Plateformeは `MISTRAL_API_KEY` と `FREEAGENT_MISTRAL_FREE_PLAN=1` が揃った時のみ利用。Experiment plan（無料・月 約10億トークン・電話確認・RPM 低め）は API から Free/paid を判定できないため `free_kind="credit"`（クレジット枠）として扱い、対象は `probe` で確認する。並列 fan-out より長文・少数リクエスト向き。
- ModelScopeは `MODELSCOPE_API_KEY`（別名 `MODELSCOPE_API_TOKEN`）と `FREEAGENT_MODELSCOPE_FREE_PLAN=1` が揃った時のみ利用。API-Inference の無料枠（公称 2,000 リクエスト/日・日次回復）は一覧 37 件が pricing を持たない（実測）ため `free_kind="credit"` として扱い、対象は `probe` で確認する。
- これらのallowlistはFree提供条件の保守対象。提供元仕様/モデル提供条件を定期確認し、実アカウントが無い状態では認証付き一覧・推論成功を主張しない。
- 根拠: [Groq rate limits](https://console.groq.com/docs/rate-limits) / [billing](https://console.groq.com/docs/billing-faqs); [Cloudflare model search](https://developers.cloudflare.com/api/resources/ai/subresources/models/methods/list/) / [pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) / [OpenAI compatibility](https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/) / [data usage](https://developers.cloudflare.com/workers-ai/platform/data-usage/); [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) / [OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai) / [terms](https://ai.google.dev/gemini-api/terms); [Vercel AI Gateway pricing](https://vercel.com/docs/ai-gateway/pricing) / [models](https://vercel.com/ai-gateway/models?freeTier=true) / [OpenAI compatibility](https://vercel.com/docs/ai-gateway/openai-compatibility); [Ollama Cloud](https://docs.ollama.com/cloud) / [OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility) / [pricing](https://ollama.com/pricing); [Z.ai pricing](https://docs.z.ai/guides/overview/pricing) / [GLM OpenAI compatibility](https://docs.z.ai/guides/develop/http/introduction); [Mistral La Plateforme pricing](https://mistral.ai/pricing) / [OpenAI compatibility](https://docs.mistral.ai/api/); [ModelScope API-Inference](https://modelscope.cn/docs/model-service/API-Inference/intro) / [rate limits](https://modelscope.cn/docs/model-service/API-Inference/limits).

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
- `probe: true` は実際にモデルを呼ぶため、`free_only` 省略時でも自動的に Free モデルのみを対象とし、
  `provider` 未指定時は資格情報が揃った（`provider_ready`）プロバイダを優先する。また `call_model(..., free_only=True)`
  はキャッシュ済み一覧で `free=False` と判明している有料モデルを拒否し（`:free` 付き ID は完全一致を先に見る）、
  `provider_http` の推論 POST は `not provider_ready(provider)` なら空 Bearer を送らず即座に未設定エラーを返す。
- HTTP 500 等の本文中に `not found` を含む応答（実測: NVIDIA NIM の未認証応答 `HTTP 500: ... Authorization<Bearer> was not found`）が
  `gone`（404 廃止）に化けないよう、`classify_error` と `probe` 判定では 5xx / 401 / 403 のステータス判定を `not found` 部分一致より優先する。
  既に 404/410 でクールダウン中のモデルを再度 `probe` した場合は `cooling_reason` を引き継いで `gone` として除外する。
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
- **非チャットモデルは自動選抜とフォールバックから外す**（§3.2）。無料一覧には埋め込み・ガード/安全
  分類器・画像/音声生成・OCR が混ざる（実測: Free 102 件中 30 件＝約 29%）。目印は**2 群に分ける**。
  **A 群（用途が対話でない: 埋め込み・再ランク・ガード/分類器・報酬・OCR/パース・音声認識）は出力
  モダリティの宣言より優先**して外す — 宣言は「テキストを出力するか」しか答えないため
  （実測: OpenRouter は `nvidia/nemotron-3.5-content-safety:free` を `output_modalities: ["text"]` と
  宣言するが、返るのは "User Safety: safe" という分類結果で、`think` の検証者に選ばれると判定不能に
  なり、反証が 1 件も得られなかった）。**B 群（視覚入出力・生成: `vision` / `vlm` / `lyria` /
  `diffusion` 等）は対話モデルにも付きうる**ので（`...-vision-instruct` は視覚入力の対話モデル）、
  **提供元の宣言があればそちらを優先**する。宣言が無い一覧（NVIDIA など）は両群の ID 目印で保守的に
  外す（目印は**区切りに囲まれた語**だけに当てる）。**明示指定は在庫の全 Free で確認する**ので
  名指ししたモデルはこれまでどおり試す。`freeagent_models` は `non_chat_candidates` に件数を出し、
  一覧では見えるままにする。

#### 4.3.1 並べ方の strategy（v0.17.0）

「誰を働かせるか」の**並べ方**だけを差し替え可能にした（`FREEAGENT_SELECT_STRATEGY`）。
`diverse_order`（プロバイダ巡回）は共通の後段なので、どの strategy でも 1 社が独占しない。

| strategy | 並べ方 | 用途 |
|---|---|---|
| `quality`（既定） | `rank_models`＝Beta 事後 mean + 0.25·sd（観測の少ないモデルを試す） | 通常運用。**v0.16.0 までと同一** |
| `catalog` | 一覧が返した順 | 切り分け・比較（品質統計を効かせない） |
| `least_busy` | 提供元の実行中本数（`_ProviderGate` の in_flight）が少ない順。同点は品質順 | 同時実行が詰まるとき |

未知の値は `quality` に落ちる（起動を止めない）。選んだ根拠は `select_models` の info の
`selection_strategy` に載る。`scripts/measure_quality.py --select-strategy <name>` で
**サーバーが実際に選ぶモデル**のまま A/B できる（未指定は従来どおり一覧の先頭から）。
回帰は `tests/test_selection_strategies.py`（7 件・同点で揺れないことと多様性の維持を含む）。

### 4.4 一覧の取得（§3.3・並列・締め切り・プロセス間キャッシュ）

`fetch_provider_models` / `all_models` / `provider_status` は選抜の入口で、**サブLLM のツール
（`ask` / `panel` / `grounded` / `think` …）が必ず通る**。ここが直列だと、遅いプロバイダ 1 つの
待ち時間がそのまま利用者の待ち時間になる（1 リクエストの上限は `min(30.0, READ_TIMEOUT)`）。

- **取得の順序はメモリ → ディスク → HTTP**。ディスク（`model_catalog.json`）の TTL は 600 秒で、
  メモリと同じ。stdio はセッションごとに新しいプロセスなので、ディスクが無いと**起動のたびに
  全プロバイダへ投げ直していた**（実測 0.842 秒・直列）。
- **並列 + 締め切り**: プロバイダごとに専用スレッドで投げ、`FREEAGENT_CATALOG_DEADLINE`（既定 10 秒）
  で打ち切る（プロバイダはすべて別ホストなので同時でよい）。合計は max(各プロバイダ) になる。
- **間に合わなかった分は前回の成功値で埋める**（メモリ → ディスク・TTL 無視）。**取得の「失敗」は
  埋めない** — 空 + `error` のまま返し、`provider_status` に出す（規約 8。締め切り超過は「分からない」、
  失敗は「分かった上で取れない」で別物）。
- **単一飛行**（`_MODELS_INFLIGHT`）: 同じプロバイダの取得が進行中なら待つ（並列ツール呼び出しで
  上流へ二重に投げない。`_kb_cached` と同じ型）。
- **書くのは成功したときだけ**（空では書かない）。`FREEAGENT_STATE_DIR` の下に
  `model_catalog.json`（`version` + `providers`）を tmp + `os.replace` で置き、`_file_lock` で
  プロセス間を直列化し、読みは mtime が変わったときだけ読み直す。`FREEAGENT_CATALOG_CACHE=0` で無効化。
  バックエンド全滅の検査（`check_offline.py`）は「状態ディレクトリにファイルが増えない」ことを見るので、
  失敗時に書かないことが契約になる。
- **実測（2026-10-21・この環境）**: `all_models()` 冷 0.842 秒（直列）→ 0.693 秒（並列）。
  新しいプロセスで最初の選抜まで 0.31〜0.75 秒 → **0.0025 秒**（ディスクが温かいとき）。
  `all_models()` を別プロセスから: 0.7965 秒 → **0.005 秒**。
- 選抜を知識取得と**重ねる**案（`grounded` で先に選抜スレッドを起動）は**採らなかった**。早期返却
  （根拠 0 件）の経路で提供元へ一覧要求を出す契約違反になり（`tests/test_knowledge_extensions.py` が
  固定している）、ディスクキャッシュで縮むのは初回の 0.3 秒だけなので割に合わない。

### 4.5 並列実行の予算（`Deadline`・v0.16.0）

`run_parallel` は `Deadline` を渡されると**残り時間で回収を打ち切る**。Python のスレッドは強制終了
できない（anyio の文書も「shutdown にはコード側の完全な協力が要る」と明記）ので、打ち切り＝
**回収をやめる**ことであり、投入済みの呼び出しは裏で完了する（キャッシュ・統計・クォータには正しく残る）。
未回収の位置は `{error, dropped: true, timed_out: true}` で埋め、**黙って `None` にしない**（規約 8・63）。
まだ走っていないジョブは `cancel_futures=True` で取り消す（枠・クォータを無駄に使わない）。

- 既定は `FREEAGENT_CALL_BUDGET`=240 秒（0 で無制限）。**MCP クライアントの待ちは実測 300 秒**で、
  300 秒で切られると取れていた結果も失われる。その手前で切って**脱落として見せる**。
- 予算は `ask_many` / `ask_map` が作る（ツール 1 回ぶん）。`panel` の refine は別呼び出しなので
  それぞれに付く（合計は 2×予算まで）。
- 実測（`tests/test_parallel_budget.py`）: 1.0 秒の worker を 0.1 秒の予算で打ち切ると **0.5 秒以内**に
  返る／並列度 1・予算 0.05 秒では走り出した 1 件以外は**実行されない**（取り消しが効いている）。

## 5. 呼び出し（§4）

- 外部 HTTP は **接続タイムアウトと読み取りタイムアウトを分けて設定**する（既定 10 秒 / 180 秒）。`_urlopen` は §3 の共有 `_OPENER` を使う。これは `http.client` の接続クラスに `_ShortConnectMixin` を混ぜ、`HTTPHandler` / `HTTPSHandler` を差し替えたもので、**接続（TCP と TLS ハンドシェイク）の間だけ** `CONNECT_TIMEOUT` を適用し、接続直後に socket を `READ_TIMEOUT` へ切り替える。`build_opener` はサブクラスを既定ハンドラの置換と見なすので、リダイレクト（`HTTPRedirectHandler`）・プロキシ（`ProxyHandler`）・`HTTPError` の既定挙動は保たれる。環境障害（接続不可・timeout）は同じプロバイダ系列の fallback を打ち切り、全候補へ同じ不通を繰り返さない。
  - **実測（修正前）**: `urlopen(timeout=CONNECT_TIMEOUT)` の上限は**応答ヘッダの待ち時間にも掛かった**。ローカルプロキシは上流の生成が終わるまで本文もヘッダも返さないため（実測: TTFB 6.13 秒 = total 6.13 秒）、生成が `CONNECT_TIMEOUT`（10 秒）を超えると `TimeoutError` になり、読み取り 180 秒は使われなかった（実測: 1200 トークンの要求が 10.0 秒で timeout）。この失敗は `is_env_failure` に該当するため**統計にも残らない**。
  - **修正後の契約**: ヘッダ待ちと本文読取は `READ_TIMEOUT` まで待てる。接続できないホストへの fail fast は接続上限のまま。`tests/test_audit_regressions.py` の `TestHttpTimeoutSeparation` が localhost の遅延サーバで 5 点（接続上限 < ヘッダ遅延 < 読み取り上限 → 成功 / 読み取り上限で打ち切り / 不通ホストは接続上限内で失敗 / 302 追従 / 4xx は `HTTPError`）を検査する。実サーバーでも確認（https://httpbin.org/delay/12 = 12 秒遅延: 旧方式 10.58 秒で `TimeoutError` → 新方式 10.76 秒で成功）。
  - **版差の吸収**: 3.13 以前の `HTTPSHandler` は `check_hostname` を保持して `do_open` へ渡すが、**3.14 以降は渡さない**（`HTTPSConnection` も `check_hostname` を受け付けない）。`getattr(self, "_check_hostname", None)` で吸収する。実バックエンド検証で `AttributeError` を検出して修正した（localhost の http テストだけでは通ってしまうので、`_HTTPSHandler` を直接検査する回帰テストを併置している）。
  - **Windows のオンデマンド ルート CA と `SSLContext` 再読込**: Windows は未訪問のルート CA（実測: `export.arxiv.org` の `Starfield Root Certificate Authority - G2`）を Schannel 接続時にオンデマンドで取得するため、Python 同梱 OpenSSL の初回接続や起動時生成の `_HTTPSHandler._context` では `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` になることがある。`_urlopen` は `CERTIFICATE_VERIFY_FAILED` 発生時に Windows 標準の `System32\curl.exe -sI`（Schannel）でルート CA 登録を促したうえで `ssl.create_default_context()` を再生成して 1 回だけ再試行する（証明書検証は無効化しない）。
  - これにより、`think` の代替案の `max_tokens` 既定 400 を縛っていた「proxy 経由の 10 秒制約」も解消した（既定値は待ち時間と利用枠の理由で据え置き）。
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

### 5.1 モデル数と提供元別の同時実行ゲート（§3.4・v0.9.5）

**Free モデルの在庫は 1 提供元に偏る**（実測: この環境の ready は `nous` の 1 社で、Free の対話モデルは 8 体）。
在庫が 1 社に偏ったまま並列度だけ上げると「同じ提供元へ同時に投げる」ことになり、429 を踏む。
そこで**同時実行の上限をプロセス全体から提供元ごとへ移した**（一次防護）。429 は
`Retry-After` を見てクールダウンに入れる従来の**事後**処理として残る。

- **ゲートの契約**: 提供元ごとに `limit`（現在の枠）と `cap`（戻せる上限）を持ち、`in_flight >= limit`
  の呼び出しは**待つ**。待ちには上限（`FREEAGENT_GATE_WAIT_S`・既定 20 秒）があり、過ぎたら
  `ProviderBusyError` → **その候補を脱落**として返す（無限待ちを作らない。例外は外へ漏らさない）。
  待ち予算は**1 回の `call_model` で共有**する（候補ごとに満額待つと「候補数 × 20 秒」待つ）。
- **適応**: 429 を受けたら枠を 1 本減らし（下限 1）、連続成功 `_GATE_RESTORE_AFTER`（既定 3）回で 1 本戻す。
  縮小専用だと一時的な 429 で永久に痩せるため（実測: 枠 8 で 429 が 1〜2 件出たあと 7→6 へ縮み、
  成功が続いて 7 へ戻った）。
- **上限の決め方**: `FREEAGENT_PROVIDER_LIMITS="openrouter=2,..."` > 実測既定（`nous` 6）> 全体既定
  （`FREEAGENT_PROVIDER_LIMIT`・4）。提供元ごとに分けるのは、**別ホストなら上流の枠も別**だから
  （全体 1 本の並列度では「別提供元を同時に走らせる」を表現できない）。
- **実測（2026-10-23・8 モデルの panel・max_tokens 128）**:

  | 枠 | HTTP 429 | 回答 | 壁時計 |
  |---|---|---|---|
  | 8 | 1〜2 件 | 4〜5/8 | 11.3 秒 |
  | 6 | 0〜1 件 | 6〜7/8 | 6.0〜6.3 秒 |
  | 4 | 1 件 | 6/8 | 4.4〜6.7 秒 |

  429 の本文は `The requested model is temporarily at capacity upstream` で、**モデル単位の上流容量**が
  主因だった（同時数を 2 まで下げても 0 にならない）。よって枠は「自爆を減らす」ためのもので、
  429 を 0 にする保証ではない。**並列度そのもの**（同一プロンプト × 12 呼び出し・fallback 無し）は
  4 並列 13.13 秒 / 8 並列 8.08 秒 / 12 並列 13.78 秒で、8 本が膝。
- **モデル数**: `size` の上限は `FREEAGENT_MAX_SUB_MODELS`（既定 8）、`models` 配列と
  `models: ["all"]` の展開上限は `FREEAGENT_MAX_MODELS_PER_CALL`（既定 16）。
  **明示 `models` は `size` で切り落とさない**（実測: 8 件渡しても `size` 既定 3 で 3 件しか走らず、
  「在庫を全部束ねる」ができなかった。旧 3 件 → 新 8 件）。`size` は**自動選抜の目標本数**である。
- **観測**: panel / fanout / consult / grounded は `concurrency`（`per_provider` の `limit` / `cap` /
  `in_flight`、`waited_total` / `reduced_total` / `timeouts_total`、`scope`）を返し、
  呼び出し単位の枠待ちは `slot_waited`（`_rate_limit_report`）に入る。`concurrency` のキーは
  **提供元名**（ref を渡しても提供元へ正規化する。実測: ref をそのままキーにして架空の提供元が並んだ）。

### 5.2 選抜・クォータ・クールダウンのはしご（v0.11.0）

オーケストレーションの質は「誰を選ぶか・何回まで許すか・失敗をどう長引かせないか」で決まる。
v0.11.0 は GitHub の実装（`freellmapi` の設計文書・MoA・FrugalGPT・RouteLLM の考え方）から
**手法だけ**を取り、依存ゼロで自前実装した。丸写しはしていない（相手は TypeScript / torch 前提）。

- **選抜は Beta 事後（決定的）**: `score = mean + 0.25·sd`（`a=1+成功, b=1+失敗`）。
  成功率は **kind ごとに n で重み付け**して合算する。旧実装は `model_quality`（kind の単純平均）に
  `0.25/(1+n)` の探索項を足していたため、**観測 1 件の成功（q=1.0）が n=50・90% のモデルを上回り**、
  同点は `sorted` の**アルファベット順**で決まっていた。実測（本番 traces 14 日・窓内 62 件・25 モデル）:
  旧順位と事後平均順の上位 8 は **0/8 一致**（集合 3/8）。乱数は使わない（Thompson の事後を決定的に使う
  ので、同じ入力で同じ順位になり、テストと説明が再現する）。
- **空応答（`no_output`）はハード失敗の半分の重み**（v0.18.0）。`classify_error` が `no_output` を
  独立に返し、品質スコアは `err` の内訳を `hard + 0.5·no_output` で減点する。理由は実測: 思考モデルが
  `max_tokens` を使い切ると本文が空で返り（A/B の catalog アームで 64 呼び出し中 16 件）、
  床 512・上限 2048 まで上げても空のままのモデルがあった。これを 404/403/5xx と同じ重みで沈めると
  **大きい予算でなら正しく答えるモデルが永久に選ばれなくなる**。0 にはしない（成功 0 件なら品質は 0）。
  内訳は `model_stats.json` の `kinds.<kind>.err.no_output` に残る（旧記録は `other` のまま再集計しない）。
- **クォータは宣言制**（`FREEAGENT_QUOTA_LIMITS="openrouter:rpd=1000+nous:rpm=60"`）。単位は
  `rpm` / `rpd` / `tpm` / `tpd`、分は直近 60 秒、日は **UTC の日付境界**（提供元は 00:00 UTC に戻す）。
  **宣言が無ければ何も記録せず止めもしない**（既定の挙動を変えない。`check_offline.py` の
  「状態を書かない」契約も守る）。上限に達した呼び出しは 429 を踏む前に `QuotaExhaustedError` で
  脱落し、理由（残り時間つき）を返す。同時実行の枠（§5.1）は**同時数**、これは**窓の量**で別物。
- **429 のクールダウンははしご**: 1 回目 60 秒（Retry-After があればそれを尊重）→ 2 回目 180 秒 →
  3 回目以降 600 秒（上限 900）。**冷えきってから再度 429 なら 1 段目に戻す**（時間をあけた 1 回を
  「何度目か」に数えない）。一律 60 秒だと「毎分 1 回叩いて毎回 429」になり、相手の枠を無駄に食う。
- **評価は `scripts/measure_quality.py`**（正答率）。`single` / `panel`（多数決）/ `moa`（提案→統合）/
  `cascade`（2 体一致で打ち切り）を、期待値を宣言したケースで機械採点する。実測（4 問・8 体）:
  全戦略 4/4 で**天井**。したがって「MoA が良い」とは言えない（呼び出しは 1 : 8 : 9 : 2）。
  判断は `--cases all --repeat 2` で行う。

### 5.3 MoA の集約層（`freeagent_panel` の `refine`・v0.13.0）

- **契約**: `refine: true` で参加者の回答を 1 つの答えに統合し `refined` に返す。呼び出しは参加数 + 1。
  既定は false（後方互換。`refine` を付けなければ余分な呼び出しをしない）。
- **統合役**: **参加していないモデル**の最上位を優先（`rank_models`）。全員参加しているときだけ最上位が
  兼務し、どちらだったかを `reason` に書く。同じモデルに自分の答えを統合させると**合意度が上がるだけで
  新しい情報が入らない**。明示 `refine_model` があればそれを使い、許可リスト・無料判定は `call_model`
  がそのまま効かせる（有料・未許可は拒否）。
- **プロンプト**: 「平均せず、対立点は事実確認して決める」＋「結論 / 確信度 / 根拠」の 3 行形式。
  `parse_labeled` で解析し `confidence` を数値で返す（本文も `text` に残す）。
- **失敗時**: パネルの結果は返し、`refined.error` に理由を残す（規約 8）。統合役の呼び出しも
  提供元の枠（§5.1）・クォータ（§5.2）・統計を通り、`calls` に合算される。
- **実測（6 体・3 ケース・2026-10-23）**: 呼び出し 7（参加 6 + 統合 1）・16〜21 秒・正答 3/3
  （`single` / `panel` と同じ＝天井）。**品質向上はまだ示せていない**（ケースが易しすぎる）。
  だから既定は false のままとし、`scripts/measure_quality.py --strategies refine --cases all --repeat 2`
  で優位が出た場合に既定を変える。

### 5.4 評価基盤（v0.14.0・P6）

天井（全戦略 100%）に当たって「改善した」と言えない状態を壊すための土台。**測り方の誤りを先に直す**。

- **最終回答の抽出**（`freeagent_bind.grading.extract_final_answer`）: ① `Final answer:` 系ラベルの
  **最後**の出現 → ② `\boxed{}` → ③ 短い本文なら最終行の値。**本文全体を走査しない**。
  旧採点は本文の全数値を走査し、部分一致で比べていたため、`"The cube of 17 is 4913? No — 2900"`
  や `"This is not silver, it is mercury."` を**合格**にしていた（9 例のプローブで 4 件誤判定）。
- **`no_answer` の区別**: 答えが書かれていない回答は誤答ではなく `no_answer` として数える。
  `truncated` が立っている応答、200 字を超える本文、`\text{Final answer: }` のような**中身の無い
  ラベル**からは**推測しない**（実測: 切れた推論の末尾の数値を拾って「7」「11」を誤答にしていた）。
- **再質問は 1 回だけ**: `no_answer` のとき、同じ問い＋直前の回答を添えて「Final answer の 1 行だけ」を
  要求する（呼び出し数に加算）。実測: 切り詰め 3/24 のうち 2 件は推論が正しく、最終行だけが無かった。
- **正規化**（Math-Verify の転用・サブセット）: LaTeX 分数（`\frac{2}{7}` / `\frac13`）、`%`→小数、
  単位、桁区切り、区間と整数集合の同一視（`[-6,-3)` ≡ `{-6,-5,-4}`）、`kind="exact"` は厳密一致。
- **ケースは自前生成**（`scripts/gen_cases.py`）: 6 系統（割合・速度と単位・連立合同式・包除＋否定・
  整数区間・LaTeX 分数）を、**正解を計算しながら**生成し、**別経路で再計算して検算**する。
  外部データセットを同梱しない（再配布条件を避ける）。**難易度の帯は事前登録**（tier 1/2/3 を同数）で、
  結果を見て測る帯を選ばない（選択バイアス）。
- **連続指標**: クラスタ熵・戦略内一致率・Brier / ECE（`--calibrate`）。**正答率は従属指標**。
  根拠: paired McNemar（α=0.05・検出力 80%）のシミュレーションで 10 ポイント差に 300〜400 ケース
  必要、5 ポイント差は 1000 件超で不可能。連続指標は d=0.5 で 30〜50 件。
- **対応のある比較**: 基準戦略（既定 `single`）との差・95% 信頼区間（bootstrap・seed 固定）・
  McNemar の p 値・`no_answer` 差を出す。**点推定だけで「改善した」と言わない**。

## 6. 知識バックエンド（§5）

| ソース | エンドポイント | 認証 | 既知の失敗モード |
|---|---|---|---|
| wikipedia | `{lang}.wikipedia.org/w/api.php` | 不要 | 記事名の揺れ（検索 API で吸収） |
| wikidata | `www.wikidata.org/w/api.php` | 不要 | ラベル欠落（`label` は `title` ではない） |
| arxiv | `https://export.arxiv.org/api/query` | 不要 | **http は 301 の先で 406**。連続アクセスで 406 → **3 秒間隔で直列化** |
| crossref | `api.crossref.org/works` | 不要 | `mailto` 未設定だと polite pool に入れない |
| openalex | `api.openalex.org/works` | **常用では無料APIキーを推奨（匿名基本利用も可）** | 匿名検索は提供元が制限中（実測 `503 Anonymous search is paused` / `429 Rate limit exceeded`） |
| github | `api.github.com/search/*` | トークン推奨（コード検索は必須） | 枠は**エンドポイント別**（実測: search 30/分・code_search 10/分・未認証 10/分）→ 予算キーをサービス単位に分ける（§5.6）。code は `text_matches` の断片を返し、**ライセンス情報を含まない**。索引は default branch のみ・384KB 未満・直近 1 年に活動のあるリポジトリ等に限定 |
| datacite | `api.datacite.org/dois` | 不要、mailto任意 | モード間でホスト予算共有。抄録・登録反映の欠落あり |
| openaire | `api.openaire.eu/graph/v3/research-products` | 今回は匿名のみ | 公式60/h、メタデータCC-BY表示、本文欠落あり |
| zenodo | `zenodo.org/api/records/` | 公開検索は匿名 | 説明メタデータCC0（メール除外）、非軍事用途、ファイル条件は別 |
| ror | `api.ror.org/v2/organizations` | 今回は匿名 | 機関属性CC0、検索候補であって機関同定の確定ではない |
| doaj | `doaj.org/api/search/articles/` | 不要（キーは発行者向け） | 記事メタデータCC0、全ルート2req/s、OA記事限定 |
| npm | `registry.npmjs.org/-/v1/search` | 不要 | 公式公開API。説明は登録者の自己申告（審査結果ではない） |
| crates | `crates.io/api/v1/crates` | 不要、識別UA必須 | Crawler Policy: 1req/s上限。説明は登録者の自己申告 |
| osv | `api.osv.dev/v1/query`・`/v1/querybatch`・`/v1/vulns/{id}` | 不要 | 生態系を省くと HTTP 400。10 生態系を querybatch で照合してから `/v1/query`。集約元は各脆弱性DB |
| ietf | `datatracker.ietf.org/api/v1/doc/document/` | 不要 | `title__contains` は AND 不可（2 語で 0 件）。`type__in=draft,rfc` で slides/chairs を除外。RFC 番号と draft 名は `name` 直引き |
| inspirehep | `inspirehep.net/api/literature` | 不要 | `fields=` で限定。DOI を出典 URL に使う。CC0 |
| oeis | `oeis.org/search` | 不要 | A 番号は `number`（`id` は旧 M/N 識別子）。0 件は JSON `null`。CC BY-SA 4.0 |
| hfhub | `huggingface.co/api/models` | 不要 | `sort=downloads`。ライセンスはタグ（`license:`）から取る |
| hn | `hn.algolia.com/api/v1/search` | 不要 | 帰属表示必須（Algolia）。10,000 req/h/IP。リンク投稿は本文が無く metadata_only |
| swh | `archive.softwareheritage.org/api/1/origin/search/{pattern}/` | 不要 | API利用規約: ポイントアクセス可・大量抽出不可。origin を `structured_record` で返す |
| librariesio | `libraries.io/api/search` | **FREEAGENT_LIBRARIESIO_KEY 必須** | 19+ 登録簿の横断検索。60 req/min。説明は登録者の自己申告 |
| ia | `archive.org/advancedsearch.php` | 不要 | `title:(語 AND 語)` に組み直して引く。**CJK クエリは HTTP を出さずに断る**（実測 10/10）。summary は目録の注記で `summary_kind=structured_record` |

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
- `sources`省略時は`DEFAULT_SOURCES`（従来6種）だけ。追加18種（datacite / openaire / zenodo / ror / doaj / npm / crates / cinii / osv / ietf / inspirehep / oeis / hfhub / hn / swh / librariesio / ia / qiita）は明示指定。重複指定を除き、無効名だけなら全ソースへ送らない。ciniiは`FREEAGENT_CINII_APPID`、librariesioは`FREEAGENT_LIBRARIESIO_KEY`が前提で、未設定ならHTTPを出さずに案内を返す（§6.7 / §6.10）。qiitaは`FREEAGENT_QIITA_TOKEN`があれば1,000回/時（無くても60回/時で動く。未認証では別の問いが1分待ちになる）。**対象は科学（物理・数学・計算機）とプログラミングまでで、医学・生物学は含まない**（§6.9）。
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

### 6.1.1 接続の再利用（§5.2.1・wikidata の api.php）

- `kb_wikidata` は api.php を**最大 3 回直列に呼ぶ**（`wbsearchentities` → `wbgetentities`(claims) →
  `wbgetentities`(labels)）。呼び出しごとに TCP + TLS を張り直すと 3 連続の中央値 **3.732 秒**、
  1 本の接続で通すと **3.096 秒**（**-0.636 秒・17%**。TLS ハンドシェイクが 163〜183ms/回なので
  2 回ぶん ≈ 0.35 秒は確実に消える。実測 2026-10-21・この環境）。
- **共用経路（`kb_http`）には入れない**。`kb_http` はリダイレクト追従・プロキシ env・読み取り途中の
  打ち切りを持つ 24 ソース共通の道で、接続の使い回しを混ぜると影響範囲が一気に広がる。§5.2.1 は
  **接続の再利用だけ**を担う専用の道（`kb_json_keepalive`）にする:
  * リダイレクトは追わない（wikidata の api.php は 200 を返す。実測）
  * プロキシが設定された環境では `kb_json` へ委譲する（`ProxyHandler` の挙動を変えない）
  * `FREEAGENT_KB_KEEPALIVE=0` / `FREEAGENT_KB_POOL_MAX=0` でも `kb_json` へ委譲する（切り分け用）
  * 予算は `_kb_deadline_at()`（§5.8 がワーカースレッドへ載せる親の締め切り）と `KB_TIMEOUT` の小さいほう
- **在庫の接続が失効していたら作り直して 1 回だけやり直す**（持続接続の失効を遮断と混同しない。
  遮断するのは 2 回目も失敗したときだけ＝`kb_http` と同じ扱い）。429 は在庫の接続で再試行し遮断しない。
- 在庫は origin ごとに `FREEAGENT_KB_POOL_MAX` 本（既定 2）まで、空きは `FREEAGENT_KB_POOL_IDLE`
  秒（既定 120）で捨てる。**貸出中は在庫から外す**ので、同時実行が待たされることはない。
- 在庫へ返す条件は「**本文を読み切った**」こと（`will_close` は返さない）。共用経路の読み取りは
  テスト用の「同じ内容が 2 回続いたら終端」という推測を持つが、この推測で止まると**未読を残した
  接続を在庫へ返す**ことになり次のリクエストが壊れる。§5.2.1 の読み取りは `read1` が空を返すか
  `Content-Length` に達するまでしか止めない（chunked の 1.0 MB 応答で終端を確認。実測）。

### 6.2 時間帯別の計測（`scripts/measure_kb.py`）

- 1 巡で全ソースを並列に 1 回ずつ引き、1 ソース 1 行を `FREEAGENT_KB_LATENCY_PATH`（既定
  `FREEAGENT_STATE_DIR/kb_latency.jsonl`・最大 20,000 行・超えたら古い行から tmp + `os.replace` で捨てる）へ追記。
  記録は `ts` / `hour`（日本時間）/ `source` / `elapsed_s` / `ok` / `items` / `error`（160 字）/ `mailto` /
  `openalex_key` / `cinii_appid` / `env_failure` に加え `datacite_kind` / `summaries`（本文がある件数）を記録し、**タイトル・本文は残さない**。
  `--sources`で対象、`--datacite-kind`でモードを指定。省略時は対応24ソースを測る（cinii / librariesio はキー未設定なら外す・§6.7 / §6.10）。論文キーワードだと空振りするソース（ror / npm / crates / librariesio / osv / ietf / hn / swh / ia / qiita / inspirehep / oeis / hfhub）は `SOURCE_QUERIES` の**分野の合う問い**を巡回し、空振りと障害を混同しない。巡回ごとにキャッシュとホスト遮断記憶を消すが、プロセス内レート制御は消さない。
- 全ソースが接続系の失敗なら `env_failure=true`（こちらのネットワーク障害）とし、`--report` の集計から外す。
- `--schedule N` は Windows のタスク（`pythonw`＝窓を出さない）を 1 時間おきに登録し、残り回数を
  `kb_latency.jsonl.schedule.json` で数えて、最後の 1 回でタスクを自分で消す（`/ED` `/ET` は HOURLY との
  組み合わせで意味が曖昧なので使わない）。`schtasks` の出力はコンソールのコードページ（`oem`）で読む。

### 6.3 任意ソースと代替の契約（§5.9〜§5.13）

- DataCiteは`datacite_kind=all/arxiv/dataset`。自然語の各語をJSON引用符でエスケープしAND結合、`sort=relevance`を指定。
  arxivは`client-id=arxiv.content`（旧資料の型がTextでも落とさない）、datasetは`resource-type-id=dataset`。
  Abstract種別の実本文だけを採用。キャッシュキーはモード・検索語・件数。年/本文の欠落もキーとして返す。
- OpenAIREはGraph V3の`search`・publication限定。DOI/instance URL/idをURLへ正規化、descriptionsを本文にする。
  メタデータのOpenAIRE CC-BYクレジットと原文licenseを区別。今回は匿名のみで認証枠の拡張は未実装。
- 新規3種は`_kb_new_cached`→既存single-flight/cache。上流の不正型・解析例外はsource付きerror。
  空/不正配列を成功扱いせず、失敗はキャッシュしない。本文が無い正常書誌は`metadata_only=true`。
  lookupには表示するが、既存ソースを含め本文なしの引用は`_kb_has_evidence`でgrounded/evidence注入・agent番号登録から除外する。
  agentの書誌は`bibliography`に分離する。生成で本文を埋めない。
- `_kb_rate_acquire`はホスト単位にLock内で採番。待機・未来の予約をせず、アクセスできない場合はローカルHTTP429相当を返す。
  DataCite0.61秒、OpenAIRE60.1秒。DataCiteのモードは同じ予算。
  メモリ内・1プロセスの制御であり、他プロセス/IP上の他アプリとは共有しない。提供元のRetry-After/遮断記憶も従来通り。
- 明示許可代替は`_kb_source_result`。arXivの429/5xx/接続系/遮断なら直ちに、応答待ちなら`KB_HEDGE_DELAY`（既定2秒）後にDataCiteへ。
  設定は`FREEAGENT_KB_HEDGE_DELAY`（非有限は2へ、0.05〜30秒）。全体締切は延長しない。
- 代替は**残り時間が`KB_HEDGE_MIN_REMAINING`（既定0.05秒、`FREEAGENT_KB_HEDGE_MIN_REMAINING`で変更）以上**のときだけ始める。
  間に合わない取得を投げないため。**実測（v0.7.3）**: 締切0.005秒で20回に1回、ヘッジの待ちが締切と同刻に解けて
  `now >= end` を数ミリ秒すり抜け、`knowledge_lookup`が返った後にDataCite代替が走り出した（CIのwindows-latest/3.12が
  この経路で落ちていた）。下限を置くと再現プローブ（同じ強制条件）で0回になり、時間が残っている通常のヘッジは不変。
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

### 6.5 第4段階 DOAJ / npm / crates.io（§5.16）

- 3種とも`SOURCES`/`KB_BACKENDS`へ追加するが`DEFAULT_SOURCES`は変更しない（明示指定のみ）。
- **DOAJ**は`/api/search/articles/{query}`のパス埋め込み検索。`_literal_search`で各語を引用してAND結合し、
  パスへは`urllib.parse.quote(…, safe="")`でエスケープする。DOIがあれば`https://doi.org/`を正規URLにする
  （既存の引用統合がDOI別名で束ねられる）。抄録は実テキストだけを本文根拠にし、欠落は`metadata_only`。
  記事メタデータはCC0（https://doaj.org/terms/ ）。公式レートは全ルート2req/s → プロセス内予算0.51秒。
- **npm / crates**はパッケージレジストリ検索で、**説明文は登録者の自己申告**（審査・品質評価ではない）。
  `summary_kind=registry_description`で論文抄録と区別する。npmはOpen Source Termsが「公式Public APIによる
  複製」を明示許可（1.0秒間隔に自制）。crates.ioはCrawler Policyが1req/s＋識別UAを要求（1.01秒間隔、
  UAは`KB_USER_AGENT`で連絡先入り）。`downloads`はint型のみ採用し、文字列等は黙って落とす。
- 採用根拠（2026-10-02実測）: DOAJ 0.38〜0.45秒・npm 0.31〜0.58秒・crates 0.75〜0.78秒、いずれも抄録/説明つき。
  DOAJはOpenAlex匿名検索の503/429時の科学系キーワード検索の受け皿、npm/cratesはGitHub未認証10req/minの
  パッケージ系クエリの分流先になる。
- 見送りの記録: PyPI / deps.devはキーワード検索APIが無い（名前直引きのみ）。PubMed / bioRxivは医学・生物学で
  対象外（§5.19）。PLOSは10req/min＋5秒待機推奨が8秒締切と不整合。HALは検索APIは公開だがOAI条項の
  非商用制限があり条件曖昧→保留群。Semantic Scholarは匿名が429頻発（実測3/3→再測でも429）でキー運用のみ
  将来候補。OpenCitationsはキーワード検索が無くDOI引用数の補完用途のみ。
- `measure_kb.py`はnpm/cratesにパッケージ系の問い（json schema validator等）を巡回させ、論文キーワードの
  空振りと障害を混同しない（RORの機関名巡回と同じ考え方）。

### 6.6 保留候補の許諾確認（一次情報・2026-10-04）

規約 29/30 の基準（検索できる公認 API か・規約が参照する AUP/bot/AI 制限・既存ソースとの重複・8 秒締切との整合）で
保留候補を**公式文書**で再確認した。**結論: J-STAGE / CiNii / CORE は登録しない**（新規実装なし）。

- **J-STAGE WebAPI**（[利用規約](https://www.jstage.jst.go.jp/static/pages/WebAPI/-char/ja)・
  [マニュアル](https://www.jstage.jst.go.jp/static/files/ja/manual_api.pdf)）— 非営利目的は利用申請不要（第 2 条 1）だが、
  条件が実装義務として重い: ①「Powered by J-STAGE」クレジットと J-STAGE へのリンク表示（第 9 条）②機械可読な状態で
  **24 時間以上の保存・キャッシュ禁止**（第 3 条 5）③**主たるコンテンツとして組み込まない**（第 3 条 3）④利用者運営
  サービスの利用者に**本規約の存在を表示し遵守させる**（第 3 条 1）⑤常に最新を表示（第 3 条 6）。商用は有償/無償を
  問わず申請＋承認（第 2 条 2）。加えて**内容が Crossref と重複する**: 論文検索結果取得の応答項目は論文タイトル
  （en/ja）・著者・資料名・ISSN・巻号頁・発行年・DOI・JOI のみで**抄録を返さない**（マニュアル 4.レスポンス
  フォーマット）。実測でも J-STAGE 掲載の日本語論文（10.14817/jlak.2016.49.25・10.1299/jsmemag.102.969_463_1 等）は
  `freeagent_lookup` の crossref で取得できる。→ **保留継続**（規約表示・キャッシュ禁止の実装義務に対し、追加で
  得られるのが書誌だけ）。
- **CiNii**（[API 利用登録](https://support.nii.ac.jp/ja/cinii/api/developer)・
  [ウェブ API 利用細則](https://support.nii.ac.jp/sites/default/files/cinii/webapi-term.pdf)・
  [利用規程](https://support.nii.ac.jp/sites/default/files/cinii/content_services-term_1.pdf)）— ウェブ API 利用細則
  第 3 条は利用目的を「**自己の学術研究**」または「**非営利の情報利活用**」に限り、それ以外は**書面で事前申請し承認を
  得る**ことを要求する。第 4 条は申請の**審査・承認**と API キー発行、第 5 条は **API キーの第三者貸与・譲渡の禁止**、
  第 6 条 1 は**再許諾の禁止**。利用規程 第 4 条は「自己の学術研究のため、又は学術研究の発展に資するため…**それ以外の
  目的のために使用しない**」、第 5 条 2 は複製物を**他人が共同使用できるサーバ等に保管して利用すること**を禁じる。
  → 汎用アシスタントへの組み込みは利用目的の限定と衝突しうるため、**プロジェクトの既定ソースにはしない**。
  ただし利用者自身が承認を得て自分の appid を設定する場合は動かせるようにした（§5.17 / §6.7。
  **プロジェクトがキーを同梱・共有することは第5条でできない**）。利用目的に当たるかの判断は利用者に委ねる。
- **CORE**（[Terms & Conditions](https://core.ac.uk/terms)）— §3 は、CORE データを使う製品・サービス・ソフトウェアが
  「**API・推薦・検索・探索システム・分析ダッシュボード等の CORE の既存サービスが提供する機能に関わる**」場合、
  無料ライセンスの適格性にかかわらず **CORE への連絡が必要**と明記し、API の利用にはライセンスを要するとする。
  → 本サーバーの lookup は検索・探索機能そのものなので**登録しない**（連絡して許諾を得るまで）。
- **Semantic Scholar** — 匿名は 429（実測 3/3）。キーは申請フォーム経由で、**API の利用条件が明示された文書を公開
  ページ / FAQ で確認できない**（FAQ はエラーコードのみ）。→ 条件が曖昧なため**保留継続**（規約 30）。
- **HAL** — 非商用条項が曖昧なまま（変更なし）。
- 副産物の実測: Crossref は `mailto` 無しの連続リクエストで **429** を返す（単発 3 回で発生）。`FREEAGENT_MAILTO` を
  設定すると polite pool に入る（既存実装）。

### 6.7 第5段階 CiNii Research（§5.17・appid 必須・明示指定のみ）

- `cinii`を`SOURCES`/`KB_BACKENDS`へ追加するが`DEFAULT_SOURCES`は変更しない（明示指定のみ）。
- **利用者自身の appid が前提**。`FREEAGENT_CINII_APPID`未設定なら**HTTPを一切出さずに**登録先と設定方法を
  案内する（実測: appid 無しでも 200 が返るが、規約は登録を要求している）。プロジェクトは appid を同梱・共有
  しない（ウェブAPI利用細則 第5条が第三者への貸与・譲渡を禁じる）。**利用目的に当たるかは利用者が判断する**
  （検索内容の判断は利用者に信託し、サーバーは代わりに同意しない）。
- エンドポイントは`https://cir.nii.ac.jp/opensearch/all`（`q`＝各語を引用してAND結合・`format=json`・`count`・
  `appid`）。応答はJSON-LDで、`items[]`の`title`/`dc:creator`/`dc:publisher`/`dc:type`/`prism:publicationName`/
  `prism:publicationDate`/`prism:startingPage`/`dc:identifier`（`cir:NAID`・`cir:NCID`・`cir:ISBN`・`cir:DOI`）/
  `link.@id`を書誌として正規化する。`dc:creator`は文字列のときもある。`dc:identifier`の辞書キーや文字列の
  各文字を値にしない。DOIがあれば引用URLは`https://doi.org/`を優先し、CiNiiのページURLは`cir_uri`に残す。
- **抄録を返さない**（実測: 応答項目にabstract/descriptionが無い）。全件`metadata_only=true`とし、
  `_kb_has_evidence`・本文注入番号の対象にしない（利用規程 第5条2の複製・編集の制限に触れないため）。
- レートは公表が無いためプロセス内1ホスト2.0秒間隔に自制する（同 第6条3の短時間大量アクセス回避）。
  他アプリ・他IPとの共有ではない（§5.11と同じ但し書き）。
- **実経路の計測は未完了**: 応答形式は実応答（appid無しで200が返る1回の確認）から写した実データで
  `tests/test_knowledge_cinii.py`（10件）が検証するが、appidを使ったstdio・レイテンシの実測は
  **appidを持つ利用者が**`probe_knowledge_stdio.py --sources cinii`と`measure_kb.py --sources cinii`で行う
  （どちらもappid未設定なら検査・計測せずに外す）。

### 6.8 日本のオープンデータ候補の評価（2026-10-04・保留。実装なし）

知識ソースとして使えるかの**評価だけ**を行い、登録はしていない（保留）。判定は規約 29/30 と同じ基準（検索できる
公認 API か・キー要否・規約が参照する先まで含めた制限・既存ソースとの重複・8 秒締切との整合・条件の曖昧さ）。

**採用候補（未実装）**

- **e-Gov データポータル（= DATA GO JP）** — キー不要。CKAN 標準の `package_search`。実測: `q=防災` が 200・
  **0.46 秒**・3,759 件（`result.results[]` に title / notes / tags / organization）。規約は**公共データ利用規約
  （PDL1.0）**（出典記載義務・加工時は加工した旨と主体を明示・商用可）。`https://www.data.go.jp/` は
  `https://data.e-gov.go.jp/info/ja` へ**リダイレクト**し、旧 CKAN パスも生きていて 18,143 件を返す
  （**DATA GO JP と e-Gov データポータルは同じカタログ**）。返るのは発行主体が書いた説明なので、npm/crates と
  同じ「登録簿の自己申告」型として扱い `summary_kind` を分ける。
- **データカタログ横断検索システム（search.ckan.jp・NII）** — キー不要。CKAN 互換 API。実測:
  `backend/api/package_search?q=防災` が 200・**0.1 秒**・**4,185 件**、`xckan_site_name` のファセットで
  DATA GO JP 1,490 / G 空間情報センター 1,310 / BODIK ODCS 704 / 東京都 270 / 学術機関リポジトリ 82 と
  **省庁・自治体を横断**する。保証フィールドは `xckan_title` / `xckan_site_name` / `xckan_site_url` /
  `xckan_last_updated` / `xckan_original_id` の 5 つ。**規約が API ドキュメントに見当たらない**ので登録前に
  確認する（規約 29）。自治体カタログに検索 API が無い問題をこれが解消する。
- **e-Stat** — **appid 必須**（利用登録・1 人 3 個まで・譲渡貸与禁止・クレジット表示義務・短時間大量アクセス禁止）。
  実測: appId 無し / dummy とも `STATUS 100「認証に失敗しました」`＝ハードゲート。`getStatsList` は `searchWord`
  （AND/OR/NOT 可）でキーワード検索でき、表題・府省・統計名・周期・更新日・ランディングページを返す。
  **統計表メタデータまでに限定**する（`getStatsData` は最大 10 万行で、文脈注入に不向き・誤読を招く）。
  cinii と同じ「キー必須の明示ソース」の型（§5.17 / §6.7）を流用できる。

**見送り（登録しない）**

- **東京都オープンデータAPI** — キー不要・CC BY だが `service.api.metro.tokyo.lg.jp/api/{apiId}/json` と
  **apiId を知らないと叩けない**。検索は HTML の SPA で、機械可読な検索 API は見つからない（`/spec/api/search`
  等は 404）。`query` を投げる知識ソースにできない（PyPI と同じ理由）。公共施設・バリアフリーは
  tokyo-transit MCP（`gov_facility_search_support`）と役割が重なる。
- **opendataapi.jp** — 個人開発の α 版（2025/01 公開）。タグ別に 0〜5 件の小規模。将来トークン管理・有償化を予定。
  **公認 API ではない**ので規約 29/30 の「条件が曖昧なものは登録しない」に該当。
- **awesome-japan-opendata の「プラットフォーム/ソフトウェア」節** — データではなくポータル構築の道具。
  **BODIK ODCS** は 18,457 件を掲げるが実測 `odcs.bodik.jp/api/3/action/package_search` は **404**
  （「CKAN/WordPress ベース」でも API は非公開）。**Open data stack** / **data eye** は有償、**odp.jig.jp** は
  自治体向け有償（基本 150 万円/年）でデータの権利者は自治体、**LinkData** はユーザー投稿型。**CKAN** はソフトで
  データではない。
- 含意: CKAN は**共通 API** なので、実装を base URL 可変にすれば 1 つの実装で複数のポータルに展開できる
  （規約と権利者はポータルごとに個別判断）。

### 6.9 第6段階ソースの採用根拠（§5.18 / §5.19・2026-10-05 の方針変更を反映）

2026-10-05 に約 40 エンドポイントを無認証で実プローブし、利用条件を一次情報で確認した。
**同日の方針変更で、対象を「科学（物理・数学・計算機）＋プログラミング」に絞り、医学・生物学は
対象外とした**。そのため第6段階で採った生命・医学系ソース（europepmc / uniprot / chembl / pdb /
quickgo / reactome / clinicaltrials / openfda / gbif）は**削除**した（実装は git 履歴に残る）。
既定 off の明示指定で残るのは **osv / ietf / inspirehep / oeis / hfhub の 5 種**で、
`DEFAULT_SOURCES` は 6 種のまま変えていない。

- **採用（プログラミング・標準）**: osv / ietf。**採用（科学）**: inspirehep（素粒子物理・CC0）/
  oeis（整数列・CC BY-SA 4.0・出典表示必須）/ hfhub（ML モデル。計算機科学に含める）。
- **削除（方針変更）**: europepmc / uniprot / chembl / pdb / quickgo / reactome / clinicaltrials /
  openfda / gbif。生命・医学系を外したことで、EBI の同一ホスト同居（chembl / pdb / quickgo）を
  前提としたサービス単位の予算キー `_ebi_key` も不要になり削除した（`_kb_new_json` の `budget_key`
  自体は OSV が使うため残す）。
- **見送りと理由**: Stack Exchange（AUP が生成AI/チャットボットの開発・試験目的の自動取得に**事前の
  書面許諾**を要求。実測 200 でも許諾が確認できない。規約 29）/ NVD（キー無しは 6 秒間隔の推奨で、
  OSV と役割が重複）/ Repology（検索結果が「プロジェクト → 全リポジトリのパッケージ」の塊で、
  実測 133 プロジェクト・`project/openssl` は 1008 件。8 秒締切と文脈予算に不向き）/ MDN（公開された
  API ではなくサイト内部のエンドポイント）/ GitLab・Codeberg（GitHub と役割が重複）/ Maven Central
  （新 ToS の階層型アクセスモデルが「商用インフラ利用」を事前許諾なしに禁止）/ PubChem・
  AlphaFold DB・Ensembl・STRING（名前直引きでキーワード検索 API が無い）/ zbMATH（実測で TLS 証明書
  検証に失敗）/ SIMBAD（実測 17.95 秒）/ SciELO（Bunny シールドの 403 チャレンジ）/ OpenML（Hugging
  Face Hub と重複）/ NASA Exoplanet（ADQL を組み立てる必要があり注入面が増える）。
- **予算キーの分離**: §5.11 のレート予算は**ホスト単位**なので、1 回の取得で同じホストの複数
  エンドポイントを叩く OSV は `_kb_new_json(..., budget_key=...)` で**予算だけサービス単位に分け**、
  遮断の記憶は netloc のまま（ホスト単位）にする。
- **本文根拠の区別**: 構造化レコードしか無い hfhub は、記録の値を 1 行にまとめて
  `summary_kind=structured_record` を付ける（論文抄録と混同させない）。cinii と同じく、書誌のみの
  ソースは `metadata_only` のままにする。
- **IETF の検索は全文検索ではない**: `title__contains` は AND 不可（実測: 2 語指定で 0 件）。RFC 番号
  （`RFC 9110` / `rfc9110` / `9110`）と `draft-...` は `name` で直引きし、それ以外は最長語 1 つで引いて
  全語がタイトルに載るものを優先する。取りこぼす場合があることを README に明記する。
- **実測（2026-10-05・実 stdio 経路・各 2 件）**: osv 0.92 秒 / ietf 0.63 秒 / inspirehep 0.86 秒 /
  oeis 1.29 秒 / hfhub 0.28 秒。一時点の計測なので、時間帯別の確認は
  `scripts/measure_kb.py --sources ...` で行う。

### 6.10 第7段階ソースの採用根拠（§5.20・2026-10-05）

「世界中のプログラミング関連 API」を無認証でプローブし、規約 32 の 2 条件（検索できる公認 API・
利用条件が明確）で評価した。**採用 3 種**はすべて既定 off の明示指定。

- **hn（Hacker News / Algolia 検索）** — 実務者の議論・Q&A。実測 0.7 秒・キー不要。Algolia HN Search
  API は「free for use with attribution（"Search by Algolia" の帰属表示）」で 10,000 req/hour/IP が上限。
  Stack Exchange が AUP（生成AI/チャットボットの自動取得に事前の書面許諾）で使えないため、議論面の
  空白をこれが埋める。`tags=story` のリンク投稿は本文が無く `metadata_only`。引用 URL は HN の
  アイテム URL とし、記事 URL は `extra.article_url` に残す（証拠は HN の投稿であって記事ではない）。
- **swh（Software Heritage）** — 公開ソースコードの長期アーカイブ（Inria）。`/api/1/origin/search/<pattern>/`
  がキー不要で実測 1.42 秒。API 利用規約は「ポイント単位のアクセスは自由・大量抽出は不可」で、
  メタデータ（来歴・種別）は事実情報として自由に使える。コード本文は取得せず、origin（リポジトリ）の
  存在・URL・取得方式を `summary_kind=structured_record` で返す（GitHub code 検索がトークン必須なことの補完）。
- **librariesio（Libraries.io）** — 19+ のパッケージ登録簿を横断するキーワード検索。**利用者自身の
  無料 API キーが前提**（60 req/min）。`FREEAGENT_LIBRARIESIO_KEY` 未設定なら HTTP を出さずに登録先を
  案内する（cinii と同じ型。§6.2 の契約）。説明は登録者の自己申告（`summary_kind=registry_description`）。
  プロジェクトはキーを同梱・共有しない。
- **見送り（実測）**: Maven Central（新 ToS が商用インフラ利用を禁止）/ Docker Hub（商用・abuse 条項が
  曖昧）/ grep.app（429 チャレンジ）/ searchcode.com（404）/ Gitee（11〜17 秒・トークン要）/ JSR・PyPI・
  pkg.go.dev（キーワード検索 API 無し）/ Homebrew・Go module index（一括ダンプ）/ Zenn（非公式 API）。
  **将来候補（キー不要で使えるが、集約の librariesio を採る場合は schema コストに見合わない）**:
  Artifact Hub / MetaCPAN / RubyGems / Packagist / pub.dev / Hex / NuGet / Debian Sources。
- **実測（2026-10-05・実 stdio 経路・各 2 件）**: hn 0.47〜0.5 秒 / swh 1.2 秒。librariesio はキー未設定
  なら検査・計測せずに外す（`probe_knowledge_stdio.py` / `measure_kb.py`）。

### 6.11 第8段階ソースの採用根拠（§5.21・2026-10-13）

Internet Archive を**メタデータ検索だけ**明示指定ソースとして採った。既定6は不変で、対象範囲
（§6.9 の科学・プログラミング）も変えず、「当時の一次資料（計算機マニュアル・書籍・ソフト）」を補う。

- **採った理由（実測）**: `advancedsearch.php` はキー不要で 0.44〜0.70 秒（5 回）。bitsavers（計算機
  マニュアル 91,403 件）や softwarelibrary を引ける。`title:(COBOL)` は IBM 360 の COBOL マニュアルを、
  `title:(FORTRAN AND manual)` は CENFOR / DEC FORTRAN IV / TRS-80 Fortran-80 を返した。
- **クエリの組み直しが必須**: 素の `q=FORTRAN manual` の 1 位は無関係な GitHub ミラーで、
  `sort[]=downloads desc` 単独も話題と無関係な人気作を混ぜた（実測）。よって `title:(語 AND 語)` に組み直す。
- **CJK クエリは HTTP を出さずに断る**: `日本語` / `京都` / `量子力学` / `title:日本語` / `日本 AND 歴史` は
  10/10 で `{"error": "... [QUERY_NOT_READY] Query has not been finalized"}` を返し、同じ 10 回で英語は
  10/10 成功した。1 回だけ成功した観測があるため恒久的な不可とは断定せず、**翻訳はサーバーでやらない**
  （依存ゼロ・LLM 非介在を守る）。英語の語に直すのは呼び出し側の仕事で、`sources` の説明に明記する（規約 5）。
- **証拠の種類**: `summary_kind=structured_record`。IA の `description` は目録・アップロード者の注記で
  抄録ではない（実測: 「Cover title Includes bibliographical references 31」）。本文・ファイルは取得しない。
- **見送り（実測）**: 全文検索 `be-api.us.archive.org/ia-pub-fts-api` は集計を毎回計算するため
  5.9〜25.1 秒（8 件中 3 件が 8 秒超）で日本語は `total=0`（§6.1 の締め切りに載らない）。CDX は 5.4〜30 秒で
  URL 起点の API。SPN2 / S3 アップロードは書き込みで利用者キーが前提（読み取り専用の性格を崩さない）。
  `inside.php`（本の中検索）は 1.5 秒だが OCR を持つアイテムに限られ、`_abbyy.gz` を持つアイテムでも
  0 件だったため主要経路にしない。
- **利用条件**: `robots.txt` の Disallow は `/control/` と `/report/` のみで `/advancedsearch.php` と
  `/metadata/` は許可。IA 自身が「Bots, LLMs, and Automated Access」で、AI エージェントを含む自動アクセスに
  説明的な User-Agent・間隔・429/`Retry-After` の尊重を求めている。**検索系の数値レートは非公表**なので
  1 req/s で自粛し、`archive.org/advancedsearch` は metadata API と別の予算キーにする（規約 32）。
- **実測（2026-10-13・実 stdio 経路・2 件）**: ia 0.8 秒（`FORTRAN manual`）。ツール一覧JSONは
  14,399 → **14,654 文字**（+255・ツール数は 11 のまま）。

### 6.12 第9段階ソースの採用根拠（§5.22・2026-10-15）

Qiita API v2 を**明示指定ソース**として採った。既定6は不変で、対象範囲（§6.9 の科学・プログラミング）も
変えず、**23 ソース中に 1 つも無かった「日本語のプログラミング記事」**を補う（cinii は日本語だが学術文献）。

- **採った理由（実測）**: `/api/v2/items?query=` はキー不要で 0.18〜0.36 秒（6 サンプル、8 秒の締め切りに
  超過 0）。絞り込み構文がそのまま使える（`tag:Python` / `user:foo` / `created:>2026-10-01` / `stocks:>50`）。
  日本語の語もそのまま引ける（実測 0.30 秒）。23 ソース中の空白を埋めるので既存ソースと重複しない。
- **間隔はトークンの有無で 2 定数**（github と同じ型）: 公称レートは認証 1,000 回/時（≒16.7/分 → 3.6 秒）、
  未認証 60 回/時（= 1/分 → 60.0 秒）。未認証では同じ問いの再実行だけがキャッシュで即返り（実測 0.000 秒）、
  別の問いはローカルの `HTTP 429: ローカルのアクセス間隔制御` として見える（隠さない・規約 8）。これは
  Qiita の実クォータそのもので、キー未設定を失敗として扱わない（規約 29 の「キー必須ソース」とは型が違う）。
- **本文は `rendered_body`（HTML）を使い `_plain_text` に通す**: `body`（Markdown）だと `_plain_text` は
  `<[^>]+>` しか落とさないので画像記法が引用文に残る（実測: `body` は画像 1・リンク 1 が残存、
  `rendered_body` は 0/0 で本文だけ）。
- **証拠の種類**: `summary_kind=discussion`（HN と同じユーザー生成の解説。査読論文ではない）。引用 URL は
  記事 URL、`identifier` は記事 ID。タグ・Likes・Stock・コメント数・更新日時は `extra` に置く。
- **見送り（実測）**: Reddit（未認証は全経路 403 `Blocked`。Responsible Builder Policy が事前承認と
  AI 目的の利用を求める）/ openBD（キーワード検索 API が無く ISBN 直引きのみ。`isbn=abc` は `[null]`）/
  Botipedia（公開 API が無い。`/api` 等はすべて同一 HTML の catch-all）/ Free Dictionary API（オリジン停止で
  522。12 語中 5 語だけが古い CDN キャッシュで 200）。
- **実測（2026-10-15・実 stdio 経路・2 件）**: qiita 0.3 秒（`tag:Python`）/ `measure_kb.py` 0.28 秒。
  ツール一覧JSONは 14,654 → **14,882 文字**（+228・ツール数は 11 のまま）。stdio 往復は 0.2605 秒（7 回中央値）。

### 6.13 §5.1 Wikipedia の完全一致昇格（2026-10-17・v0.6.1）

**症状（実測）**: ja.wikipedia で `大規模言語モデル` を素の検索（`generator=search`）にかけると、返るのは
マークアップ言語 / 機械翻訳 / プログラミング言語 で、目的の記事は **20 件中 19 位**だった。既定の
`gsrlimit=3` では窓の外に落ちるため、`freeagent_lookup` / `freeagent_grounded` の根拠が丸ごと無関係になる。
記事は索引にある（`pageid:4769449` が一致、`intitle:大規模言語モデル` は 1 位）。**サーバーの URL 組み立ては
正常で、提供元の順位の問題**。`srwhat=title` はどのクエリでも 0 件を返す（この版では機能していない）。

**対処**: 検索と**並列**に `titles=<query>&redirects=1&prop=extracts` を投げ、**タイトル完全一致**が存在し、
中身（extract）が空でなければ先頭へ昇格する（検索側に同じタイトルがあれば重複を落とす）。

- **昇格は完全一致だけ**。「タイトルに含む」で昇格させると `量子コンピュータ` の 2 位に
  「イオントラップ型量子コンピュータ」が上がり `コンピュータ` を追い出す（実測）ので採らない。
- **中身が空の extract は昇格させない**: `大規模言語モデルの一覧` は記事があるのに intro が 0 文字で、
  そのまま昇格させると空の項目が先頭に来る（実測）。
- **`missing` / `invalid` はキーの有無で判定する**（値は空文字で返るので、値の真偽では見分けられない）。
- **演算子・引用符を含む問いと 80 文字超では lookup を投げない**（`intitle:` をタイトルとして引くと
  無関係な記事に当たる）。このときは従来どおり 1 リクエスト。
- **並列にする理由（実測）**: 逐次 1.15 秒 → 並列 0.58 秒。wikipedia は `kb_json` 経由でホスト間隔の制御が
  無く、同一ホストへの 2 並列でローカル 429 にならないことも確認済み（§5.11 の予算キーは `_kb_new_json` の話）。
- **片方の失敗で全体を落とさない**: lookup が落ちても検索結果はそのまま返す（`_wiki_exact_hit` が None を返す）。
- **実測（修正後・実 API・キャッシュなし）**: `大規模言語モデル` 636ms で先頭が目的記事（量子コンピュータ 588ms /
  強化学習 585ms / 東京 646ms / en 725ms はいずれも順位・所要とも変化なし）。2 回目はキャッシュで 0ms。
- 回帰テスト: `tests/test_audit_regressions.py` の wikipedia 群（昇格・空 extract・演算子・lookup 失敗・
  リクエスト本数）。

### 6.14 言語の自動選択と関連性の測定（2026-10-17・v0.6.2）

**症状（実測）**: `lang` の既定が `ja` 固定だったため、**ASCII の問いを ja 索引に投げると無関係な記事が
根拠になる**。`Transformer` は ja で「トランスフォーマー (曖昧さ回避) / ASUS Eee Pad TF101 / TF201」、
en では「Transformer / Transformer (deep learning) / Generative pre-trained transformer」。
`diffusion model` も ja は「フィジカル・レビュー / 拡散律速凝集」で en は「Diffusion model」。
逆に日本語の問いは ja が正しい（`強化学習` は ja 1 位、en は「安倍晋三暗殺」）。

**対処**: `_kb_auto_lang(query, lang)` — 明示があればそれを尊重し、無ければ**問いに CJK（かな・漢字・ハングル）
を含む→ja、それ以外→en**。判定は正規表現 1 本（決定的・依存ゼロ）。`opts["lang"]` の既定を「空」にし、
`kb_wikipedia` の既定も空にした（`KB_BACKENDS["wikipedia"]` は `opts.get("lang") or ""` を渡す）。

- **wikidata には適用しない**（実測: `Transformer` は ja の 2 件目に「トランスフォーマー（機械学習アーキテクチャ）」
  が出るが、en は変圧器とメディア作品。ja のままの方が当たる）。`KB_BACKENDS["wikidata"]` は `or "ja"` のまま。
- **`lang` の description を更新**（規約 5: モデルの唯一の窓口）。「省略時は問いから自動。英語の語は自動で en」。
- 未指定は「空文字」で伝える（`as_str(lang, "")`）。`as_str(None, "ja")` のままだと自動判定に到達しない。

**測定器（なぜ必要か）**: `measure_kb.py` は**所要時間**、`measure_adoption.py` は**自発利用率**を測る。
**「速いが無関係」はどちらにも映らない**（実測: ja.wikipedia は 0.6 秒で正常に見えていた）。
`scripts/measure_relevance.py` は固定クエリ集合 ×「トップ 1 に含まれるべき語」で的中を数え、JSONL に追記する。

- **同じ測定器を修正前後で回して差が出ることを確認**（HEAD を一時展開し、新しい測定器をそこへコピーして実行）:
  wikipedia **3/6（60%）→ 5/5（100%）**、全体 8/10 → 10/10。
- 外れても**失敗にしない**（上流の順位は変わる。実行する検証であってゲートではない）。
- 期待を書かないケース（`GRPO`、wikidata の `Transformer`）は**記録のみ**にする。wikidata の `Transformer` は
  1 位が変圧器で、**言語を変えても直らない**（en でも電気機器が 1 位）ため、曖昧語は既知の限界として記録する。
- 記録先は `FREEAGENT_RELEVANCE_PATH`（既定 `<state_dir>/relevance.jsonl`）。モデルは呼ばない・キー不要のソースだけ。

### 6.15 健全性監査の findings と v0.8.1 の修正（2026-10-19）

健全性監査で 15 件の不都合を実測で再現した。うち 2 件は重大（実害あり）、5 件は機能不具合、
8 件は記述の陳腐化・規約違反だった。**すべて v0.8.1 で修正**し、`tests/test_audit_v081.py`
（20 件）に退行を固定した。

**重大 1 — 知識取得の読み取りに壁時計の上限が無かった**（§5.8 / 規約 27）。
`urlopen(timeout=…)` の上限は「ソケット 1 回の待ち」に対するもので、**リクエスト全体の壁時計制限
ではない**。1 バイトずつ drip する提供元では読みが終わるまで何分も待った（実測: ローカル drip
サーバで **98.0 秒** = `KB_TIMEOUT`（既定 20 秒）の 5 倍、zenodo 実測で **122.75 秒**）。
一方 `_kb_gather` の締め切り（8 秒）は**待たずに返す**ので利用者には影響しなかったが、
裏の取得スレッド（daemon）が 2 分以上動き続け、常駐 HTTP ではスレッドを占有していた。
→ `_kb_read_within(resp, deadline)` を追加し、**残りの壁時計予算**を見ながら読む。
  終端判定は `Content-Length`（`read1` は 1 バイトずつしか返さないことがある）で行い、
  予算超過は `TimeoutError` として失敗扱いにする（ホストを短く遮断）。
  修正後: drip 応答は **20.0 秒**で打ち切り、通常ソースは 0.3〜1.7 秒のまま（退行なし）。

**重大 2 — 常駐 HTTP に `Origin` / `Host` の検証が無かった**（§8.2 / 規約 38）。
MCP の StreamableHTTP は `Origin` ヘッダの検証を要求（仕様の MUST）するが、素通しだった
（実測: `Origin: https://evil.example` と `Host: evil.example` の両方で **200**）。
ブラウザで開いた別ページから `http://127.0.0.1:8790/mcp` へ JSON-RPC を送れる状態だった
（`FREEAGENT_HTTP_TOKEN` 未設定なら認証ガードも無い）。
→ `_http_origin_ok` / `_http_host_ok` を追加。`Origin` が無い CLI は通し、
  待ち受ホストと一致するオリジンだけ許可（`Origin: null` は拒否）、`Host` も待ち受ホストと
  一致を要求する。待受が `0.0.0.0` のときは制限しない（LAN 公開のとき）。
  修正後: 他サイト・`Origin: null`・偽装 `Host` は **403**、CLI と同一オリジンは **200**。

**機能不具合 5 件**:

- **計画の差し替えで達成済みと同一性が消えた**（§2.7）。`plan=["A","B"]` で A 完了 →
  `plan=["B"]` にすると A の `done` が消え、**id 1 が別物 B に付け替わっていた**。
  → `_plan_merge` を追加。同じ文面は id と `done_at` を引き継ぎ、新しい文面には**未使用の id**を
  割り当て、抜けた項目は**廃止**（`plan_retired`）として残す。廃止項目も `subgoal` の参照先と
  して有効なので、**参照が宙に浮かない**。
- **`next_thought_needed: null` が終端になっていた**。`as_flag(None)` は False なので、
  クライアントが null を送っただけで台帳が暗黙に終端した（実測: null → False / キー無し → True）。
  → `_think_needed_flag` を追加し、**明示された null は未指定扱い**（既定 True）にする。
- **終端なのに `next_call` が出ていた**。「続けるか」を False にしておきながら次の思考を誘導していた。
  → 終端時は `next_call` を `None` にする。
- **不正な `sources` 名が content に出なかった**。`structuredContent.unknown_sources` には
  あるのに人間向け表示に無いので、typo が黙って捨てられていた。
  → `freeagent_lookup` / `freeagent_grounded` の content と、`_render_body` のエラー分岐に表示する。
- **`freeagent_models` の返り値キーがモードで違った**（引数なし → `free_models` のみ /
  `query` 付き → `models` のみ）。→ **常に両方**を返す（差分は `query` だけ）。

**記述の陳腐化・規約違反 8 件**（規約 5 / SPEC §7）:

- description の 3 マーカー（【使う条件】【使わない条件】【競合より優先】）が **7/11 ツールで欠落**。
  → `_NOT_USE_DESC` を新設し、**各マーカーの本文を 1 か所**に集約（`_desc_assemble`）。
  【競合より優先】の**重複**（panel / grounded / consult で 2 回）も解消。
- `freeagent_models` の `provider` 説明が 7 つしか無く**実在の `vercel` / `ollama` が無い**。
  → `PROVIDER_ORDER` から生成するようにした（以後プロバイダを足しても自動で追従）。
- `freeagent_grounded` の `sources` 説明が **`query`** を参照していた（実引数は `question`）。
  → `question` に修正。あわせて**本文根拠にならないソース**（crossref / hn）を明記。
- `lang` の説明が「Wikipedia/Wikidata の言語。省略時は問いから自動」と書いていたが、
  **wikidata は `ja` 固定**（実測で ja の方が当たったため。AGENTS 規約 40 / §6.14）。
  → 説明で区別する。
- README のツール一覧JSON文字数が 14,882 のまま（HEAD は 15,870）。
- CI に `scripts/smoke_http.py` が無かった（HTTP 経路の退行が見えない）。
  → CI に追加。

### 6.16 台帳のプロセス間競合と `subgoal` の契約（2026-10-20・v0.8.2）

健全性監査で 2 件の不都合を実測した。**いずれも v0.8.2 で修正**し、`tests/test_audit_v082.py`
（10 件）に退行を固定した。

**重大 1 — 台帳が複数プロセス間で壊れる（last-writer-wins）**（§2.6 / §6.9）。
`thoughts.json` のロックは**プロセス内だけ**（`_THOUGHTS_LOCK` は `threading`）なので、
同じ `FREEAGENT_STATE_DIR` を複数プロセスで共有すると**片方が上書きで他方の思考を消す**。
実測: 4 プロセスを**同じ時刻に**起動して同じ `session_id` へ書き込ませると、5 試行中 3 回で欠落
（失った数 `[3, 2, 1, 0]`）。台帳は `thoughts.json` に 1 本なので、**カード単位の台帳でも同じファイルへ書き込む**。

さらに**読み取りの陳腐化**が別に起きていた: `_ensure_thoughts_loaded` は一度 `True` になると
**二度と `thoughts.json` を読まない**。→ 別プロセス（MCP 子プロセス = KANBAN ワーカーなど）が
足した思考を常駐プロセスから**永久に見えなかった**。→ 常駐プロセスが自分の台帳を保存すると、
**ファイル上からも他プロセスの手が消えた**（実測 True）。

→ 2 つを直した。
1. **プロセス間の排他**（`_file_lock`）: 台帳ファイルの隣（`thoughts.json.lock`）にロックファイルを
   置き、Windows は `msvcrt.locking`、POSIX は `fcntl.flock` で**書き込みを直列化**する
   （取れなくても止めない＝best-effort）。書き込みは**直前のファイル内容と統合**してから行う。
   統合は**集合もの（`steps`）＝番号の和集合**、**状態もの（`plan` / `plan_retired` /
   `branch_meta` / `total_history`）＝`updated_at` が新しい方の値**（計画の改訂は差し替えなので
   和集合にすると廃止項目が復活する）。番号の採番も**ファイル反映後の内容**に対して行う。
2. **再読込**（`_ensure_thoughts_loaded`）: ファイルの更新時刻（mtime）を保持し、
   変わっていれば**ディスクから取り直す**。→ 別プロセスの追記が次の呼び出しで見える。

修正後: 4 プロセスの同時書き込みで **5 試行すべて欠落 0**（修正前は 3 回で欠落）。
別プロセスの追記は `view=true` で見え、自分の保存でもファイル上の他プロセスの手を消さない。

**重大 2 — `subgoal` の契約が schema と実装で食い違っていた**（§6.10 / §2.7）。
schema は「plan の 1 始まり」（= **位置番号**）と書いていたが、**実装は plan 項目の `id`** で
照合していた（`_thought_apply_ops` の `item.get("id") == sub`）。`_plan_merge` は同一文面以外に
**未使用の id**を割り当てるので、**差し替えの後は位置と id がずれる**。

実測（20 回反復で 20 回再現）:

```
plan=["A","B","C"], subgoal=1 → A 完了（A は id=1）
plan=["B","C"] に差し替え → live plan は [{"id":2,"B"},{"id":3,"C"}]、retired に {"id":1,"A",done:true}
subgoal=1 を送る → **位置 1 の B(id=2) は未完了のまま、廃止済み A(id=1) が done=True になる**
```

→ モデルが schema に従って `subgoal=1` を送ると**無関係な項目を完了にする**。

→ 解決の順番を 1 つに固定した（`_think_structure`）:
1. 現在の plan の**id**に一致 → その項目（同一性。差し替えをまたいでも不変）
2. 現在の plan の**1 始まりの位置** → その項目（id が一致しない番号は位置として扱う）
3. **廃止済み項目**の id → その項目（記録は残すが、達成済みには-mark しない）
どれにも当たらなければ**エラー**（推測で繋がない）。

`plan_progress` は**現在の plan の項目だけ**を数える（廃止項目の印と表示が矛盾しないよう）。
台帳の出力に `plan[].position`（1 始まりの並び順）を追加し、**`id` は同一性**だと分かるようにした。
`subgoal` の schema の説明も「plan 項目の id」と「位置」の両方を明記する形へ更新した。

### 6.17 レート制限・429・inflight 待ち（2026-10-20・v0.8.3）

健全性監査で挙がった「同一ホストのレート予算が fan-out と連打で脱落させる」を実測して直した。
**監査の「429」は 2 つの別理由を混在させていた**ので、まず切り分ける。

**実測 1 — crossref の 429 は「レート制限」ではなく共有プールのTransientな拒否**。
start-to-start 間隔 0.4 / 0.6 / 0.8 / 1.0 秒で 10 回ずつ叩いて **どの間隔でも 1〜3 回失敗**した
（間隔では直らない）。429 には `Retry-After` が無く、**1 秒で回復**する。
→ **再試行**が正解（10 試行すべて成功・11〜14 リクエスト。待つ必要なし）。
旧実装は 429 を受けると**ホストを 60 秒ブロック**して、以来そのホストのソースが全滅した
（実測: 連打 8 クエリで 1 件のみ成功）。→ `kb_http` で **429 を再試行**し、
**再試行しても駄目なときだけ**遮断する。修正後: 連打 6 回すべて成功。

**実測 2 — GitHub の search 枠は repo/issue で共通**。
`X-RateLimit-Resource` はどちらも `search` で、交互に叩くと `Remaining` が 9→9→8→8 と減った
（＝片方の枠を片方が使う）。旧実装は `budget_key` をエンドポイント別（`search/repositories` と
`search/issues`）にして、**同じホストの枠を奪い合っていた**（fan-out で成功 1/4）。
→ 予算キーを **`api.github.com/search`** に揃える（`code` 検索は枠が違うので別キーのまま）。
上流は**同時 4 本までは全部 200**、6 本で 3/6 失敗、8 本で全滅（= 10/分の枠）。

**実測 3 — 予算の「待つ」**。`_kb_new_json` は枠が埋まっているとき**待たずに 429 を返していた**。
上流には枠が残っているのに落とすのは無駄（= 実装の落ち）。→ **待てるなら待って叩く**。
待つのは **(a) 待ち時間が `_KB_RATE_WAIT_MAX_S` 以下**、**(b) 締め切り内に収まる**、のときだけ。
fan-out では各ソースが独立スレッドなので、1 ソースが待っても全体の待ち時間は max(各ソース)。
締め切り（`deadline_at`）は `_kb_gather` が**スレッドローカル**で届ける
（24 個の `kb_*` の signatures を変えないため）。

**実測 4 — openalex の 429 は実装の外**。本文が
`"Insufficient budget … free daily budget shared by everyone on your network's IP … $0 remaining"`
で、`Retry-After` は 3476 秒。= **日次予算切れ**で間隔では直らない。直叩きでも 429 になる
（= 実装のせいではない）。`Retry-After` を尊重した遮断（900 秒 cap）はそのまま妥当。

**実測 5 — inflight の待ちに上限が無い**。同一キーの 2 番目が無制限に待つと、取得に 14 秒かかる
ソースで**利用者を 13.8 秒待たせる**（`KB_DEADLINE`=8 秒の 1.7 倍）。
→ `_kb_cached` の待ちに上限（`_KB_INFLIGHT_WAIT_MAX_S`、既定は `KB_DEADLINE`）を置き、
**上限に達したら待たずに自分で取得する**（待つより早い）。

**実装上の注意（この特定の変更で踏んだ）**:
- 再試行ループの中で**応答本文と要求本文で変数名を分けない**と、2 回目の再試行が文字列の本文を
  送って `TypeError: POST data should be bytes` になる（`resp_body` と `body`）。
- 429 の**再試行中は遮断しない**（一時的な拒否なので、ホストをブロックすると他のソースまで巻き込む）。
- `Retry-After` があると尊重するが、**待つ上限**（`_KB_HTTP_429_MAX_WAIT_S`）を掛ける
  （Google 等は数秒〜数分なので、無制限には待たない）。

### 6.18 引用の根拠充填率（2026-10-20・v0.8.4）

健全性監査で挙がった「引用は返るのに本文根拠が無い」を実測して直した。
**根拠の要否は場面で違う**ので、**場面ごとに別の取り方**にしている。

**実測 1 — crossref の abstract 充填率は平均 27%**（9 クエリ × 5 件 = 45 件中 12 件）。
クエリによって 0/5（rust programming language・static analysis・webAssembly・blockchain）から
5/5（type theory）まで偏る。→ そのままでは `grounded` の**根拠が尽きる**。
Crossref は `filter=has-abstract:true` を公式にサポートしている。
これを付けると **9 クエリすべてで 5/5（45/45 = 100%）**。関連度は少し落ちる（上位に別の論文が混ざる）が、
**根拠が要る場面では「根拠が無い書誌」より「少し関連度の落ちた根拠」の方が有用**。

**実測 2 — hn の `story_text` はほぼ空**（リンク投稿なので）。
`tags=story` の検索だけだと**根拠 0 件**。一方 `tags=comment` の `comment_text` には
**議論本文**が入る（実測 263〜1607 文字）。Algolia は `tags` を複数指定できない
（実測: `(story,comment)` は story と同じ結果）→ **別のクエリとして 2 回**取る。

**実測 3 — arXiv は日本語クエリで 0 件**（英語なら 5/5）。
→ `sources` 説明（`freeagent_lookup` / `freeagent_grounded` 両方）に**「英文の語で指定」**を明記。

**設計 — `need_evidence` フラグ**。
`knowledge_lookup` に `need_evidence`（既定 False）を足し、`grounded` だけが True で呼ぶ。
- `grounded`（根拠が要る場面）: crossref は `has-abstract:true` を付け、hn はコメントも検索する。
- `lookup`（書誌の網羅性が要る場面）: **絞り込まない**（フィルタしない）。

**実装上の注意（この特定の変更で踏んだ）**:
- **キャッシュキーにも根拠の有無を入れる**。crossref / hn は**同じクエリでも返すべき引用が違う**
  （crossref は `has-abstract` の有無で別の論文を返す）。入れないと先に取った結果が使い回される
  （実測: フラグを立てても充填率が 27% のままだった）。
- **同一ホストへ 1 回の取得で複数のエンドポイントを叩くときは `budget_key` で予算を分ける**。
  hn の story → comment の 2 連打は**別予算キー**にしないと 2 番目が
  「HTTP 429: ローカルのアクセス間隔制御」で落ち、comment 側の結果が 0 件になっていた
  （`if not cerr` で黙って捨てられていた）。`_kb_new_json` の docstring が明示している逃げ道。
- **片方の失敗で全部落とさない**。comment が落ちても story は生かす。

### 6.19 巨大関数の分割と未テスト関数（2026-10-20・v0.8.5）

健全性監査で挙がった「`server.py` の巨大化」と「テストが無い公開関数」を直した。

**実測 1 — 巨大関数の分布**。`server.py` は 8,273 行・トップレベル関数 274・クロージャ 59。
**80 行超のトップレベル関数は 9 個（合計 1,175 行）**:
`tool_think` 173 / `tool_consult` 162 / `_http_make_handler` 162 / `tool_models` 161 /
`_think_structure` 122 / `call_model` 119 / `tool_agent` 104 / `_render_think` 91 /
`thought_merge` 81。

**実測 2 — 未テストの公開関数**。**トップレベル public 関数 126 個のうち 40 個**が
テストから直接参照されていない（うち 10 個は `mock.patch.object` で文字列参照）。
**11 ツールのうち 4 つのハンドラ**（`tool_ask` / `tool_fanout` / `tool_map` /
`tool_delegate`）と `serve_http` / `trace_call` が無かった。
→ **4 つのツールが壊れても CI は緑**だった。

**実測 3 — 分割の安全性**。`_render_body` の 11 分岐は
**分岐の外側のローカル変数を一切参照しない**（AST で機械的に検証）。
→ 機械的に分割できる。

**実装**:
- `_render_body`（365 行）→ **ツール別の描画関数 11 個**（`_render_models` /
  `_render_ask` / … / `_render_delegate`）＋ **dispatch 表 `_RENDERERS`**。
  `tool_think` の 51 行の検証ブロック → `_think_verify_block`。
  → 80 行超の関数の合計行数は **1,493 → 1,175 行**。

**振る舞いが変わらないことの検証**:
- **golden baseline**（11 ツール ＋ エラー経路 ＋ 未知ツール = 14 ケース / 4,123 字）を
  分割**前**のコードから取り、分割**後**と**バイト単位で完全一致**を確かめた。
- **変異テスト**で新規テストが退行を捕まえることを確かめた（7 変異すべてを捕まえた）。

**実装上の注意（この特定の変更で踏んだ）**:
- **`ast.get_source_segment` を素朴に連結すると `if A: ... elif B: ...` は壊れる**。
  AST 上 `elif` は「外側 If の orelse にある If」なので、外側の If のセグメントが
  `elif` 領域を**含んで**、取り出すと `elif` が 2 回出る（`IndentationError`）。
  → **生の行範囲**（最初のステートメントの `lineno` 〜 最後の `end_lineno`）を使う。
- **`ast.get_source_segment` は最初の行だけ列 0 から返す**（`padded=True` でも）。
  インデントを一段外す処理は、**行ごとの最小行頭インデント**を測ってから一律に外す。
- **描画表は手で書かない**。分岐表を手で書くと**新しいツールの描画が漏れる**。
  → 関数名から生成し、**全ツールが表にあることをテストで検査**する。

**規約**: 11 ツールの描画は「1 ツール 1 関数」に分ける（描画表で dispatch する）。
新ツールを足すときは `_RENDERERS` に**必ず登録する**（テストが漏れを検出する）。

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
| 代替案 | `propose_alternatives` | ステップの `alternatives` | 検証者とも別のモデル（台帳の使用済みモデルを除外）。`THINK_ALT_SYSTEM` で「代替: …」を最大 3 行。フォールバックも検証者に落とさない（`avoid`）。`truncated`（上限で打ち切り）なら**最終行の案を捨てる**（文の途中で切れた案を完全な案として残さない。実測「代替: 親プロセスのコマン」）。既定の上限は 400 のまま（接続タイムアウトの旧制約は §5 の修正で解消したが、予算を増やすと待ち時間と利用枠の消費が増えるため据え置き）。全員が環境障害なら**書かない** |
| 閲覧 | `view=true` + `session_id`（`brief=true` で要約） | — | 何も書かない。サブも呼ばない（`verify` が付いていても）。`brief` は**現行の道筋**（改訂済み・棄却分岐を除く）だけを返し、省いた件数を `omitted` に出す（黙って隠さない）。計画・仮説・分岐・`active_path` は要約でも残す |

- **推測で繋がない**: 参照先が無い操作はエラーで返し（`known_thoughts` / `known_branches` を添える）、
  台帳を書かない。誤った番号のまま積むと以後の `active_path` が静かに壊れる。
- 検証者・提案者のプロンプトは**現行の道筋**（`superseded_by` 無し・`abandoned` 分岐以外）だけを載せ、
  改訂なら改訂前の文、仮説の検証なら対象の仮説を添える。印は統合時＝検証の後に付くため、**今回の
  呼び出しで改訂する元の思考・決着させる分岐も**プロンプト側で先に反映する（`_think_preview_row`）。
- 並列呼び出しで `branch_id` を省略した新しい分岐が同時に来ると、自動割当が同じ ID になり得る
  （割当はロック外）。明示の `branch_id` を推奨する。

### 7.5.2 KANBAN のカードとの紐づけ（v0.7.0）

KANBAN のワーカー（`hermes -p <profile> chat -q "work kanban task t_x"`）は、このサーバーの推論ツールを
カードの作業中に呼ぶ。**思考がどのカードのものか**を後から辿れるように、`freeagent_think` に `card` を足した。

- `card`（任意・64 文字に切り詰め）を渡すと台帳の行に `card` が載る。**渡された値が優先**で、未指定なら既存を保つ。
- **`card` は台帳の取っ手**: `session_id` を省略して `card` を渡すと、そのカードの既存台帳があれば**継続する**
  （無ければ新規。継続したときは `notes` に 1 行出す）。明示の `session_id` が常に優先。
  **実測（v0.7.0 のワーカー実行・カード t_2822eb3f）**: `session_id` を省略した 2 手目が新しい台帳になり、
  道筋が分断された（`view=true` + `card` は最新の台帳しか返らない）。v0.7.1 で継続に変えた。
- `view=true` + `card`（`session_id` 省略時）で、そのカードの台帳を**読み戻せる**（`thought_find_by_card` が
  最後に更新された台帳を返す）。`session_id` を併せて渡した場合は **`session_id` が優先**（card は記録のみ）。
- カードが未知なら**推測せずエラー**（`カード <id> に紐づく台帳はありません`）。
- 応答に `card` を返し、`next_call.args.card` にも引き継ぐ（次の思考で落とさないため。`view` の応答も同じ）。
- **別プロセスからも読める**: 台帳は `thoughts.json` に落ちるので、ワーカーとは**別の MCP プロセス**（メイン
  セッションなど）でも `view=true` + `card` で同じ道筋を読み戻せる。**実測（v0.7.1・別プロセス実行）**: カード
  `t_ae980837` の `view` で `ledger.steps_recorded=2` / `ledger.latest` に 2 手の本文 / `active_path=[1,2]` が返り、
  未知カードは推測せずエラーになった。**カード ID を控えておけば、メインがワーカーの思考を後から監査できる**。
- **完了・ブロックはこのツールの仕事ではない**: カードの終端は `kanban_complete` / `kanban_block`（Hermes の
  `kanban_*` ツール）で行う。MCP サーバーはボードを書かない（ワーカー系譜の MCP 子プロセスは書き込みフェンスで
  `PermissionError` になる。§6.13 の調査で確認済み）。description にもその分担を書いてある（規約 5）。
- 記録先は既存の台帳（`thoughts.json`）で、新しいストアは作らない。上限 `THOUGHT_MAX` / `THOUGHT_MAX_STEPS` は同じ。
- 回帰テスト: `tests/test_freeagent_bind.py` の `TestThoughtLedgerStore`（記録・検索・未指定時の保持）と
  `TestThinkCardLink`（ツール経由の記録・カードからの読み戻し・未知カード・session_id 優先）。

## 7.6 利用者向け説明と実装の境界

README は利用開始・結果の読み方・障害時の対処を先に置き、実装上の設計理由はこの SPEC に置く。
利用者が用途から選べる導線を設け、既定6ソースと明示指定18ソースを区別する。
引数一覧・追加プロバイダの確認設定・過去の計測記録は折りたたみ、
無料枠・データ送信・保存・引用の限界は利用者が確認できる形で残す。
次の既存挙動を、保証されている機能として過大に説明しない。

- `provider_ready` / 一覧の `usable` は資格情報の存在などの判定で、キーの有効性を検証しない。
  `usable_now` は ready な Free 候補からクールダウン中を除いた数であり、未検証モデル・認証失敗記憶中の
  候補も含みうる。自動選抜は別途認証記憶を考慮する。「実応答できるモデル数」と同一視しない。
- `tool_map` は `items[:64]`、`tool_fanout` は `prompts[:16]` で入力を切り詰める。
  `MAX_CALLS_PER_RUN` は fanout の組合せ上限であり、map 全体に適用される上限ではない。
  **切り詰めた件数は必ず返す**（v0.14.1/v0.14.2・規約 63）: map は `items_dropped`
  （`FREEAGENT_MAX_MAP_ITEMS`・既定 64・1〜256）、fanout は `prompts_dropped`
  （`FREEAGENT_MAX_PROMPTS_PER_CALL`・既定 16・1〜64）。どちらも本文に警告行が出る
  （`_dropped_lines` を共有）し、schema に `maxItems` を宣言する。
  以前は `items[:64]` / `prompts[:16]` と**黙って捨てていた**。
- `tool_think` の `plan` は上限 12 を**超えたらエラー**（切り捨てではない。多すぎる入力を
  そのまま受け取らない契約で、schema にも `maxItems: 12` を宣言）。台帳側の切り詰めは防御用。
- 知識検索は LLM による生成を介さないが、提供元の誤り・古さ・検索の関連性不足は防げない。
  `summary` は導入部・説明・取得可能なアブストラクトなどであり、原典の全文とは限らない。
  締め切り後の結果が次回すぐ返るのは、同じプロセス・同じ検索条件で取得が成功しキャッシュに入った場合のみ。
- `enabled=false` ではサーバーは起動されず、本サーバーの `next_action` は返らない。
  代替へ切り替える判断はホスト側の規則・LLM に依存する。停止方法としてキー削除・故意の接続断は推奨しない。
- MCP `env` とシェルの環境は別で、単体スクリプトは MCP `env` を自動で読み込まない。
  `smoke_stdio.py` のネットワークモードは一覧までの検査で、実推論の成功を検査しない。
  `warmup_models.py` の終了コード 0 も生存モデルが 1 件以上ある保証ではない。
- `measure_adoption.py` は全セッション比の採用率に加えて、依頼文の言い回しから「必要場面」を近似判定した採用率と見逃しを出す（`NEED_CUES`）。判定は近似であり、必要場面の完全な判定ではない。既定では `messages.tool_calls` を持つセッションだけが対象で、ツールを 1 つも呼ばなかったセッションは `--include-tool-free` を付けたときだけ分母に入る。見逃しに添える競合の回数は**そのセッション全体の回数**で、必要能力を置き換えた回数そのものではない。
  - **誤検出の型（すべて実測で特定して修正）**: (a) 「読み取り専用の再レビュー。…を読み、」＝**自分で読む依頼**（`_COMMON_EXCLUDE`）。(b) `@url` で貼られた**添付ページ本文**の英語 "sources"（`utm_source` やページ見出し）を依頼文と誤認（`_request_only` で添付を落とす）。(c) **委譲の完了通知**（`[ASYNC DELEGATION …]` が user ロールで保存される）の本文（`_MACHINE_NOTICE`）。(d) サービス・提供元・公式条件を探す依頼は学術コーパス外（`sources` の `exclude`。ただし `strong`＝文献・論文・先行研究・サーベイがあれば除外しない）。
  - **コーパス外の根拠（実測）**: `freeagent_lookup` に「LLM inference API provider comparison alternatives」を引くと**論文 9 件が返り、提供元の一覧は返らない**（学術ソースは API 提供元を索引していない）。同様に「J-STAGE / CiNii の公式利用条件」も該当コーパスが無い。この型は `web_search` が正しい選択なので見逃しに数えない。
  - これらを直した結果、直近 25 セッションの必要場面は **12→6 件**、必要場面の採用率は **62%→100%（6/6・見逃し 0）**。**数字が良くなったのは計測器を直した結果で、モデルの行動は変えていない**（自発率の改善と読まないこと）。必要場面が 6 件しかないので、この 100% を「問題が無い」証拠としない（**見つけられなくなった**が正確）。判定は依然として言い回し依存の近似なので、`--excerpt` で中身を確認してから判断する。
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

### 8.2 HTTP (StreamableHTTP) トランスポート（§10）

**既定は stdio。** `--transport http` / `FREEAGENT_TRANSPORT=http` のときだけ HTTP で待ち受ける。目的は
「**1 プロセスを複数クライアントが共有する**」こと。stdio を N 本立てると、プロセス内に持つホスト予算
（`_KB_RATE_NEXT`）・`_KB_CACHE` / `_KB_INFLIGHT`・`_MODELS_CACHE`（600 秒。**一覧だけは `model_catalog.json` に落ちるので、プロセスを入れ替えても温かい**・§4.4）・`_COOLDOWN`・`model_stats`・
`MAX_WORKERS` が N 分割される（規約 28 が警告する「同一 IP の連打」）。

仕様の範囲は、Hermes の `mcp.client.streamable_http` が実際に要求する経路に絞る（実測で確定）:

| 要求 | 契約 |
|---|---|
| `POST <path>`（単一メッセージ） | `200` + `application/json` + **JSON-RPC オブジェクト 1 個**。配列で返すとクライアントは `Failed to parse JSON response` で切る |
| `POST <path>`（batch） | `200` + **配列** |
| `POST <path>`（通知＝id なし） | `202` + 本文なし。**要求に 202 を返すとクライアントは失敗として扱う** |
| `initialize` の応答 | `mcp-session-id` ヘッダを付ける（以降クライアントが返す）。未知のセッションは `404` + `-32001`。ヘッダ無しの要求は**素通し**（他クライアント互換） |
| `GET <path>` | `Accept: text/event-stream` とセッションが必須。サーバー起点の通知（§8.6）を SSE で流し、空ならキープアライブのコメント行。セッション無し `400` / 未知 `404` / Accept 違い `406` |
| `DELETE <path>` | `204` でセッション終了（以後は `404`）。未知は `404` |
| 認証 | `FREEAGENT_HTTP_TOKEN` を設定したときだけ要求（`Authorization: Bearer` か `x-freeagent-token`、`secrets.compare_digest`）。未設定なら素通し |
| パス・本文 | `FREEAGENT_HTTP_PATH`（既定 `/mcp`）以外は `404`。`FREEAGENT_HTTP_MAX_BODY` 超は `413` |

- **メソッド表は §9 の `_replies_for` 1 か所**に置き、stdio の `_dispatch_batch` / `_dispatch_message` と
  HTTP が共有する（表を分けると片方だけ更新され、stdio と HTTP が食い違う）。`_dispatch_message` は
  `tools/call` だけプールへ流す（従来どおり）。**id なしの `initialize` / `logging/setLevel` には応答しない**
  （batch 経路に揃えた。従来の単一経路は id `null` の応答を書いていた）。
- 通知はセッションの待ち行列へ入れ、`GET` の SSE で流す。`GET` を張らないクライアントには届かないが、
  警告は最初のツール結果にも載るので実害はない。
- **二重起動は失敗させる**: `ThreadingHTTPServer` の `allow_reuse_address` を `False` にする
  （Windows の `SO_REUSEADDR` は同一ポートへの二重バインドを許し、状態が 2 つに割れたまま気づけない）。
- **ハーネス目印はヘッダでも受ける**（`x-freeagent-harness`。HTTP では子プロセスの env が渡らないため）。
  ヘッダ > env の優先で、`detect_harness(params, env, marker)` の `marker` がヘッダ由来（`source: "header"`）。
- 実装は標準ライブラリのみ（`http.server`）。stdio の起動を重くしないため `http.server` と `argparse` は
  HTTP の関数の中で遅延 import する。
- セッションは `FREEAGENT_HTTP_SESSION_TTL`（既定 1800 秒）で失効し、上限（既定 64）に達したら古い順に落とす。
  `_HARNESS` はプロセス共有なので判定は**最後に initialize したクライアント**のものになる（警告 1 回・
  最初の結果への注記 1 回という性質は変わらない）。
- 検証: `python scripts/smoke_http.py`（実クライアント経路）と `tests/test_http_transport.py`。
- **Windows での常駐化は repo の生成器が担う**（`scripts/install_resident_windows.py`）。worker（VBS）・
  launcher（`.pyw`）・ログオン起動・見張りタスク・`hermes config` の切替/戻しを 1 コマンドで作り直せる
  （機械固有のパスは引数。既定 `%LOCALAPPDATA%\hermes`）。**生成物はコンソールを生まない**: 生存確認は
  WMI（`Win32_Process` の CommandLine 照合＝子プロセスを作らない。使えないときだけ WinHttp へ落ちる）、
  起動は `pythonw.exe`（GUI サブシステム）、ログは VBScript のファイル追記。`WshShell.Exec("cmd /c netstat …")`
  や `cmd /c …` は子プロセスにコンソールを割り当て、Windows 11（既定のターミナル = Windows Terminal）では
  **数分おきに黒いウィンドウが一瞬出る**（実測: 5 分周期の見張りで毎回 1 個。ウィンドウを隠す指定でも表示された）。
  二重起動の判定は launcher が付ける目印 `freeagent-resident-<port>` と旧来の `server.py … --port <port>` の
  **両方**に当てる（片方だけだと起動方式を変えた直後に二重起動し、`allow_reuse_address = False` の
  ため起動が失敗する）。検証: `tests/test_resident_installer.py`。
- **目印（ハーネス判別）の点検も HTTP に対応する**: `scripts/apply_proactive.py --check` は
  `x-freeagent-harness` ヘッダを env と同じ目印として読み、HTTP なら不足時にヘッダを足すコマンドを出す
  （ヘッダを読まないと常駐運用の実機で「未設定」と誤警告した）。

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
- **除外の読み取りはサーバー節で区切る**。`mcp_servers` の下は**すべてインデントされる**ため、節の
  終端を「インデント無しの行」で判定すると節が終わらず、**後方の別サーバー（`gitlab` 等）の
  `exclude` を混入**させる（実測: `get_mcp_server_version` / `list_duo_sessions` を対象サーバーの
  設定と誤認し、`--check` が偽陽性で exit 1）。境界は `server_block()` と同じ（インデント 2 の
  次のサーバー名まで）。
- 測定は回答本文ではなく **`state.db` の `messages.tool_calls`** で行う（`scripts/measure_adoption.py`）。
  遅延カタログ経由の呼び出しは `tool_call` として記録され、実名は `arguments.calls[].name` に入る。
- **思考台帳の常用**: 判断規則（`SNIPPET`）・`freeagent_think` の description 先頭【常用】・
  `PROACTIVE_INSTRUCTIONS` の 3 箇所で「2 段以上の推論が要る問題では、考え始める前に `freeagent_think` を開き、
  plan で分解・revises_thought で改訂・branch_from_thought で分岐・total_thoughts で見積り調整・
  kind=hypothesis と tests_hypothesis で仮説の生成と検証を積む」を指示する。1 問 1 答・単純な事実確認・雑談は
  対象外（全問で開くと 1 ターンが無駄に伸びる）。台帳側でも補強する: 計画なしの 1 ステップ目と、仮説・分岐・
  改訂が 1 つも無い 3 ステップ目に**だけ** `suggestions` で促す（毎ステップ出すと雑音になる）。
- **文面に優先順位を足しても動かなかった（実測・否定的結果）**: 「出典・根拠が要る依頼では web_search /
  web_extract の前に freeagent_lookup / grounded を検討する」を判断規則へ追加し、同じ依頼を新プロセスで
  前後比較した（3 組。自己申告で、追加した文が実際に文脈へ入っていることも確認済み）。**3 組とも選択は
  変わらなかった** — 既に正しく選べていた 2 組はそのまま、過去に見逃していた探索型の依頼
  （「世界中から API を探して」）は前後とも `web_search` のままで `freeagent_lookup` を使わなかった。
  同種の文面を足すだけでは動かないので**この追加は取り消した**。次に試すなら `description` 側
  （`freeagent_lookup` の記述）を変え、同じ 3 組で測る。
- **「有効な間だけ」効かせる**: 判断規則は `<!-- freeagent-bind: proactive-usage -->` 〜
  `<!-- /freeagent-bind: proactive-usage -->` のブロックで SOUL.md に置く。`--write-snippet` は**既存の
  ブロックを最新の文面に差し替え**（旧形式＝終端マーカー無しも可。ブロック外は触らない。改行コードは元の
  ファイルに合わせる）、`--remove-snippet` はブロックだけを外す（MCP を外したときに規則が空振りし続けない）。
  規則自体にも「無効・不通なら存在しないツールを探さず代替で完遂」を含める。

## 12. 検証

```bash
python -m compileall -q src/freeagent_bind   # 構文
python scripts/check_integrity.py            # レジストリ・スキーマ・版の整合
python -m unittest discover -s tests         # オフライン回帰（件数は実行結果を参照。一覧キャッシュは `tests/test_catalog_cache.py`）
python scripts/smoke_stdio.py                # 実クライアント経路（stdio）
python scripts/smoke_http.py                 # 実クライアント経路（HTTP: 通知 202 / batch / セッション / SSE）
python scripts/check_offline.py              # 全滅時の縮退（例外漏れ・ハング・状態汚染なし）
python scripts/measure_adoption.py           # 自発利用率（state.db を読むだけ）
python scripts/measure_relevance.py --report # 知識ソースの関連性（トップ1 の的中率）
FREEAGENT_PROBE_NET=1 python scripts/smoke_stdio.py   # バックエンド生存
env -u PYTHONPATH PYTHONPATH=src python scripts/warmup_models.py   # モデルの生存確認を定着
```