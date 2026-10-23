# AGENTS.md — hermes-freeagent-bind

Hermes Agent の **Free モデルをサブ LLM として並列に走らせる** MCP サーバー（Python 3.11+ / 実行時依存ゼロ /
単一ファイルのモノリス）。メイン LLM の知識補助として **arXiv / Crossref / OpenAlex / Wikipedia /
Wikidata / GitHub** を既定で引き、**DataCite / OpenAIRE / Zenodo / ROR / DOAJ / npm / crates / CiNii /
OSV / IETF / INSPIRE-HEP / OEIS / HF Hub / Hacker News / Software Heritage / Libraries.io /
Internet Archive / Qiita** は
明示指定で追加する。**対象は科学（物理・数学・計算機）とプログラミングまでで、医学・生物学は対象外**。

- 全体像は `README.md`、**実装の契約は `SPEC.md`**（変更時は両方を同一変更内で更新する）。
- 本体は `src/freeagent_bind/server.py` の 1 ファイルで、**§0〜§10 の区画**に分かれている。
  機能追加は既存の行を書き換えるのではなく**新しい§を立てる**形で行い、冒頭 docstring の目次も更新する。

## セットアップ

```bash
hermes proxy start            # 推論バックエンド（Free モデル）
python -m compileall -q src/freeagent_bind
python scripts/check_integrity.py
python -m unittest discover -s tests
python scripts/smoke_stdio.py
```

`pip install -e .` は不要（依存ゼロ）。`hermes mcp add` は対話式で、TTY が無いと `Cancelled.` になり
設定が書かれない。**`hermes config set mcp_servers.freeagent-bind.<key>` で非対話に組む**。

## 変更時に必ず実行する検証（ゲート）

```bash
python -m compileall -q src/freeagent_bind   # 構文
python scripts/check_integrity.py            # TOOLS/HANDLERS の一致・スキーマ・版・content の規約
python -m unittest discover -s tests         # オフライン回帰（件数は実行結果を参照・ネットワーク不要）
python scripts/smoke_stdio.py                # 実クライアント経路（initialize/tools/list/tools/call）
python scripts/smoke_http.py                 # HTTP 経路（1 プロセス共有。通知 202・batch・セッション・SSE）
python scripts/probe_knowledge_stdio.py      # 新規知識ソースの実API・stdio（ネットワークあり）
python scripts/check_offline.py              # バックエンド全滅: 例外漏れ・ハング・状態汚染が無いこと
python scripts/measure_adoption.py           # 自発利用率の測定（state.db を読むだけ・副作用なし。必要場面の近似判定も出す）
python scripts/measure_kb.py --report         # 知識バックエンドの時間帯別の応答時間（記録を読むだけ）
python scripts/measure_relevance.py           # 知識ソースの関連性（トップ1 の的中率。ネットワークあり）
python scripts/measure_quality.py --cases 6   # 戦略ごとの正答率（ネットワーク・Free の枠を使う）
python scripts/apply_proactive.py            # 率先利用の設定（既定は表示のみ。--apply で適用）
FREEAGENT_PROBE_NET=1 python scripts/smoke_stdio.py   # バックエンド生存（プロキシが要る）
env -u PYTHONPATH PYTHONPATH=src python scripts/warmup_models.py   # モデルの生存確認を定着（数分）
```

終了コード 0 が正常。1 は失敗（レジストリ不一致・版不一致・例外漏れ・UTF-8 破綻・プロトコル破綻）。

スクリプトは起動直後に `sys.stdout/stderr` を **UTF-8 に reconfigure** する（日本語 Windows の cp932 や
CI の cp1252 では、日本語の `print` が `UnicodeEncodeError` になりゲート自体が落ちる — 実測）。
`tests/test_freeagent_bind.py::TestHostEncodingTolerance` が再発を防ぐ。

## 守るべき規約

1. **例外をツールの外へ漏らさない**。失敗は `structuredContent.error` で返す（`handle_tool_call` が最後の砦）。
2. `content` = 人間向け日本語、`structuredContent` = LLM 向け純粋 JSON。**両方返す**。
3. `content` に**LLM 向けの指示文を書かない**（指示は `description` と docstring に置く）。
4. 数値引数は `as_int` / `as_float` を通す。**非有限（inf / nan）は既定値へ落とす**（`"1e999"` →
   `int(inf)` の OverflowError が実際に漏れた）。文字列引数は文字列だけを受ける。
5. `description` の先頭に【使う条件】【使わない条件】【競合より優先】を置く。**description がモデルの
   唯一の窓口**で、これが無いと `delegate_task`（同一モデルの分身＝多様性ゼロ）が選ばれる。
6. **空応答を成功として返さない**。思考トークンで予算を使い切るモデルがあり、空を回答として渡すと
   メイン LLM が無回答を回答と誤解する。予算を上げて1回だけ再試行し、なお空ならエラーにする。
7. **クールダウン中は除外せず後回し**（空きが足りないときだけ補充）。除外すると選択肢が痩せ、
   後回しにしないと「選択直後に 429 → 全候補がクールダウン中 → 1体へ縮退」が起きる。
8. **失敗を content に出して隠さない**。討論ラウンドの脱落は `failed` / `debate_summary.dropped` に残し、
   表示は1行に集約する（LLM へは全ラウンドを返し、人間には重複なく見せる）。
9. 外部 HTTP は **(connect, read) のタイムアウト必須**。`urllib` の `timeout` は**応答ヘッダ待ちにも掛かる**ので、
   §3 の共有 `_OPENER`（接続の間だけ接続上限、接続後は読み取り上限）を使う。Windows で未キャッシュの公開ルート CA（`export.arxiv.org` の Starfield Root G2 等）により `CERTIFICATE_VERIFY_FAILED` が出た場合は、Schannel 経由の HEAD でストア更新を促し `_HTTPSHandler._context` を再生成して 1 回再試行する（検証は無効化しない）。遮断はホスト単位で記憶して fail fast。
