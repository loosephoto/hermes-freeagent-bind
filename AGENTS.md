# AGENTS.md — hermes-freeagent-bind

Hermes Agent の **Free モデルをサブ LLM として並列に走らせる** MCP サーバー（Python 3.11+ / 実行時依存ゼロ /
単一ファイルのモノリス）。メイン LLM の知識補助として **arXiv / Crossref / OpenAlex / Wikipedia /
Wikidata / GitHub** を引く。

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
python -m unittest discover -s tests         # オフライン回帰（85 件・ネットワーク不要）
python scripts/smoke_stdio.py                # 実クライアント経路（initialize/tools/list/tools/call）
python scripts/check_offline.py              # バックエンド全滅: 例外漏れ・ハング・状態汚染が無いこと
python scripts/measure_adoption.py           # 自発利用率の測定（state.db を読むだけ・副作用なし）
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
9. 外部 HTTP は **(connect, read) のタイムアウト必須**。遮断はホスト単位で記憶して fail fast。
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

## ライセンス

MIT。データは各提供元の条件に従い、回答には出典を表示すること。