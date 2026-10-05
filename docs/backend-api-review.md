# 推論バックエンド / 知識API 代替候補レビュー

対象: hermes-freeagent-bind
目的: 推論バックエンド（LLM プロバイダ）をより良い API で代用できるか、および知識ソースに
プログラミング・科学分野向けの API を追加できるかを、世界中の候補から洗い出して検討する。
制約: **Wikipedia / Wikidata / GitHub は現行のまま残す**（本レビューで置換対象にしない）。

本レビューの数値・可否は、実 API への無認証プローブ（`probe_apis.py` / `probe_apis2.py`、
2026-10 実行）と公式ドキュメントの突合による。推論の「無料枠での実際の推論可否」は
API キーが無いため未検証（残課題 §7）。

**第2ラウンド（世界規模スキャン, 2026-10-04 追記）**: 「世界中から」という観点を足し、米欧以外
（中国・日本・韓国・東南アジア・インド・中東・露・中南米・アフリカ）を含む推論 API と知識 API を
`probe_world.py` / `probe_world2.py`（無認証・並列・約 150 ホスト）で実測した。結果は §8（推論）/
§9（知識）/ §10（統合方針の追補）にまとめる。要点は「推論は中国勢と Ollama Cloud・Typhoon が
実質的な追加候補、知識はプログラミング 24 ソースと科学 30+ ソース、加えて SciELO / CiNii / AJOL /
NOPR という非欧米圏の一次情報が無認証で使える」。Wikipedia / Wikidata / GitHub は従来どおり維持する。

---

## 0. 結論サマリ（TL;DR）

### 推論バックエンド
- **現行 7 プロバイダ（nous / openrouter / nvidia / huggingface / groq / cloudflare / gemini）は
  置換不要**。役割が重複しない補完として **オプトインで 4 候補を追加**するのが妥当。
- 追加候補（優先順）:
  1. **Z.ai（Zhipu GLM）** — `glm-4.7-flash` / `glm-4.5-flash` / `glm-4.6v-flash` が API 上**永久無料**。
     OpenAI 互換。無料枠が広く fan-out に向く。
  2. **Mistral La Plateforme** — Experiment tier が**約 10 億トークン/月**。カード不要（電話確認必須）。
     RPM が低く並列には不向きだが、トークン総量が大きい。
  3. **SambaNova Cloud** — Free tier は **20 RPM / 20 RPD / 200,000 TPD（モデル毎）**、カード不要。
     RPM はあるが **RPD 20 が fan-out には極端に少ない**（低優先の非常用）。
  4. **Cohere** — トライアルキー無料（20 RPM / 1,000 コール/月）。ただし**非商用限定**（条件付き採用）。
- **棄却**: GitHub Models（**2026-07-30 に完全廃止**）/ Cerebras・Together（支払必須）/
  Chutes（有料化）/ DeepInfra（永久無料枠なし）。

### 知識 API
- 既存 6＋8 ソースは維持。**プログラミング系**に Stack Exchange / OSV.dev / deps.dev（＋ PyPI 等）、
  **科学系**に PubMed / PubChem / UniProt / RCSB PDB / INSPIRE-HEP / bioRxiv / OpenFDA / OpenML /
  Hugging Face Hub を**オプトイン（明示指定）で追加**する価値が高い。すべて無認証で 200 を実測。
- **棄却**: Papers with Code（API 廃止・HTML を返す）/ Libraries.io（要キー）。
- **条件付き**: Semantic Scholar（匿名は共有プールで不安定・実測 429。キーは無料メール不可）、
  ChEMBL（実測 16 秒タイムアウト）、CORE（無認証でも通ったが v3 はキー推奨）。

### 世界規模スキャン（第2ラウンド, 2026-10-04）
- **推論**: 中国勢（Z.ai / Zhipu 本土 / ModelScope / SiliconFlow / DashScope）と **Ollama Cloud**・
  **Typhoon（タイ）** が実質的な追加候補。特に **ModelScope は無認証で 200 の公開カタログ**、Z.ai は
  GLM Flash が**永久 $0**。一方で **Yi(01.AI) は 410 でサービス終了**、OctoAI / Lepton / Jais は DNS 死。
  日本・韓国・欧州・露の各社は**有料中心**で無料枠が乏しく、オプトインでも価値は限定的。
- **知識（プログラミング）**: Stack Exchange / OSV / deps.dev / PyPI / Maven / Go proxy / Packagist /
  RubyGems / NuGet / npm / crates / **MDN** / **RFC Editor** / **IETF datatracker** / **Unicode** /
  Codeberg / GitLab / SPDX / **NVD** / CIRCL / Repology / OpenSSF Scorecard / HN Algolia が無認証 200。