10. arXiv は **https**（http は 301 の先で 406）かつ **3 秒間隔で直列化**する。
11. 「結論/確信度/確認したい点」の解析は**1 行 1 ラベルと決め打たない**（同一行に複数ラベルが来る）。
12. サブエージェントは**最終ステップでツールを封じて回答を要求する**。到達しなかったら推測で埋めず
    `steps_exhausted` と収集済みの根拠を返す。
13. **`protocolVersion` はクライアント提示値をそのまま返す**。固定すると `tools/list` が取り消される。
14. **stdout へは必ず UTF-8**（cp932 に落ちると応答が黙って捨てられる）。
15. 蓄積データは `FREEAGENT_STATE_DIR`（既定 `%LOCALAPPDATA%\hermes-freeagent-bind`）。**一時領域に置かない**。
    書き込みは tmp + `os.replace` + `Lock`。知識は蓄積しない（作業状態だけ）。
16. **モデル一覧を信じない**。実測: NVIDIA は 82 件中 55 件が 404（EOL）、HF の無料 3 件は権限不足で 403、
    OpenRouter の `:free` にも提供元都合の 403 がある。`freeagent_models` の `probe: true` で生存確認し、
    **404/410 と 401/403 だけ除外**する。**timeout・空応答・429・5xx は残す**（生きているが今は応えない
    ものを永久に隠さない）。生存確認は `allow_fallback=False`（有効だと他プロバイダの応答が「生存」に化ける）、
    かつ `free_only` 省略時でも有料モデルは呼ばず、`provider` 未指定時は `provider_ready` なプロバイダを対象にする。
    HTTP 500 本文中の `not found`（NVIDIA NIM の Authorization 欠落等）を `gone` に誤分類しないよう、5xx/auth 判定を先に置く。
17. **認証失敗はプロバイダ単位で覚える**（`provider_auth.json`・既定 15 分）。覚えないと毎回同じ
    プロバイダを引き当てて空振りする（実測: 4 体選抜のうち 3 体が HF）。**自動選抜からだけ外し、明示指定は
    試す**（キーを直せば即復帰）。除外は必ず `notes` に出す。
17b. **403 を「キー未設定」と決め打たない**。`_is_auth_error` の署名で判定する（401 だけは無条件で認証）。
    実測: HF の `:together` 経由は **Cloudflare Error 1010 "Access denied"**、OpenRouter の `:free` は
    モデル単位の提供元制限で 403 を返す。これらを認証失敗にすると**プロバイダ全体を 15 分止め、生きた
    モデルまで選抜から消える**。エラー文言に `HTTP 401/403` のような曖昧表記を書くと生存確認の
    部分一致が 401 に誤マッチする（実測）ので、実際のステータスを書く。
18. **選抜はプロバイダを巡回させる**（`diverse_order`）。品質観測が無いと同点になり、素の順序では
    モデル ID のアルファベット順で 1 プロバイダが枠を独占する。プロバイダ順は最良モデルの順位で決める。
19. HF の料金・文脈長は**提供元単位**（`providers[]`）。`status == "live"` かつ `is_free`／価格 0 の
    提供元があるときだけ Free と判定する（`_is_hf` / `_free_providers` / `_context_length`）。
20. ツールを増減したら `README.md` のツール表・`SPEC.md`・`tests/`・`scripts/` を同一変更内で更新する。
    README の「**期待しないこと（実測に基づく）**」節は**超高性能にはならない**旨を明示する場所で、
    数値（起動時間・スキーマ文字数など）は**測り直して**書く（推測値・他プロジェクトの値を流用しない）。
    ツールを増減したら起動時間とスキーマ文字数も測り直す。
21. **不通（プロキシ停止・DNS 不達・TCP 拒否・タイムアウト）では状態を一切書かない**（`is_env_failure` →
    `observe_call` が no-op）。環境障害は**モデルの成績ではない**ので、統計に入れると「プロキシが落ちて
    いた数分」が全モデルの評価を下げ、復旧後も選抜が歪む。トレースにも意味のある情報が無い（切り分けは
    `FREEAGENT_DEBUG_LOG`）。この契約は `python scripts/check_offline.py` が**状態ディレクトリにファイルが
    増えないこと**で検証する。**モデルの失敗（429 など）は従来どおり記録する**（no-op を広げすぎない）。
22. **失敗応答には `structuredContent.next_action`（`kind` / `advice` / `fallback_tools` / `check` /
    `reenable`）を付ける**。無効化・不通でも利用者のターンは続くので、ここで「再試行するな／代替はこれ」を
    返さないと、存在しないツールを掘り続けるか同じ失敗を繰り返してターンと時間を捨てる（旧実装の実測）。
    `content` には書かない（人間が読むチャネル。規約 3）。
23. **自発利用の実効レバーは `description` ＋ 毎ターン注入される判断規則（memory / `SOUL.md`）＋
    競合サーバーの汎用面を外すこと**（`mcp_servers.<name>.tools.exclude`）。MCP `instructions` は
    **Hermes では読まれない**ので当てにしない（他クライアント向けに返すだけ）。記述の工夫だけでは
    自発率が 1/2 で頭打ちになる（実測）。変更後は `python scripts/measure_adoption.py` で**最低 2 標本**
    測る（実行中セッションは起動時のツール一覧を保持するので、測定は新プロセスで）。判断規則の文面は
    `scripts/apply_proactive.py` が出すものを単一の出典にする（文面を散らすと乖離する）。
    **除外パターンは「ライブの」実ツール名に照合してから書く**（`fnmatchcase`。glob でなければ完全一致）。
    `ask_*`（アンダースコア）は実名（`ask-all` / `consensus-step` … ハイフン区切り）に 1 件も一致せず、
    **空振りのまま「設定した」気にさせる**。加えて **`cache/mcp_schema_cache.json` は不完全**（実測 18 件。
    実在する `panel` / `consensus` / `consensus-step` が欠けており、キャッシュで照合すると「存在しない」と
    誤判定する）ので、照合は `hermes mcp test <server>` のライブ一覧で行う。`apply_proactive.py --check` が
    設定済みの除外の空振りを exit 1 で検出する。**除外の読み取りはサーバー節で区切る**（`mcp_servers` の下は
    すべてインデントされるので、終端を「インデント無し行」で判定すると後方の別サーバーの `exclude` を
    混入し、`--check` が偽陽性で exit 1 になる — 実測）。
    **SOUL.md の判断規則はマーカーで囲んだブロックとして差し替える**（`--write-snippet` は最新の文面へ
    差し替え、`--remove-snippet` で外す）。「入っていれば何もしない」にすると、文面を更新しても古い規則が
    残り続ける。ブロック外の利用者の記述と**元の改行コード**（CRLF / LF）は変えない。

