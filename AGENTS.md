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
python -m unittest discover -s tests         # オフライン回帰（47 件・ネットワーク不要）
python scripts/smoke_stdio.py                # 実クライアント経路（initialize/tools/list/tools/call）
FREEAGENT_PROBE_NET=1 python scripts/smoke_stdio.py   # バックエンド生存（プロキシが要る）
```

終了コード 0 が正常。1 は失敗（レジストリ不一致・版不一致・例外漏れ・UTF-8 破綻・プロトコル破綻）。

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
16. ツールを増減したら `README.md` のツール表・`SPEC.md`・`tests/`・`scripts/` を同一変更内で更新する。

## ライセンス

MIT。データは各提供元の条件に従い、回答には出典を表示すること。