- **知識（科学）**: 既出の PubMed / PubChem / UniProt / RCSB PDB / INSPIRE-HEP / bioRxiv / openFDA /
  OpenML / HF Hub に加え、**PDBe / Ensembl / AlphaFold DB / OEIS / KEGG / STRING / GBIF / EBI OLS /
  Europe PMC / zbMATH / SIMBAD TAP / Reactome / ChEMBL / ClinicalTrials.gov / EBI BioStudies /
  Gene Ontology / QuickGO / InterPro / PRIDE / BioModels / OPSIN / PBDB / JPL Horizons /
  NASA Exoplanet Archive / USGS / MPC / LMFDB / MAST** が無認証 200。
- **知識（非欧米圏）**: **SciELO（中南米・南ア）**、**CiNii Research（日本・JSON-LD）**、
  **AJOL（アフリカ・OAI-PMH）**、**NOPR（インド・OAI-PMH）** が無認証で 200。Europeana（欧州文化・
  demo key）も可。J-STAGE は公開 API 無し、CNKI / AMiner / KISTI は有料/要キー。

---

## 1. 評価軸

freeagent-bind の実装契約（AGENTS.md / SPEC.md）から、候補は次のすべてを満たす必要がある。

1. **実行時依存ゼロ**（標準ライブラリ `urllib` のみで叩ける。独自 SDK 前提は不可）
2. **推論は OpenAI 互換**（`/v1/chat/completions` + `/v1/models`。`PROVIDER_SPECS` の形に載る）
3. **無料枠が実在**（free-tier 限定ポリシー。有料専用・要課金は対象外）
4. **キー取得が容易**（無認証 > 無料キー > 電話/実名確認。課金・法人契約は不可）
5. **オプトイン**（既定 off。`required_env_flags` 等で明示確認を要求する現行方式に従う）
6. **科学 / プログラミング分野のカバレッジ**（既存ソースと重複しないこと）
7. **レート制限と利用条件が許容範囲**（商用可否・データ利用条件を含む）

---

## 2. 現行の棚卸し

### 推論（`PROVIDER_SPECS`）
| provider | base_url | 無料判定 | 備考 |
|---|---|---|---|
| nous | hermes proxy (127.0.0.1:8645/v1) | pricing | 資格情報を代理付与・キー不要 |
| openrouter | openrouter.ai/api/v1 | pricing | `:free` SKU。一覧は未認証可 |
| nvidia | integrate.api.nvidia.com/v1 | credit | 一覧に廃止・未有効が混在 → probe 必須 |
| huggingface | router.huggingface.co/v1 | hf_providers | 提供元単位の free 判定 |
| groq | api.groq.com/openai/v1 | allowlist | Free tier 確認必須 |
| cloudflare | api.cloudflare.com/.../ai | allowlist | Workers Free 確認必須 |
| gemini | generativelanguage.googleapis.com/v1beta/openai | allowlist | tier とデータ利用の二重確認 |

### 知識（`KB_BACKENDS`）
- 既定 6: `wikipedia` / `wikidata` / `arxiv` / `crossref` / `openalex` / `github`
- オプトイン 8: `datacite` / `openaire` / `europepmc` / `zenodo` / `ror` / `doaj` / `npm` / `crates`

登録は `kb_<name>(query, limit, opts)` を実装し、`KB_BACKENDS["name"] = lambda ...` と
`SOURCES = (*SOURCES, "name")` を追記する方式（既定に入れないものは `DEFAULT_SOURCES` に足さない）。
新規ソースは `_kb_new_cached(key, source, producer)`、レート制限が厳しいホストは
`_kb_new_json(url, interval)`（ホスト単位の直列化）を使う。

---

## 3. 推論バックエンド候補

### 3.1 採用候補

#### (1) Z.ai / Zhipu GLM — 優先度: 高
- base_url（OpenAI 互換）: `https://api.z.ai/api/paas/v4`（`/api/openai/v1` も可）
- 無料モデル: `glm-4.7-flash` / `glm-4.5-flash` / `glm-4.6v-flash`（入力・出力とも 0）
- 実プローブ: `GET /api/paas/v4/models` → **401（存在確認）**
- 特徴: **永久無料**、無料枠が広い（目安 約 1 req/s。実名確認あり）。fan-out と相性が最も良い。
- 注意: 実名確認・キー発行が前提。無料枠の正確な上限はコンソール表示に依存。

#### (2) Mistral La Plateforme — 優先度: 中
- base_url: `https://api.mistral.ai/v1`
- 無料枠: Experiment tier（**約 10 億トークン/月**、カード不要・**電話確認必須**、RPM 低め ≈ 2）
- 実プローブ: `GET /v1/models` → **401（存在確認）**
- 特徴: トークン総量が大きい。RPM が低いので「並列 fan-out の本数」ではなく
  「長文・少数リクエスト」に向く。Codestral などコード系も同 tier。

#### (3) SambaNova Cloud — 優先度: 低（非常用）
- base_url: `https://api.sambanova.ai/v1`
- Free tier（公式）: **20 RPM / 20 RPD / 200,000 TPD（モデル毎）**、カード不要
  （モデル: `DeepSeek-V3.1` / `Meta-Llama-3.3-70B-Instruct` / `gpt-oss-120b`）