24. **思考台帳（`freeagent_think`）の検証は opt-in、採番はロック内、環境障害では書かない**。
    (a) `verify=true` のときだけサブLLMを呼ぶ（既定は台帳のみ＝サブ呼び出し 0 回。全ステップに検証を
    付けると 1 ターンが分単位になる）。検証者は**生成者と別モデル**で、同意ではなく反証を探す。
    (b) 台帳の読み・採番・書きは `thought_merge` の**1 つのロック内**で行う（分けると並列呼び出しで
    片方の思考が上書きで消える）。上限超過は黙って捨てずエラーで返す。
    (c) **環境障害では台帳に書かない**（規約 21）: `verify=true` で全検証者が環境障害で落ちたら、
    その思考は記録せずエラーを返す（検証されていない前提の上に次の思考を積まない）。モデル側の失敗
    （429 など）は記録し、`failed_rows` と `answered: 0` で隠さず返す。`check_offline.py` が
    「`verify=true` で呼んでも状態ディレクトリにファイルが増えない」ことで検証する。
    (d) 次の一手の助言は `structuredContent.suggestions` に置き、`content` には書かない（規約 3）。
    (e) **構造（計画・改訂・分岐・仮説）の参照は推測で繋がない**。存在しない番号・分岐・仮説・サブ目標は
    **サブ呼び出しの前に**エラーで返し、台帳を書かない（`_think_structure`）。改訂は消さずに
    `superseded_by` を付け、検証者・提案者には**現行の道筋**だけを渡す。今回の呼び出しで改訂・決着させる
    対象もプロンプト側で先に反映する（印は統合時＝検証の後に付くので、放置すると撤回済みの前提が
    「現行」として検証者に渡る。実装中のテストで検出）。`propose_alternatives` の到達不能も (c) と同じく書かない。

25. **ハーネス判別は目印が主、推定は従**（§8.6）。Hermes の `clientInfo` は MCP SDK 既定の `mcp` で
    **Hermes 固有でない**（実測）ので、`clientInfo` だけで「Hermes」と決めない。確実なのは設定の `env:` に置く
    `FREEAGENT_HARNESS=hermes`（`env:` だけが子プロセスにそのまま渡る。`HERMES_*` は渡らない）。判別不能
    （unknown）の Hermes 利用者を毎回の警告で煩わせない（stderr 1 行だけ）。Hermes 以外（other）でも
    **動作は止めず警告だけ**、各チャネル 1 プロセス 1 回。判定はサブプロセスを使わない（起動が秒単位で遅くなる）。
    `notifications/message` を送るので `capabilities.logging` の宣言を外さない（仕様上 MUST）。
    スモークなどのクライアントは **`id` の一致する応答まで読み、途中の通知を飛ばす**（通知を応答として読むと
    以降が 1 つずつずれる。実装中に `smoke_stdio.py` で発生）。

26. **並列呼び出しの予備候補で枠を重複させない**（`_ModelClaims`）。フォールバックが同じ呼び出しの他の枠・
    除外モデル・（think の代替案では）検証者と同じモデルに落ちると、「独立 N 体」の表示が実質 1 体になる（実測）。
    予備が尽きたら脱落として返し、同じモデルで埋めない。`max_tokens` で打ち切られた応答の**最終行**は
    文の途中で切れているので、完全な項目として拾わない（`parse_alternatives(truncated=True)`）。

27. **知識取得は締め切りで待ち時間に上限を付け、遅れたソースは脱落として見せる**（§5.8）。全ソースの完了を
    待つと、1 ソースの遅延（1 リクエスト最大 20 秒・Wikidata は最大 3 回直列）がそのまま全体の待ち時間になる。
    並列数を LLM 用の `MAX_WORKERS` に揃えない（ソースは別ホストなので全部同時でよい）。締め切りは取得の
    中止ではなく、裏の取得を `_kb_cached` に入れて次回を速くする。ソースの追加・差し替えは**時間帯別の実測**
    （`measure_kb.py --report`）を根拠にする。1 時点の計測で「速い／遅い」と決めない（01:59 の計測では
    遅延が再現しなかった）。HTTP 200 でも中身が JSON でない提供元がある（dblp はボット判定の HTML）ので、
    候補は**状態コードではなく中身**で確かめる。

28. **追加ソースは明示指定、arXiv代替は明示許可だけ**（§5.9〜§5.13）。既定6ソースを維持し、
    `fallback=true`のJSON真偽値だけがDataCiteへ追加送信できる。検索式・版指定を弱めて代替しない。
    同じ締切を主系/代替に共有し、期限後に新たな代替を開始しない。主系の失敗と実取得元を残す。
    抄録欠落は`metadata_only`として本文根拠にせず、同じDOIの配信元を独立した裏付けに数えない。
    DOI/URLの対応は全入力を先に確認し、曖昧な書誌の版帰属を入力順で決めない。本文選択とは別に別名を保持する。
    本文予算で注入しなかった番号も引用成功に数えない（§6.11）。取得候補registryと注入済み番号を分離する。
    ホスト予算はモード間で共有するがプロセス間共有ではないため、複数MCP/CLIによる同一IPの連打に注意する。

