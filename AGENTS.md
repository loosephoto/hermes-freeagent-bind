# AGENTS.md — hermes-freeagent-bind

Hermes Agent の **Free モデルをサブ LLM として並列に走らせる** MCP サーバー（Python 3.11+ / 実行時依存ゼロ /
単一ファイルのモノリス）。メイン LLM の知識補助として **arXiv / Crossref / OpenAlex / Wikipedia /
Wikidata / GitHub** を既定で引き、**DataCite / OpenAIRE / Europe PMC / Zenodo / ROR** は明示指定で追加する。

- 全体像は `README.md`、**実装の契約は `SPEC.md`**（変更時は両方を同一変更内で更新する）。
- 本体は `src/freeagent_bind/server.py` の 1 ファイルで、**§0〜§9 の区画**に分かれている。
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
python scripts/probe_knowledge_stdio.py      # 新規知識ソースの実API・stdio（ネットワークあり）
python scripts/check_offline.py              # バックエンド全滅: 例外漏れ・ハング・状態汚染が無いこと
python scripts/measure_adoption.py           # 自発利用率の測定（state.db を読むだけ・副作用なし。必要場面の近似判定も出す）
python scripts/measure_kb.py --report         # 知識バックエンドの時間帯別の応答時間（記録を読むだけ）
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
   §3 の共有 `_OPENER`（接続の間だけ接続上限、接続後は読み取り上限）を使う。遮断はホスト単位で記憶して fail fast。
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
    ものを永久に隠さない）。生存確認は `allow_fallback=False`（有効だと他プロバイダの応答が「生存」に化ける）。
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
    設定済みの除外の空振りを exit 1 で検出する。
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
    既存ソースと重複するもの（PubMed / bioRxiv ⊂ Europe PMC）、レートが8秒締切と不整合なもの（PLOS 10req/min）、
    条件曖昧なもの（HAL 非商用条項）は登録しない。DOAJはパス埋め込み検索なので検索語を必ずURLエスケープする。

31. **Free推論プロバイダは課金tierを推測しない**。APIがFree/paid tierを返さない場合は、モデルID allowlist・明示確認env・厳密な未許可model拒否を組み合わせる。確認envは利用者申告であり、契約そのものは検証できないとREADME/SPECに明記し、課金プラン変更後に解除するよう案内する。Google Unpaid tierのデータ利用/人手レビューなどプライバシー条件がある場合は別の明示確認を要求し、既定有効にしない。

## ライセンス

MIT。データは各提供元の条件に従い、回答には出典を表示すること。