- 実プローブ: `GET /v1/models` → **200（無認証で価格つきカタログが公開）**
- 注意: **RPD 20（1 日 20 リクエスト）は fan-out 型サーバーには実質使えない**。
  「たまの単発フォールバック」用途に限定すべき。Developer tier（20M TPD）は課金必須なので対象外。

#### (4) Cohere — 優先度: 条件付き（非商用）
- base_url（OpenAI 互換）: `https://api.cohere.ai/compatibility/v1`
- 無料枠: トライアルキー（**20 RPM / 1,000 コール/月**、**非商用限定**）
- 実プローブ: `GET /v1/models`（`api.cohere.com`）→ **401（存在確認）**
- 注意: 「無料」だが **1,000 コール/月**で、規約上**本番・商用利用は不可**。採用するなら
  `required_env_flags` で非商用確認を必須にし、既定 off を厳守する。

### 3.2 棄却・保留

| 候補 | 判定 | 理由 |
|---|---|---|
| **GitHub Models** | **棄却** | **2026-07-30 に完全廃止**（公式ドキュメント明記）。`models.github.ai` の 200 は残骸スタブで推論不可 |
| Cerebras | 棄却 | $5 トライアルのみ。永久無料枠なし（要課金） |
| Together AI | 棄却 | $5 最低入金が必要で free ではない |
| Chutes | 棄却 | 有料化（$3/月 base の pay-as-you-go）。「no longer free」 |
| DeepInfra | 棄却 | 永久無料枠なし（トライアルクレジットのみ） |
| Novita | 保留 | $0 価格モデルが回転で存在＋$0.50 トライアル。ただし無料モデルが入れ替わるため固定 ID を保証できない |
| Nebius / Hyperbolic | 保留 | サインアップクレジット型。無料枠が一時的 |

### 3.3 代用の可否（結論）
- **「現行を置き換える」必要はない**。nous（キー不要のローカル proxy）が主系で、
  他は補完という現行設計が妥当。
- **「追加で代用する」ことは可能**。上記 4 候補はすべて `PROVIDER_SPECS` の形
  （`base_url` / `key` / `key_env` / `free_kind` / `required_env_flags`）にそのまま載る。
- ただし **fan-out 用途での実効容量**を最優先すると、実際に効くのは
  **Z.ai（広い無料枠）＞ Mistral（大量トークン）＞ Cohere（非商用・少量）＞ SambaNova（RPD20）**。
  SambaNova を「無料枠があるから」と主力に据えるのは誤り（検証者モデルの指摘どおり）。

---

## 4. 知識 API 候補

すべて無認証で実プローブし、**200 で実データが返ったもの**を採用候補とした。

### 4.1 プログラミング分野

| API | 認証 | レート | 実プローブ | 判定 |
|---|---|---|---|---|
| **Stack Exchange API v2.3** | 不要（任意キー） | 300/日（キー 10,000/日） | 200（SO 検索） | **採用** |
| **OSV.dev** | 不要 | 制限なし（公称） | 200（POST 脆弱性照会） | **採用** |
| **deps.dev** | 不要 | 公称なし | 200（依存・ライセンス・勧告） | **採用** |
| **PyPI JSON API** | 不要 | 公称なし | 200 | **採用**（npm/crates の姉妹） |
| Maven Central Search | 不要 | 公称なし | 200 | 任意 |
| Go module proxy | 不要 | 公称なし | 200（テキスト） | 任意 |
| Packagist / RubyGems / NuGet | 不要 | 公称なし | 200 | 任意 |
| Libraries.io | **要キー** | — | 401 | 棄却 |
| Papers with Code | — | — | HTML（**API 廃止**） | 棄却 |

- **Stack Exchange** は「実装上の落とし穴・エラーメッセージ・定番手法」の一次情報として、
  GitHub（コード）・arXiv（論文）を補完する。gzip 応答のデコードに注意（要 `Accept-Encoding` 対応）。
- **OSV.dev / deps.dev** は「このパッケージ版は既知脆弱性の影響を受けるか」に**確定的に**答える。
  セキュリティ文脈でサブ LLM に渡す根拠として価値が高い（幻覚が入らない）。

### 4.2 科学分野

| API | 分野 | 認証 | 実プローブ | 判定 |
|---|---|---|---|---|
| **PubMed E-utilities (NCBI)** | 生命・医学 | 不要（任意キー） | 200 | **採用** |
| **PubChem PUG REST** | 化学 | 不要 | 200（分子式・分子量） | **採用** |
| **UniProt REST** | タンパク質 | 不要 | 200 | **採用** |
| **RCSB PDB** | 構造生物学 | 不要 | 200 | **採用** |
| **INSPIRE-HEP** | 素粒子物理 | 不要 | 200 | **採用** |
| **bioRxiv / medRxiv** | プレプリント | 不要 | 200 | **採用** |
| **OpenFDA** | 医薬・医療機器 | 不要 | 200 | **採用** |
| **OpenML** | 機械学習データ | 不要 | 200 | **採用** |
| **Hugging Face Hub API** | モデル / データセット | 不要（公開分） | 200 | **採用** |
| Semantic Scholar | 全分野 | 匿名共有プール / 無料キー | **429（不安定）** | 条件付き |
| CORE | OA 全文 | 無認証でも 200 / v3 はキー推奨 | 200 | 条件付き |
| ChEMBL | 創薬・生物活性 | 不要 | **タイムアウト（16s）** | 条件付き |
| OpenCitations | 引用リンク | 不要 | 200 | 任意 |
| Ensembl REST | ゲノム | 不要 | 200 | 任意 |
| AlphaFold DB | タンパク質構造 | 不要 | 200 | 任意 |
| OEIS | 数学（整数列） | 不要 | 200 | 任意 |
| Unpaywall | OA 版探索 | **メール必須** | 422（ダミー） | 任意 |
| Materials Project | 材料 | 要キー | 401 | 任意（キー） |
| NASA ADS | 天文学 | 要トークン | 401 | 任意（キー） |