29. **第3段階は利用条件を満たす公開メタデータだけ**（§5.14/§5.15）。Zenodoは説明/注記のみ、ファイル・全文は取得しない。
    メタデータCC0とファイルライセンス/アクセス条件を分離し、メール欄/テキスト中のメールを返さない。非軍事用途に限る。
    引用符/アドレスリテラルも省略し、未対応の@が残る値は返さない。省略ラベルやscript/style等・コメントを本文根拠にしない。
    ROR IDはCrockford文字集合と公式チェックサムを検証し、誤ったIDを修復しない。
    RORは機関候補の実属性を構造化根拠として返す。論文抄録を生成せず、設立年を出版年にしない。検索順位で機関同定を確定しない。
    既定6を維持し追加5は明示のみ。ROR6.1秒、Zenodo2.01秒のプロセス内間隔を他アプリ/IP全体の保証にしない。
    API規約が参照するAUP/bot/AI利用制限も実プローブ前に確認し、許諾未確認候補は登録しない。既定offは許諾ではない。
    保留候補の条項レベルの確認結果は SPEC §6.6 に記録し、**再確認なしに登録しない**（J-STAGE は「Powered by J-STAGE」
    表示・24時間以上のキャッシュ禁止・利用者への規約表示、CORE は T&C §3 が検索/探索機能に関わる製品の連絡を要求）。
    **キーが前提のソースは利用者自身のキーがあるときだけ動かす**（CiNii = `FREEAGENT_CINII_APPID`・§5.17/§6.7）:
    未設定なら HTTP を出さずに登録先を案内し、既定ソースには加えない。**プロジェクトはキーを同梱・共有しない**
    （貸与・譲渡の禁止）。**利用目的に当たるかの判断は利用者に信託し、サーバーは代わりに同意しない**。
    プローブと計測はキー未設定ならそのソースを外して続行する（未設定は失敗ではない）。

30. **第4段階は検索できる公認APIだけ**（§5.16）。DOAJ（記事メタデータCC0・2req/s公認）/ npm（公式Public APIで
    複製を明示許可）/ crates.io（Crawler Policy: 1req/s＋識別UA）を明示指定ソースとして追加。既定6は不変。
    パッケージの説明文は**登録者の自己申告**であり審査結果ではない（`summary_kind=registry_description`で
    論文抄録と区別し、品質・安全性の根拠として提示しない）。キーワード検索APIが無いもの（PyPI / deps.dev）、
    既存ソースと重複するもの、医学・生物学（対象外。§5.19）、レートが8秒締切と不整合なもの（PLOS 10req/min）、
    条件曖昧なもの（HAL 非商用条項）は登録しない。DOAJはパス埋め込み検索なので検索語を必ずURLエスケープする。

31. **Free推論プロバイダは課金tierを推測しない**。APIがFree/paid tierを返さない場合は、モデルID allowlist・明示確認env・厳密な未許可model拒否を組み合わせる。確認envは利用者申告であり、契約そのものは検証できないとREADME/SPECに明記し、課金プラン変更後に解除するよう案内する。Google Unpaid tierのデータ利用/人手レビューなどプライバシー条件がある場合は別の明示確認を要求し、既定有効にしない。許可リスト方式で**提供元の一覧が当該 ID を返さない**場合（実測: Z.ai の `/models` は有料の旗艦だけを返し、$0 の Flash を返さない）は、公表された Free ID を**在庫として補う**（`synthesize_free_ids` + `_with_allowlisted_rows`。**既定は無効**で、一覧が許可リストの ID を返す提供元では補わない）。一覧に無いことを理由に Free を隠すと、その提供元は自動選抜に永久に入らない。**クレジット型（Vercel AI Gateway / Ollama Cloud / Mistral / ModelScope）はモデル単位の Free 判定ができず、無料枠はカタログのサブセット**（Vercel は認証付きの `include_availability` でしか対象を列挙できず、Ollama は starter の範囲を公表していない）ので、**公式に対象一覧を出しているものは許可リスト（Vercel = 実測 15 件）、出していないものはクレジット枠＋プラン確認 env**（Ollama）で扱い、サブセットである旨と 402 の扱い（フォールバック・記憶しない）を README/SPEC に明記する。

32. **第6段階は「検索できる公認 API」＋「利用条件が明確」の 2 条件を満たすものだけ**（§5.18/§5.19）。
    第6段階で採った 13 種のうち、**2026-10-05 の方針変更で生命・医学系 9 種（europepmc / uniprot /
    chembl / pdb / quickgo / reactome / clinicaltrials / openfda / gbif）を削除**した（対象は
    「科学（物理・数学・計算機）＋プログラミング」まで。医学・生物学は対象外）。残るのは
    osv / ietf / inspirehep / oeis / hfhub の 5 種で、いずれも既定 off の明示指定。**同一ホストに
    複数サービスがあるときは予算キーをサービス単位に分ける**（`_kb_new_json(..., budget_key=...)`。
    遮断の記憶は netloc のまま）。**構造化レコードしか無いソースは `summary_kind=structured_record`**
    を付け、論文抄録と混同させない。**キーワード検索 API が無いものは登録しない**
    （PubChem / AlphaFold DB / Ensembl / STRING）。**検索式を弱めて代替しない**: IETF の
    `title__contains` は AND 不可なので、RFC 番号と draft 名は `name` で直引きし、それ以外は最長語で
    引いて全語一致を優先する（全文検索だと書かない）。Stack Exchange は AUP が事前の書面許諾を要求する
    ので、実測 200 でも登録しない。Maven Central は新 ToS が商用インフラ利用を事前許諾なしに禁止する
    ため登録しない。

33. **第7段階は「世界中のプログラミング関連 API」から、検索でき・利用条件が明確なものだけ**（§5.20）。
    採用 3 種（hn / swh / librariesio）は既定 off の明示指定。hn は Algolia HN Search（帰属表示必須・
    10,000 req/h/IP。引用 URL は HN のアイテム URL とし、記事 URL は `extra.article_url` に残す）。
    swh は Software Heritage（ポイントアクセス可・大量抽出不可。origin を `summary_kind=structured_record`）。
    **キーが前提のソースは利用者自身のキーがあるときだけ動かす**（librariesio = `FREEAGENT_LIBRARIESIO_KEY`。
    未設定なら HTTP を出さずに登録先を案内。プロジェクトはキーを同梱・共有しない）。grep.app（429 チャレンジ）/
    searchcode.com（404）/ Gitee（11〜17 秒・トークン要）/ JSR・PyPI・pkg.go.dev（キーワード検索 API 無し）/
    Homebrew・Go module index（一括ダンプ）/ Zenn（非公式 API）は登録しない。Artifact Hub / MetaCPAN /
    RubyGems / Packagist / pub.dev / Hex / NuGet / Debian Sources は将来候補（集約の librariesio を採る
    場合は schema コストに見合わない）。

34. **無料一覧の非チャットモデルは自動選抜から外す**（§3.2）。無料一覧には埋め込み・ガード/安全分類器・
    画像/音声生成・OCR が混ざる（実測: Free 102 件中 30 件＝約 29%。`openrouter/google/lyria-3-*` は音楽生成、
    NVIDIA の `embed` / `guard` / `vision` / `parse` / `reward` / `riva-translate` 等）。これらは選抜されると
    1 回失敗して降格するまで枠と時間を浪費する（実測: `ok=0` のまま選ばれ続けた）。目印は**2 群に分ける**。
    **A 群（用途が対話でない: 埋め込み・再ランク・ガード/分類器・報酬・OCR/パース・音声認識）は出力
    モダリティの宣言より優先**する — 宣言が答えるのは「テキストを出力するか」だけなので、宣言を優先すると
    生きたガード/分類器が選抜に残り続ける（実測: `nvidia/nemotron-3.5-content-safety:free` は
    `output_modalities: ["text"]` と宣言されるが、返るのは "User Safety: safe" という分類結果で、`think` の
    検証者に選ばれて判定不能になった）。**B 群（視覚入出力・生成: `vision` / `vlm` / `lyria` / `diffusion` 等）は
    宣言があればそちらを優先**する（`...-vision-instruct` は視覚入力の対話モデル）。宣言が無い一覧
    （NVIDIA など）は両群の ID の**区切りに囲まれた語**だけで保守的に外す（`someembeddedmodel` のような
    部分一致で誤除外しない）。**明示指定は在庫の全 Free で確認する**ので名指ししたモデルはこれまでどおり
    試す。`freeagent_models` は `non_chat_candidates` に件数を出し、一覧では見えるままにする（黙って消さない）。

35. **変更内容（直近の更新内容）は README に書かず、リリース時に GitHub Releases へ記載する** — `README.md` / `SPEC.md` / `AGENTS.md` は「現行の仕様」を書く場所で、履歴は持たない。`pyproject.toml` の `version` を上げ、`vX.Y.Z` タグを打って同じ本文の Release を作成する（本文＝要約・変更点の箇条書き・検証行・`**Full Changelog**` リンク）。

36. **第8段階は Internet Archive のメタデータ検索だけ**（§5.21 / §6.11）。`advancedsearch.php` は実測
    0.44〜0.70 秒でキー不要だが、**素のクエリは順位が壊れる**（`q=FORTRAN manual` の 1 位が無関係な GitHub
    ミラー）ので `title:(語 AND 語)` に組み直す。**CJK クエリは HTTP を出さずに断る**（実測 10/10 が
    `QUERY_NOT_READY`。翻訳はサーバーでやらず、英語の語で引くのは呼び出し側の仕事として `sources` の
    説明に書く）。証拠は目録の注記なので `summary_kind=structured_record`。**全文検索 API は採らない**
    （5.9〜25.1 秒で 8 秒の締め切りに 3/8 超過・日本語 `total=0`）。CDX は 5.4〜30 秒、SPN2 と S3 は
    書き込みで利用者キーが前提。`inside.php` は OCR のあるアイテムに限られ主要経路にできない。

37. **第9段階は日本語の技術記事（Qiita）だけ**（§5.22 / §6.12）。`/api/v2/items?query=` はキー不要で
    実測 0.18〜0.36 秒。**間隔はトークンの有無で 2 定数**（github と同じ型）: 認証 1,000 回/時 → 3.6 秒、
    未認証 60 回/時 → 60.0 秒。未認証では同じ問いの再実行だけがキャッシュで即返り、別の問いは
    **ローカルの 429 として見える**（隠さない・規約 8）。**引用文は `rendered_body`（HTML）から作り、
    `body`（Markdown）を使わない** — `_plain_text` は `<[^>]+>` しか落とさないので画像記法が残る（実測）。
    証拠は `summary_kind=discussion`。**キー不要のソースなので、未設定でも HTTP を出して動かす**
    （規約 29 の「キー必須ソースは未設定なら HTTP を出さない」とは型が違う）。見送り（実測）: Reddit
    （未認証は 403 で遮断・事前審査必須）/ openBD（キーワード検索 API 無し）/ Botipedia（公開 API 無し）/
    Free Dictionary API（停止）。

38. **HTTP は「1 プロセス共有」のための経路で、既定は stdio**（§10）。単一メッセージには
    **JSON-RPC オブジェクト 1 個**で応答する（配列で返すと Hermes のクライアントが `Failed to parse JSON
    response` で切る）。通知には 202 を返し、**要求に 202 を返さない**。`initialize` の応答に
    `mcp-session-id` を付け、未知のセッションは 404。メソッド表は §9 の `_replies_for` に集約し、
    stdio と HTTP で分岐を作らない。**二重起動は失敗させる**（Windows の `SO_REUSEADDR` は同一ポートへの
    二重バインドを許すので `allow_reuse_address = False`）。HTTP では env の目印が渡らないため、ハーネス判別は
    `x-freeagent-harness` ヘッダを env より優先する。`http.server` / `argparse` は遅延 import（stdio の起動を
    重くしない）。検証は `python scripts/smoke_http.py` と `tests/test_http_transport.py`。