- **Semantic Scholar** は匿名だと全世界で 1 つのキーを共有するプールで、実測でも 429 だった。
  無料キーは**フリーメールドメイン不可・審査に約 1 か月**のため、既定ソースには不向き。
  「キーがある人だけ使えるオプトイン」に留めるのが妥当。
- **PubMed / PubChem / UniProt / RCSB PDB / INSPIRE-HEP** は無認証・安定で、科学分野の
  一次データとして最も費用対効果が高い。
- **Hugging Face Hub** は推論で既に HF を使っており、知識側でもモデル/データセット検索が可能。
  追加の認証を要さない点で相性が良い。

---

## 5. 統合方針（実装する場合）

### 5.1 推論プロバイダ（`PROVIDER_SPECS` に追記）
既存の `groq` / `cloudflare` と同じ「allowlist + 明示確認」パターンで足す。例（Z.ai）:

```python
"zai": {
    "base_url": os.environ.get("FREEAGENT_ZAI_BASE_URL", "https://api.z.ai/api/paas/v4"),
    "key": os.environ.get("ZAI_API_KEY", ""),
    "key_env": "ZAI_API_KEY", "free_kind": "allowlist", "always_ready": False,
    "free_model_ids": ("glm-4.7-flash", "glm-4.5-flash", "glm-4.6v-flash"),
    "required_env_flags": ("FREEAGENT_ZAI_FREE_TIER",), "catalog_requires_ready": True,
    "note": "GLM Flash は API 上無料。無料枠の正確な上限はコンソール表示に依存",
},
```
`PROVIDER_ORDER` の既定文字列にも追記する。`scripts/probe_providers.py` / `warmup_models.py`
で生存確認を回し、`tests/test_provider_<name>.py` を追加する。

### 5.2 知識ソース（`kb_<name>` + `KB_BACKENDS` に追記）
既存の §5.9〜§5.16 と同じ形。**新しい § を立てる**（既存行を書き換えない）。

```python
# §5.17 プログラミング系（Stack Exchange / OSV / deps.dev）
def kb_stackexchange(query, limit=3): ...
def kb_osv(query, limit=3): ...          # POST /v1/query
def kb_depsdev(query, limit=3): ...

SOURCES = (*SOURCES, "stackexchange", "osv", "depsdev")
KB_BACKENDS.update({
    "stackexchange": lambda q, limit, opts: kb_stackexchange(q, limit),
    "osv": lambda q, limit, opts: kb_osv(q, limit),
    "depsdev": lambda q, limit, opts: kb_depsdev(q, limit),
})

# §5.18 科学系（PubMed / PubChem / UniProt / RCSB PDB / INSPIRE-HEP / bioRxiv / OpenFDA / OpenML / HF Hub）
...
```
- `DEFAULT_SOURCES` には**足さない**（既定 6 ソースを維持し、明示指定のみで追加）。
- レート制限が厳しいホストは `_kb_new_json(url, interval)` を使う
  （Stack Exchange は gzip 応答の展開が必要なので、既存の `kb_http` に gzip 対応を足すか、
  専用ヘルパで `Accept-Encoding: identity` を送る）。
- POST 系（OSV）は `kb_http` が GET 前提のため、ボディ送信の小ヘルパを 1 つ足す。
- 各ソースに `_cite(...)` で URL つき出典を必ず付ける（citation 統合 `_kb_merge_citations` に乗る）。
- `scripts/probe_knowledge_stdio.py` に新ソースの実 API 疎通を追加し、
  `tests/test_knowledge_stage*.py` に回帰を足す。

### 5.3 実装時のゲート（AGENTS.md 準拠）
```
python -m compileall -q src/freeagent_bind
python scripts/check_integrity.py
python -m unittest discover -s tests
python scripts/smoke_stdio.py
python scripts/probe_knowledge_stdio.py
```
README.md と SPEC.md を同一変更内で更新（§目次・ソース一覧・`sources` の説明）。

---

## 6. 実プローブ生データ（抜粋）

`probe_apis.py`（無認証・並列）:
```
[200] stackexchange   {"items":[{"tags":["python","multithreading",...} ...
[200] osv.dev         {"vulns":[{"id":"GHSA-462w-v97r-4m45","summary":"Jinja2 sandbox escape ...
[200] deps.dev        {"packageKey":{"system":"NPM","name":"react"},"versions":[...
[200] pypi            {"info":{"author":"Kenneth Reitz ...
[200] maven-central   {"responseHeader":{"status":0,...
[200] packagist       {"results":[{"name":"monolog/monolog",...
[200] rubygems        [{"documentation_uri":"https://api.rubyonrails.org/v8.1.4/",...
[200] nuget           {"@id":"https://api.nuget.org/v3/registration5-semver1/newtonsoft.json/index.json",...
[401] libraries.io    {"error":"An API key is required. ...
[429] semantic-scholar {"message":"Too Many Requests. ..."}
[200] pubchem         {"PropertyTable":{"Properties":[{"CID":2244,"MolecularFormula":"C9H8O4",...
[200] uniprot         {"results":[{"entryType":"UniProtKB reviewed (Swiss-Prot)","primaryAccession":"P01308",...
[  0] chembl          TimeoutError: The read operation timed out
[200] rcsb-pdb        {"audit_author":[{"name":"Fermi, G."},...
[200] inspire-hep     {"hits":{"hits":[{"id":"2181837",...
[200] biorxiv         {"messages":[{"status":"no posts found"}],"collection":[]}
[200] openfda         {"meta":{"disclaimer":"Do not rely on openFDA ...
[200] huggingface-hub [{... "id":"meta-llama/Llama-3.1-8B-Instruct",...
[200] openml          {"data":{"dataset":[{"did":2,"name":"anneal",...
[401] nasa-ads        {"message":"Missing \"Authorization\" in headers."}
[200] infer-sambanova {"data":[{"context_length":131072,"id":"DeepSeek-V3.1",...
[403] infer-cerebras  {"detail":"Not authenticated"}
[401] infer-mistral   {"detail":"Invalid API Key"}
[401] infer-zai       {"error":{"code":"1001","message":"Authentication parameter not received ...
[200] infer-deepinfra {"object":"list","data":[{"id":"google/gemma-3-27b-it",...
[200] infer-novita    {"data":[{"created":...,"id":"zai-org/glm-5.3-flash",...
```

`probe_apis2.py`:
```
[200] gh-models-new   OK                         ← 残骸スタブ（2026-07-30 廃止）
[  0] gh-models-legacy URLError: getaddrinfo failed
[200] openrouter-models / [200] nvidia-models    ← 一覧は未認証で公開
[200] pubmed-eutils   {"esearchresult":{"count":"71429",...
[422] unpaywall       {"message":"Please use your own email address ...
[200] core            {"totalHits":184971,...
[200] alphafold       [{"modelEntityId":"AF-P01308-F1",...
[200] ensembl         {"display_name":"BRCA2","id":"ENSG00000139618",...
[401] materials-project {"message":"No API key found in request"}
[200] paperswithcode  <!doctype html> ...        ← API 廃止
[200] oeis            [{"number":55,"data":"1,1,1,1,2,3,6,11,23,...
[200] hf-datasets     [{"id":"stanfordnlp/imdb",...
[200] opencitations   [{"oci":"06010048871-06120344846",...
```

---

## 7. 残課題・未検証（正直な限界）

1. **無料枠での実推論は未検証**。API キーがこの環境に無いため、`/v1/models` の取得可否までしか
   確認できていない。採用前に各社キーで `chat/completions` を 1 回叩き、無料枠が実際に通ることを
   確認すること（検証者モデルの指摘: 「カタログ取得成功 ≠ 無料枠の推論可否」）。
2. **利用条件（商用可否・データ利用・再配布）は未検証**。少なくとも Cohere は**非商用限定**、
   Gemini / Mistral の無料枠は**入力が製品改善に使われ得る**。追加時は `required_env_flags` に
   明示確認を要求し、README に注意書きを入れる。
3. **OpenAI 互換でも差異がある**（モデル ID、画像入力、トークン計上、ストリーミング、
   tool calling）。`free_model_ids` は各社の live カタログで再確認する。
4. **Z.ai の無料枠上限**は公開情報が割れており（「無制限・約 1 req/s」「実名確認後」）、
   コンソール表示が唯一の正。実キーで確認する。
5. **Stack Exchange の gzip 応答**と **OSV の POST** は現行 `kb_http`（GET 前提）に
   そのままでは載らない。小ヘルパの追加が必要。
6. 本レビューは独立モデル 2 体の反証（freeagent_think の verify）を通しており、
   「無料枠の実証不足」「商用条件の未検証」の 2 点を指摘として反映済み。

---

付録: 判定に使った生データは `probe_apis.py` / `probe_apis2.py`（Hermes scratch 領域）で再現可能。

---

## 8. 世界規模スキャン: 推論バックエンド（2026-10 第2ラウンド実測）