39. **既定ソースの並びは提供元の順位を鵜呑みにしない**（§5.1 / §6.13）。ja.wikipedia の素の検索は
    日本語の複合語で順位が崩れる（実測: 「大規模言語モデル」は記事があるのに 20 件中 19 位で、`gsrlimit=3` の
    既定では無関係な項目だけが返った）。**完全一致（`titles=` の lookup）だけを先頭へ昇格**させ、それ以外の
    並びは変えない（「タイトルに含む」での昇格は他を追い出す退行になる）。中身が空の extract は昇格させない。
    lookup は検索と**並列**に投げ（逐次 1.15 秒 → 並列 0.58 秒）、片方の失敗で全体を落とさない。演算子・
    引用符を含む問いと長すぎる問いでは投げない。

40. **言語の既定は問いから決める（wikipedia だけ）**（§5.1 / §6.14）。`lang` の既定を `ja` に固定すると、
    ASCII の問いが ja 索引に落ちて無関係な記事が根拠になる（実測: `Transformer` は ja で「ASUS Eee Pad TF101」、
    en で「Transformer (deep learning)」）。`_kb_auto_lang` は CJK→ja / それ以外→en の決定的な規則で、
    明示指定は尊重する。**wikidata には適用しない**（実測で ja の方が当たる）。未指定は `as_str(lang, "")` の
    **空文字**で伝える（`"ja"` を既定にすると自動判定に到達しない）。`lang` の description も同時に更新する（規約 5）。
    あわせて **`scripts/measure_relevance.py` で「速いが無関係」を測る** — `measure_kb.py`（時間）と
    `measure_adoption.py`（自発率）では検出できないクラスなので、知識ソースの並びを変えたら回して
    `--report` の的中率を見る（上流の順位変動があるので**失敗にはしない**）。

41. **KANBAN のカードとの紐づけは `card` で行い、完了は `kanban_*` に任せる**（§6.9 / §7.5.2）。
    `freeagent_think` の `card` は台帳の行に載せ、`view=true` + `card` で読み戻せる（`session_id` が優先・未知カードは
    推測せずエラー）。**渡された値が優先で、未指定なら既存を保つ**（同じカードを別の呼び出しが上書きしない）。
    **`card` は台帳の取っ手**にする: `session_id` 省略時はカードに紐づく既存台帳を継続する（ワーカーは
    カード ID しか持たないため。実測で `session_id` 省略の 2 手目が台帳を分断した）。明示の `session_id` が優先。
    `view` の `next_call.args` にも `card` を入れ、**別プロセス（メインセッション）からも同じ card で読める**ことを
    SPEC に書く（台帳は `thoughts.json` に落ちるため。実測済み）。テストの一時ディレクトリは `addCleanup` で消す。
    カードの終端（完了・ブロック）は Hermes の `kanban_complete` / `kanban_block` が行う — ワーカー系譜の MCP
    子プロセスは**ボードへの書き込みがフェンスで拒否される**ので、ここで代替を作らない（規約 30 と同じ「できない
    ことを別経路で偽装しない」）。description に分担を書く（規約 5）。新しいストアは作らない（既存の `thoughts.json`）。

42. **締切直前の代替取得は始めない**（§5.10）。代替（arXiv→DataCite）は残り時間が`KB_HEDGE_MIN_REMAINING`
    （既定0.05秒）以上のときだけ開始する。間に合わない取得を投げないため、かつ「呼び出しが返った後に代替が
    始まる」競合を消すため（実測: 締切0.005秒で20回に1回、判定を数ミリ秒すり抜けた。CIのwindows-latest/3.12が
    この経路で落ちていた）。下限を変えたら**両方向**を試す: 残り時間が僅かなら始めない／十分なら始まる。

43. **Windows の常駐配線は repo の生成器が作る**（§8.2 / `scripts/install_resident_windows.py`）。VBS に
    `WshShell.Exec("cmd /c …")` / `cmd /c …` を書かない — 子プロセスにコンソールが付き、Windows 11 では
    数分おきに**黒いウィンドウが一瞬出る**（実測: 5 分周期の見張りで毎回 1 個）。生存確認は WMI
    （`Win32_Process`）、起動は `pythonw.exe`、ログはファイル追記。二重起動の判定は launcher の目印と
    旧来のコマンドラインの両方に当てる。

44. **知識取得の読み取りは壁時計でも打ち切る**（§5.8）。`urlopen(timeout=…)` の上限は
    「ソケット 1 回の待ち」の上限であって**リクエスト全体の壁時計制限ではない**。1 バイトずつ
    drip する提供元では読みが終わるまで何分も待つ（実測: ローカル drip サーバで 98.0 秒 =
    `KB_TIMEOUT`（20 秒）の 5 倍、zenodo 実測 122.75 秒）。→ `_kb_read_within` で残りの壁時計
    予算を見ながら読む。終端判定は `Content-Length` で行う（`read1` は 1 バイトずつしか
    返さないことがある）。**`read(n)` は n バイト揃うまでブロックする**ので、大きな n を
    指定すると予算を突き抜ける。`KB_DEADLINE` の締め切りは待たずに返すので、利用者の
    待ち時間には影響しないが、裏の取得スレッドが長時間占用される点は直しておく。

45. **常駐 HTTP は `Origin` / `Host` を検証する**（§8.2）。MCP の StreamableHTTP は
    `Origin` ヘッダの検証を要求（仕様の MUST）。素通しだとブラウザの別ページから
    `http://127.0.0.1:8790/mcp` へ JSON-RPC を送れる（実測: 検証前は `Origin: https://evil.example` と
    `Host: evil.example` の両方で 200。`FREEAGENT_HTTP_TOKEN` 未設定なら認証ガードも無い）。
    → `Origin` が無い CLI は通し、待ち受ホストと一致するオリジンだけ許可（`Origin: null` は
    拒否）、`Host` も待ち受ホストと一致を要求する。待受が `0.0.0.0` のときは制限しない。
    **HTTP 経路の検証は CI にも載せる**（`smoke_http.py`）— stdio だけ回すと HTTP 側の退行が
    見えない。