`probe_world.py` / `probe_world2.py`（無認証・並列・約 150 ホスト）で、米欧以外を含む推論 API を
`GET /v1/models`（相当）で実測した。判定は **200=公開カタログ / 401・403=存在（要キー） /
0・404・410=到達不能・廃止**。

### 8.1 実測サマリ

| 区分 | 件数 | 代表 |
|---|---|---|
| 公開カタログ 200 | 11 | ModelScope / SambaNova / Ollama Cloud / Typhoon / Sarvam / Novita / DeepInfra / Featherless / Chutes / OpenCode Zen / Atlas Cloud |
| 存在 401・403 | 約 30 | Z.ai / Zhipu(CN) / SiliconFlow / DashScope / Qianfan / Moonshot / MiniMax / Volcengine / Hunyuan / StepFun / Baichuan / SenseNova / Mistral / Cohere / Upstage / Sakana / CLOVA / Aleph Alpha / Yandex / GigaChat / GMI / Nebius / Hyperbolic / Together / Cerebras / Fireworks / xAI / Reka / Replicate / AnyAPI / nscale |
| 廃止・死 | 4+ | **Yi(01.AI)=410 `model_service_closed`** / OctoAI / Lepton / Jais（DNS 死） |

### 8.2 採用候補（無料枠が実在し、`PROVIDER_SPECS` にオプトイン追記できる）

| provider | 地域 | base_url | 無料枠（公称） | 無認証実測 | 判定 |
|---|---|---|---|---|---|
| **Z.ai（Zhipu GLM）** | 中国 | `https://api.z.ai/api/paas/v4` | `glm-4.5/4.7-flash` が**永久 $0** | 401 | **採用（高）** |
| **Zhipu 本土** | 中国 | `https://open.bigmodel.cn/api/paas/v4` | GLM-4-Flash 永久無料＋新規 2000 万 token | 401 | **採用（高・中国回線）** |
| **ModelScope** | 中国 | `https://api-inference.modelscope.cn/v1` | **2000 回/日**、OpenAI+Anthropic 両対応 | **200** | **採用（高）** |
| **Ollama Cloud** | 米 | `https://ollama.com/v1` | Free=月次 starter credit・1 並列・カード不要 | **200** | **採用（中）** |
| **Typhoon（SCB 10X）** | タイ | `https://api.opentyphoon.ai/v1` | 研究ショーケース**無料**・5 req/s・商用 OK | **200** | **採用（中）** |
| **SiliconFlow** | 中国 | `https://api.siliconflow.cn/v1` | 9B 以下**永久無料**＋新規 2000 万 token | 401 | 採用（中） |
| **DashScope（百炼）** | 中国 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 1 モデル 100 万 token/3 か月＋新規 1000 万 | 401 | 採用（中） |
| SambaNova | 米 | `https://api.sambanova.ai/v1` | 20 RPM / 20 RPD / 200k TPD・カード不要 | 200 | 採用（低・非常用） |
| Mistral La Plateforme | 仏 | `https://api.mistral.ai/v1` | Experiment ≈10 億 token/月・電話確認 | 401 | 採用（中） |
| Cohere | 加 | `https://api.cohere.ai/compatibility/v1` | 20 RPM / 1000 コール/月・**非商用** | 401 | 条件付（非商用） |

> **中国勢の位置づけ**: すべて OpenAI 互換 `/v1/chat/completions` で、`base_url` とキーを差し替えるだけで
> `PROVIDER_SPECS` に載る。ただし **SiliconFlow / DashScope / ModelScope は SMS＋実名（中国/香港/澳門/台湾の
> ID）確認が必要**な場合があり、日本在住者は「キー発行の可否」を先に確認すること（残課題 §7.2）。

### 8.3 棄却・保留

| 候補 | 地域 | 判定 | 理由 |
|---|---|---|---|
| **Yi（01.AI）** | 中国 | **棄却** | 実測 **410 `model_service_closed`**（サービス終了） |
| OctoAI / Lepton / Jais | 米/中東 | **棄却** | DNS 解決不能（廃止） |
| Together / Cerebras / Fireworks / xAI / Reka | 米 | 棄却 | 無料枠なし・課金必須（Cerebras は 403、$5 トライアルのみ） |
| Atlas Cloud | 中/米 | 棄却 | 無料枠が「Temporarily Unavailable」・最低 $25 入金 |
| Chutes | 米 | 棄却 | 有料化（$3/月〜） |
| DeepInfra | 米 | 棄却 | 永久無料枠なし（トライアルのみ） |
| Sarvam | 印 | 保留 | OpenAI 互換だが**₹100 トライアルのみ**（永久無料枠ではない） |
| Novita | 中/米 | 保留 | $0 モデルが回転＋$0.50 トライアル。固定 ID を保証できない |
| Featherless | 米 | 保留 | サブスク前提（$10/月）で「無料枠」が不明瞭 |
| OpenCode Zen | 米 | 保留 | 期間限定 promo の free モデルのみ（恒久性なし） |
| Upstage | 韓 | 保留 | Solar Pro の promo 以外は前払い（$100/月〜） |
| Sakana / CLOVA(NAVER) | 日/韓 | 保留 | 有料中心（実測 401） |
| Aleph Alpha | 独 | 棄却 | 有料 |
| Yandex / GigaChat | 露 | 保留 | 有料/トライアル中心（実測 401） |
| Moonshot / MiniMax / Volcengine / Hunyuan / StepFun / Baichuan / Qianfan / SenseNova | 中国 | 保留 | 実測 401。無料枠は回転/トライアルで、キー発行後に live カタログで要再確認 |

### 8.4 代用の可否（結論）

- **現行 7 プロバイダ（nous / openrouter / nvidia / huggingface / groq / cloudflare / gemini）を
  置き換える必要はない**。nous（キー不要のローカル proxy）が主系という設計は妥当。
- **「世界中から追加して代用する」ことは可能**。実効容量の優先順は
  **ModelScope ＞ Z.ai ＞ Mistral ＞ DashScope ＞ SiliconFlow ＞ Ollama Cloud ＞ Typhoon ＞ SambaNova ＞ Cohere**。
  中国勢は無料枠が広く fan-out に向くが、**実名/電話確認というキー取得の壁**がある点で欧米勢と性格が異なる。
- **「より良い API で代用」の実質的な意味**は、(a) 無料枠の総量を増やす（ModelScope / Mistral）、
  (b) 中国語・タイ語など**非英語の一次知識**に強いモデルを足す（Typhoon / Qwen / GLM）、
  (c) レイテンシの違う経路を冗長化する（Ollama Cloud）、の 3 点。単一プロバイダへの置換ではなく
  **多系統化**が正解。

---

## 9. 世界規模スキャン: 知識 API（プログラミング / 科学 / 非欧米圏）

すべて無認証で実プローブし、**200 で実データが返ったもの**を採用候補とした（既存ソースとの重複は除外）。

### 9.1 プログラミング分野（24 ソース、全 200）

| API | 用途 | 実測 |
|---|---|---|
| Stack Exchange API v2.3 | 実装の落とし穴・エラーメッセージ | 200 |
| OSV.dev | 既知脆弱性の確定判定 | 200 |
| deps.dev（v3 / v3alpha） | 依存関係・ライセンス・勧告・criticality | 200 |
| PyPI JSON / Maven Central / Go proxy | パッケージメタ | 200 |
| Packagist / RubyGems / NuGet / npm / crates | 各言語エコシステム | 200 |
| **MDN Web Docs API** | Web 標準の一次リファレンス | 200 |
| **RFC Editor（rfcNNNN.txt）** | IETF 標準文書の本文 | 200 |
| **IETF datatracker API** | RFC/ドラフトのメタ検索 | 200 |
| **Unicode UCD** | 文字・ブロック定義 | 200 |
| Codeberg / GitLab API | GitHub 以外のコードホスト | 200 |
| SPDX license list | ライセンス識別子 | 200 |
| **NVD CVE 2.0** / CIRCL（vulnerability-lookup） | 脆弱性データベース | 200 |
| Repology | ディストリ横断のパッケージ版 | 200 |
| OpenSSF Scorecard | リポジトリのセキュリティ評価 | 200 |
| HN Algolia | 技術コミュニティの議論 | 200 |

- **MDN / RFC Editor / IETF datatracker / Unicode** は「Web・プロトコル・文字コード」の**規格一次情報**で、
  コード（GitHub）と論文（arXiv）の中間にある**仕様**を埋める。grep.app は 429 で不安定（保留）。

### 9.2 科学分野（30+ ソース、全 200）

| 分野 | API |
|---|---|
| 生命・医学 | NCBI E-utilities（PubMed / Taxonomy）/ Europe PMC / **ClinicalTrials.gov v2** / **EBI BioStudies** |
| 化学 | PubChem PUG REST / **ChEMBL** / **OPSIN**（名称→構造） |
| タンパク質・構造 | UniProt / RCSB PDB / **PDBe** / **AlphaFold DB** / **InterPro** / **PRIDE** |
| ゲノム・経路 | **Ensembl** / **KEGG** / **Reactome** / **STRING** / **Gene Ontology** / **QuickGO** / **BioModels** / **EBI OLS**（オントロジー） |
| 素粒子物理 | INSPIRE-HEP |
| 数学 | **OEIS** / **zbMATH Open** / **LMFDB** |
| 天文・宇宙 | **SIMBAD TAP** / **MAST** / **JPL Horizons** / **NASA Exoplanet Archive** |
| 地球科学 | **GBIF** / **PBDB**（古生物） / **USGS**（地震） / **MPC**（小惑星） |
| プレプリント | bioRxiv / medRxiv |
| 医薬 | openFDA |
| 機械学習 | OpenML / Hugging Face Hub |

- **PubMed / PubChem / UniProt / RCSB / INSPIRE-HEP** は前回どおり最優先。今回 **Ensembl / KEGG / Reactome /
  STRING / Gene Ontology / QuickGO / InterPro / PRIDE / BioModels / EBI OLS** を足すと**生命科学の
  「配列→構造→経路→アノテーション」**が一気通貫になる。**zbMATH / LMFDB / OEIS** で数学、
  **SIMBAD / MAST / JPL Horizons / NASA Exoplanet** で天文が埋まる。