46. **計画の id は「同一性」**（§2.7 / 思考台帳）。`plan` を差し替えても**同じ文面は同じ id と
    達成済みの印を引き継ぐ**。id を再利用すると、それまで `subgoal` で参照していたステップが
    別の項目を指すようになる（実測: `plan=["A","B"]` で A 完了 → `plan=["B"]` で B が id 1 に
    なり、ステップ 1 が A についてのものだったのに B に付け替わる）。新しい文面には
    **未使用の id**を割り当て、抜けた項目は**廃止**（`plan_retired`）として残す。廃止項目も
    `subgoal` の参照先として有効なので、**参照が宙に浮かない**。

47. **真偽引数の null は「未指定」**（思考台帳）。`as_flag(None)` は False なので、
    `as_flag(args.get("next_thought_needed", True))` と書くと、クライアントが null を送った
    だけで台帳が暗黙に終端する（実測: null → False / キー無し → True）。→ 明示された null は
    未指定扱い（既定）にする。**終端（false）にしたときは `next_call` を出さない** —「続けるか」を
    False にしておきながら次の思考を誘導しない。

48. **返り値のキーはモードで変えない**（§7）。`freeagent_models` は検索時と非検索時で
    返るキーが違い（実測: 引数なし → `free_models` のみ / `query` 付き → `models` のみ）、
    利用側がモードで別のキーを読む羽目になる。→ 必要なキーは**常に両方**返す。
    同様に、**指定したのに使えなかったソース名は content に出さない**（typo を黙って捨てない）。

49. **台帳はプロセス間で壊さない**（§2.6 / §6.16）。`thoughts.json` のロックは**プロセス内だけ**なので、
    同じ `FREEAGENT_STATE_DIR` を複数プロセスで共有すると**片方が上書きで他方の思考を消す**
    （実測: 4 プロセスの同時書き込みで 5 試行中 3 回で欠落）。→ **ファイルロック**
    （`thoughts.json.lock`・Windows は `msvcrt.locking` / POSIX は `fcntl.flock`）で直列化し、
    書き込む前に**直前のファイル内容と統合**する。統合は**集合もの（`steps`）＝番号の和集合**、
    **状態もの（`plan` / `plan_retired` / `branch_meta` / `total_history`）＝`updated_at` が新しい方**
    （計画の改訂は差し替えなので和集合にすると廃止項目が復活する）。番号の採番も
    **ファイル反映後の内容**に対して行う。**読み**は更新時刻（mtime）を見て**変わっていれば読み直す**
    （一度読んだままにすると、別プロセスの追記が永久に見えない。実測: 親の保存でファイル上から消えた）。
    台帳が無効（`FREEAGENT_THOUGHTS=0`）なら**ロックファイルを作らない**（状態を汚さない）。

50. **`subgoal` の番号は 1 つに決める**（§6.10）。`subgoal` は**plan 項目の `id`**を指す
    （同一性。差し替えをまたいでも変わらない）。id に一致する項目が無く**1 始まりの位置**に当たる
    場合は、その位置の項目として扱う（差し替えで id が飛び番号になるため）。**廃止済み項目**の id も
    参照できる（記録は残す）が、達成済みには-mark しない。どれにも当たらなければ**エラー**。
    旧実装は id だけで照合していたため、差し替えの後は**位置 1 の項目ではなく廃止済み項目を
    完了扱い**にしていた（実測: 20 回反復で 20 回再現）。→ 解決は `_think_structure` が一箇所で
    行う（`plan` 出力の `id` と `position` の両方を出すので、利用者はどちら。だから指示できるか分かる）。


51. **429 は「待つ」ではなく「再試行」**（§5.8 / §6.17）。crossref の 429 は**共有プールの
    Transientな拒否**で、間隔（0.4〜1.0 秒）不管で 10 回中 1〜3 回出る。→ 間隔では直らない。
    **再試行**で 10 試行すべて成功する（待つ必要なし）。429 を受けたら**再試行**し、
    **再試行しても駄目なときだけ**ホストを遮断する（遮断すると同じホストの他のソースまで巻き込む）。
    `Retry-After` があれば尊重するが**待つ上限**（`_KB_HTTP_429_MAX_WAIT_S`）を掛ける。
    **=`Retry-After` が無く 1 秒で回復する提供元と、`Retry-After` が 3476 秒ある
    openalex の日次予算切れは別物**（後者は間隔でも再試行でも直らない＝実装の外）。

52. **予算は「ホスト × サービス」単位で揃える**（§5.11 / §6.17）。GitHub の search 枠は
    **repo と issue で共通**（`X-RateLimit-Resource: search`。交互に叩くと Remaining が 9→9→8→8
    と減る）。エンドポイントごとに別キーにすると**同じホストの枠を奪い合う**
    （fan-out で成功 1/4）。→ 予算キーは `api.github.com/search` に揃える
    （`code` 検索は枠が違うので別キー）。**枠が埋まっているときは待たずに落とすのではなく待て**:
    待つのは「待ち時間が `_KB_RATE_WAIT_MAX_S` 以下」かつ「締め切り内に収まる」ときだけ。
    締め切りは `_kb_gather` が**スレッドローカル**で届ける（24 個の `kb_*` の signatures を変えない）。

53. **inflight の待ちには上限**（§5.8 / §6.17）。`_kb_cached` の `event.wait()` に上限が無いと、
    取得に 14 秒かかるソースで**利用者を 13.8 秒待たせる**（`KB_DEADLINE`=8 秒の 1.7 倍）。
    → 上限（`_KB_INFLIGHT_WAIT_MAX_S`、既定は `KB_DEADLINE`）を超えたら**待たずに自分で取得**する。