### 9.3 非欧米圏の一次情報（「世界中から」の中核）

| API | 地域 | 実測 | 備考 |
|---|---|---|---|
| **SciELO ArticleMeta** | 中南米・スペイン・ポルトガル・**南ア** | 200 | 1800+ 誌・90 万+ 記事。`/api/v1/article/identifiers/`・`/journal/` が無認証 |
| **CiNii Research OpenSearch** | 日本 | 200（JSON-LD） | 論文・図書・博士論文・研究データを横断。`format=json` で取得可 |
| **AJOL（OAI-PMH）** | アフリカ | 200 | アフリカの学術誌。OAI-PMH でメタデータ取得 |
| **NOPR（OAI-PMH）** | インド | 200 | NISCAIR の OA リポジトリ |
| Europeana | 欧州（文化） | 200 | `api2demo` キーで試用可。恒久利用は無料キー |
| Taiwan TCI / OpenLibrary / Internet Archive | 台湾・世界 | 200 | TCI は HTML、後者 2 つは JSON API |

- **SciELO / CiNii / AJOL / NOPR** は、英語圏の OpenAlex / Crossref が取りこぼす
  **非英語・途上国地域の査読論文**を補う。citation 統合（`_kb_merge_citations`）にそのまま乗る。
- **J-STAGE は公開 API なし**、**CNKI / AMiner / KISTI ScienceON は有料/要キー**、**Redalyc /
  KoreaScience / LA Referencia は DNS 死**、**ChinaXiv は 403（保守中）**、**Math-Net.Ru はタイムアウト**。

### 9.4 棄却・保留（第2ラウンド）

| API | 判定 | 理由 |
|---|---|---|
| Semantic Scholar | 保留 | 匿名は共有プールで実測 429（前回同様） |
| CORE | 保留 | 無認証は実測 429 |
| OpenCitations | 保留 | `/index/v1/metadata` は **410 Gone**（`api.opencitations.net` へ移転） |
| Met Museum | 棄却 | `/public/collection/v1/search` は **2026-10-01 に廃止** |
| Materials Project / NASA ADS | 保留 | 要キー（実測 401） |
| OQMD / Gaia TAP / COD | 保留 | タイムアウト／接続遮断（実測 0） |
| grep.app | 保留 | 実測 429（不安定） |
| DBpedia | 保留 | 実測 503 |

---

## 10. 統合方針の追補（実装順序の推奨）

§5 の方針に、世界規模スキャンの結果を踏まえた**実装順序**を追記する。すべて**オプトイン（既定 off）**で、
`DEFAULT_SOURCES` / `PROVIDER_ORDER` の既定は変えない。

1. **推論**: `PROVIDER_SPECS` に **ModelScope → Z.ai → Mistral → DashScope → SiliconFlow → Ollama Cloud →
   Typhoon → SambaNova → Cohere** の順で追記（`allowlist`＋`required_env_flags` パターン）。
   `scripts/probe_providers.py` / `warmup_models.py` で生存確認、`tests/test_provider_<name>.py` を追加。
2. **知識（プログラミング）**: §5.17 の 3 ソース（Stack Exchange / OSV / deps.dev）に加え、
   **MDN / RFC Editor / IETF datatracker / NVD / Repology / OpenSSF Scorecard** を同一 § で追加。
   gzip 応答（Stack Exchange）と POST（OSV）用小ヘルパを 1 つずつ用意する。
3. **知識（科学）**: §5.18 を「生命科学（Ensembl / KEGG / Reactome / STRING / GO / QuickGO / InterPro /
   PRIDE / BioModels / OLS）」「構造（PDBe / AlphaFold）」「数理・天文・地学（zbMATH / LMFDB / OEIS /
   SIMBAD / MAST / JPL Horizons / NASA Exoplanet / GBIF / PBDB / USGS / MPC）」に分割して追記。
4. **知識（非欧米圏）**: 新 §5.21 を立て、**SciELO / CiNii / AJOL / NOPR** を追加。CiNii は JSON-LD、
   AJOL / NOPR は OAI-PMH なので XML パーサ（標準 `xml.etree`）ヘルパを 1 つ足す。
5. **ゲート**: §5.3 のとおり `compileall` → `check_integrity` → `unittest` → `smoke_stdio` →
   `probe_knowledge_stdio` を回し、README.md / SPEC.md のソース一覧と `sources` の説明を同一変更で更新。

> **注意（第2ラウンドの教訓）**: 「カタログが 200」≠「無料枠で推論できる」。中国勢は**キー発行に
> 実名/SMS 確認**が必要な場合があり、日本在住者は先にキーを取れるか確認すること（§8.2 注記）。
> また `410`/DNS 死（Yi / OctoAI / Lepton / Jais）のように**プロバイダは短期間で消える**ため、
> `probe` による生存確認を定期実行に組み込むこと。