54. **モデル一覧は並列・締め切り・プロセス間キャッシュで取る**（§3.3 / SPEC §4.4）。ここは
    サブLLM のツールが選抜の前に必ず通るので、**直列だと遅いプロバイダ 1 つの待ちが利用者の待ちになる**
    （実測: 冷起動の直列 0.842 秒・1 リクエストの上限は 30 秒）。取得はメモリ → ディスク
    （`model_catalog.json`・TTL 600 秒）→ HTTP の順で、プロバイダごとに専用スレッドへ投げ
    `FREEAGENT_CATALOG_DEADLINE`（既定 10 秒）で打ち切る。**間に合わなかった分は前回の成功値で
    埋め、取得の「失敗」は埋めない**（締め切り超過＝分からない / 失敗＝分かった上で取れない。規約 8）。
    **書くのは成功したときだけ**（規約 21。`check_offline.py` が状態ディレクトリの空を検査する）。
    同じプロバイダを二重に取りに行かない（`_MODELS_INFLIGHT` の単一飛行）。実測: 新しいプロセスで
    最初の選抜まで 0.31〜0.75 秒 → **0.0025 秒**。「選抜を知識取得と重ねる」案は**採らない**
    （早期返却の経路で提供元へ一覧要求を出す契約になり、`test_knowledge_extensions.py` が固定している）。

55. **上流への同時実行は提供元ごとのゲートで制限する**（§3.4 / SPEC §5.1）。プロセス全体の
    `FREEAGENT_MAX_WORKERS` だけでは、Free の在庫が 1 提供元に偏ったときに
    「並列度を上げる＝同じ提供元へ同時に投げる」になり 429 を踏む（実測: ready が `nous` 1 社・
    Free 8 体）。**枠を超えた呼び出しは待つ**（429 にしない）が、待ちには上限
    （`FREEAGENT_GATE_WAIT_S`）を置き、過ぎたら `ProviderBusyError` で**脱落として返す**
    （無限待ちを作らない）。待ち予算は **1 回の `call_model` で共有**する（候補ごとに満額待つと
    「候補数 × 上限」待つ）。429 では枠を 1 本減らし（下限 1）、連続成功で 1 本戻す
    （縮小専用にすると一時的な 429 で永久に痩せる）。枠は**プロセス内のカウンタ**なので、
    プロセスを増やした分だけ上流への合計は増える（README の「並列ワーカーとプロセスの数」）。
    枠の実測（8 モデル panel）: 8 で 429 が 1〜2 件 / 6 で 0〜1 件 / 4 で 1 件。
    **429 の主因はモデル単位の upstream capacity** なので、枠は 429 を 0 にする保証ではない。
56. **`size` は「自動選抜の目標本数」で、明示指定を切り落とす道具にしない**（SPEC §5.1）。
    実測: `models` に 8 件渡しても `size` の既定 3 で 3 件しか走らず、「在庫を全部束ねる」が
    できなかった（旧 3 件 → 新 8 件）。`size` の上限は `FREEAGENT_MAX_SUB_MODELS`（既定 8）、
    `models` 配列と `models: ["all"]` の展開上限は `FREEAGENT_MAX_MODELS_PER_CALL`（既定 16）。
    `["all"]` は**対話モデルのみ**を在庫から取る（埋め込み・画像生成を混ぜない）。fanout の
    合計呼び出し上限（`FREEAGENT_MAX_CALLS_PER_RUN`）は `["all"]` でも効かせる。
    観測値（`concurrency` / `slot_waited`）は**提供元名**で返す（ref をそのままキーにすると
    架空の提供元が並ぶ。実測）。

57. **選抜は Beta 事後を決定的に使う**（SPEC §5.2）。`score = mean + 0.25·sd`（`a=1+成功, b=1+失敗`）で、
    成功率は **kind ごとに n で重み付け**して合算する。旧実装は kind の単純平均に `0.25/(1+n)` を足し、
    **観測 1 件の成功が n=50・90% を上回り**、同点はアルファベット順で決まっていた（実測: 本番 traces で
    旧順位と事後平均順の上位 8 が 0/8 一致）。乱数は使わない（同じ入力で同じ順位＝テストと説明が再現する）。
    `model_quality` / `model_status`（利用者向けの表示）は意味を変えない。順位だけが変わる。
58. **クォータは宣言制で、宣言が無ければ状態を書かない**（SPEC §5.2）。`FREEAGENT_QUOTA_LIMITS` に
    書いた提供元だけを使い切る前に止める（`rpm` / `rpd` / `tpm` / `tpd`、日は **UTC 境界**）。
    宣言が無いときに `quota.json` を作ると `check_offline.py` の「状態を書かない」契約が壊れる。
    同時実行の枠（規約 55）は**同時数**、クォータは**窓の量**で、片方だけでは守れない。
59. **429 のクールダウンははしごを上る**（60 → 180 → 600 秒・上限 900）。1 回目は `Retry-After` を
    尊重する（短く指定されたら従う）。**冷えてから再度 429 なら 1 段目に戻す**。一律 60 秒だと
    「毎分 1 回叩いて毎回 429」になり、相手の枠を無駄に食う（実測: 8 モデルの panel で 1〜2 件の 429）。

60. **外部プロバイダは「無認証 GET /models の実測」と「無料枠の文書」の両方を通ったものだけ足す**
    （SPEC §4.1 / §4.1.1）。判定は 200＋JSON（公開カタログ）/ 401・403（存在してキーが要る）/
    404・例外（廃止）。**200 は無料枠の証拠ではない**（実測: 有料リレーも 200 を返す）。
    無料枠を API が返さない提供元は `free_kind="credit"` ＋ `required_env_flags` で**利用者の申告**にし、
    対象は `probe` で確認する（規約 31）。**非公式リレー・再配布サービスは足さない**（規約 33。
    実測: `freellmapi` の 47 実装のうち xkiro / navy / ainative / llm7 / kilo / routeway / requesty 等）。
    追加は `PROVIDER_ORDER` の**末尾**（オプトイン）に置き、`tests/test_freeagent_bind.py` の
    `test_registry_has_eighteen_providers` を同じ変更で更新する。

## ライセンス

MIT。データは各提供元の条件に従い、回答には出典を表示すること。