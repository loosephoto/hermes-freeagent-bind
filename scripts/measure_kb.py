#!/usr/bin/env python3
"""measure_kb.py — 知識バックエンドの応答時間を**時間帯別に**測る（server.py §5.8 の締め切りの根拠）。

「時間によっては遅い」ソースを特定するための計測。1 回の実行で全ソースを 1 巡（本番と同じく
ソースごとに並列）し、1 ソース 1 行を JSONL に追記する。**本文は記録しない**（所要秒・成否・件数だけ）。
キャッシュとホスト遮断の記憶は巡回ごとに消す（キャッシュ命中を計測に混ぜない）。

使い方（リポジトリ直下で）:

    python scripts/measure_kb.py                  # 1 巡測って追記
    python scripts/measure_kb.py --rounds 6 --interval 600   # 10 分おきに 6 巡（1 時間）
    python scripts/measure_kb.py --report         # 時間帯（日本時間の時）× ソースで集計
    python scripts/measure_kb.py --sources datacite openaire
    python scripts/measure_kb.py --sources datacite --datacite-kind dataset
    python scripts/measure_kb.py --schedule 24    # Windows のタスクで 1 時間おきに 24 回（終われば自動で削除）
    python scripts/measure_kb.py --unschedule     # タスクを消す

記録先: `FREEAGENT_KB_LATENCY_PATH`（既定 `FREEAGENT_STATE_DIR/kb_latency.jsonl`・最大 20,000 行）。
Hermes の外で動くので、Hermes の設定の `env:`（`FREEAGENT_MAILTO` など）は**反映されない**。
同じ条件で測りたいときは、同じ変数を環境に入れてから実行する（`--report` の `mailto` 列で区別できる）。

終了コード: 0=正常 / 1=引数・スケジュール登録の失敗。計測でソースが失敗しても 0（失敗も計測値）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import subprocess
import sys
import threading
import time

# pythonw（タスクスケジューラ）では stdout/stderr が None。日本語 Windows では cp932 で落ちる（実測）。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from freeagent_bind import server as S  # noqa: E402

TASK_NAME = "freeagent-bind-kb-latency"
# schtasks はコンソールのコードページ（日本語 Windows は cp932）で出力する。UTF-8 で読むと化ける（実測）。
_CONSOLE_ENC = "oem" if os.name == "nt" else "utf-8"
MAX_LINES = 20000
JST = datetime.timezone(datetime.timedelta(hours=9))
# 巡回ごとに変える（提供元側のキャッシュに当たり続けないため）。日本語・英語を混ぜる。
QUERIES = [
    ("large language model hallucination", "大規模言語モデル"),
    ("graphene thermal conductivity", "グラフェン"),
    ("earthquake early warning", "緊急地震速報"),
    ("transformer attention mechanism", "機械翻訳"),
    ("CRISPR gene editing", "ゲノム編集"),
    ("quantum error correction", "量子コンピュータ"),
]
_ENV_ERRORS = ("URLError", "TimeoutError", "timed out", "getaddrinfo", "WinError 10061",
               "WinError 10060", "Connection refused", "Network is unreachable")


def log_path() -> str:
    return os.environ.get("FREEAGENT_KB_LATENCY_PATH") or os.path.join(S.state_dir(), "kb_latency.jsonl")


def _say(text: str) -> None:
    if sys.stdout is not None:
        print(text)


# ソースごとに**分野の合う問い**を回す。論文キーワードだとパッケージ検索や生物種検索は空振りし、
# 空振りと障害を混同する（規約 27 の計測規律）。
SOURCE_QUERIES = {
    "npm": ["json schema validator", "http client", "websocket server",
            "markdown parser", "logging library", "unit testing"],
    "crates": ["json schema validator", "http client", "websocket server",
               "markdown parser", "logging library", "unit testing"],
    "librariesio": ["json schema validator", "http client", "websocket server",
                    "markdown parser", "logging library", "unit testing"],
    "osv": ["jinja2", "requests", "lodash", "serde", "express", "axios"],
    "ietf": ["DNSSEC", "QUIC", "CoAP", "TLS", "HTTP", "DNS"],
    "hn": ["rust ownership", "python asyncio", "database index", "docker networking",
           "typescript generics", "postgres performance"],
    "swh": ["kubernetes", "linux", "vscode", "tensorflow", "redis", "react"],
    "inspirehep": ["higgs boson", "dark matter", "neutrino", "supersymmetry", "black hole", "QCD"],
    "oeis": ["Fibonacci", "prime", "Catalan", "factorial", "partition", "1,2,3,5,7,11"],
    "hfhub": ["llama", "bert", "whisper", "diffusion", "mistral", "embedding"],
}

def measure_round(index: int | None = None, sources: list[str] | None = None,
                  datacite_kind: str = "all") -> list[dict]:
    """全ソースを並列に 1 回ずつ引き、1 ソース 1 行の記録を返す（書き込みはしない）。"""
    with S._KB_CACHE_LOCK:
        S._KB_CACHE.clear()
    with S._KB_BLOCK_LOCK:
        S._KB_BLOCKED.clear()
    picked = list(dict.fromkeys(s for s in sources if s in S.KB_BACKENDS)) if sources is not None else list(S.KB_BACKENDS)
    if "cinii" in picked and not getattr(S, "CINII_APPID", ""):
        # appid 未設定では必ずエラーになる。障害として記録せず対象から外す（規約 21 と同じ考え方）
        picked = [s for s in picked if s != "cinii"]
        _say("skip: cinii は FREEAGENT_CINII_APPID 未設定のため計測しません")
    if "librariesio" in picked and not getattr(S, "LIBRARIESIO_KEY", ""):
        # キー未設定では必ずエラーになる。障害として記録せず対象から外す
        picked = [s for s in picked if s != "librariesio"]
        _say("skip: librariesio は FREEAGENT_LIBRARIESIO_KEY 未設定のため計測しません")
    now = datetime.datetime.now(JST)
    en, ja = QUERIES[(now.hour if index is None else index) % len(QUERIES)]
    rows: dict[str, dict] = {}

    def one(src: str) -> None:
        # 日本語の問いは日本語版を持つソースだけ（Wikipedia / Wikidata）。論文系は英語で引く
        query = ja if src in ("wikipedia", "wikidata", "cinii") else en
        slot = now.hour if index is None else index
        if src == "ror":
            names = ["CERN", "University of Tokyo", "Massachusetts Institute of Technology",
                     "University of Oxford", "CNRS", "Kyoto University"]
            query = names[slot % len(names)]
        if src in SOURCE_QUERIES:
            terms = SOURCE_QUERIES[src]
            query = terms[slot % len(terms)]
        start = time.monotonic()
        try:
            res = S.KB_BACKENDS[src](query, 3, {"lang": "ja", "kind": "repo", "datacite_kind": datacite_kind})
        except Exception as exc:  # noqa: BLE001
            res = {"error": f"{type(exc).__name__}: {exc}"}
        elapsed = round(time.monotonic() - start, 3)
        err = str((res or {}).get("error") or "") if isinstance(res, dict) else "不正な結果"
        rows[src] = {"ts": now.isoformat(timespec="seconds"), "hour": now.hour, "source": src,
                     "elapsed_s": elapsed, "ok": not err,
                     "items": len((res or {}).get("items") or []) if isinstance(res, dict) else 0,
                     "error": err[:160], "mailto": bool(S.KB_MAILTO),
                     "openalex_key": bool(getattr(S, "OPENALEX_API_KEY", "")),
                     "cinii_appid": bool(getattr(S, "CINII_APPID", "")),
                     "datacite_kind": datacite_kind if src == "datacite" else "",
                     "summaries": sum(bool(c.get("summary")) for c in (res.get("citations") or [])
                                      if isinstance(c, dict)) if isinstance(res, dict) else 0}

    threads = [threading.Thread(target=one, args=(src,), daemon=True) for src in picked]
    for t in threads:
        t.start()
    for t in threads:
        t.join(S.KB_TIMEOUT * 4)   # Wikidata は最大 3 回直列＋余裕
    out = [rows[src] for src in picked if src in rows]
    # 全滅かつ接続系の失敗なら「こちらのネットワーク障害」。ソースの成績と混ぜない（規約 21 の考え方）
    env_down = bool(out) and all((not r["ok"]) and any(k in r["error"] for k in _ENV_ERRORS) for r in out)
    for r in out:
        r["env_failure"] = env_down
    return out


def append(rows: list[dict]) -> None:
    path = log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    if len(lines) > MAX_LINES:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(lines[-MAX_LINES:])
        os.replace(tmp, path)


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))]


def report() -> int:
    path = log_path()
    if not os.path.exists(path):
        _say(f"記録がありません: {path}")
        return 0
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    env = sum(1 for r in rows if r.get("env_failure"))
    rows = [r for r in rows if not r.get("env_failure")]
    _say(f"記録 {len(rows)} 行（こちらのネットワーク障害として除外 {env} 行） / {path}")
    if not rows:
        return 0
    sources = list(dict.fromkeys(r["source"] for r in rows))

    def line(label: str, sel: list[dict]) -> str:
        ok = [r["elapsed_s"] for r in sel if r.get("ok")]
        fail = len(sel) - len(ok)
        if not ok:
            return f"{label:<10}  n={len(sel):3}  成功 0  失敗 {fail}"
        slow = sum(1 for v in ok if v > S.KB_DEADLINE)
        return (f"{label:<10}  n={len(sel):3}  p50 {statistics.median(ok):5.2f}s  p95 {_pct(ok, 0.95):5.2f}s  "
                f"最大 {max(ok):5.2f}s  失敗 {fail}  締め切り({S.KB_DEADLINE:g}s)超え {slow}")

    _say("\n■ ソース別（全時間帯）")
    for src in sources:
        _say(line(src, [r for r in rows if r["source"] == src]))
    _say("\n■ 時間帯（日本時間の時）× ソースの p95（成功のみ・秒）と失敗数")
    hours = sorted({r["hour"] for r in rows})
    _say("  時  " + "".join(f"{s[:9]:>11}" for s in sources))
    for h in hours:
        cells = []
        for src in sources:
            sel = [r for r in rows if r["hour"] == h and r["source"] == src]
            ok = [r["elapsed_s"] for r in sel if r.get("ok")]
            fail = len(sel) - len(ok)
            cell = (f"{_pct(ok, 0.95):.1f}" if ok else "-") + (f"/✗{fail}" if fail else "")
            cells.append(f"{cell:>11}")
        _say(f"  {h:02d}  " + "".join(cells))
    return 0


def _pythonw() -> str:
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe   # pythonw ならコンソール窓が毎時開かない


def schedule(times: int) -> int:
    if os.name != "nt":
        _say("--schedule は Windows のタスクスケジューラ専用です。cron で `python scripts/measure_kb.py` を"
             "1 時間おきに実行してください。")
        return 1
    # 終了日時（/ED /ET）は HOURLY との組み合わせで意味が曖昧なので使わない。残り回数を自分で数え、
    # 尽きたらタスクを自分で消す（_scheduled_tick）。
    start = datetime.datetime.now() + datetime.timedelta(minutes=1)
    times = max(1, times)
    action = f'"{_pythonw()}" "{os.path.abspath(__file__)}" --scheduled'
    cmd = ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "HOURLY", "/MO", "1",
           "/ST", start.strftime("%H:%M"), "/TR", action]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding=_CONSOLE_ENC, errors="replace")
    _say((proc.stdout or proc.stderr).strip())
    if proc.returncode == 0:
        _write_remaining(times)
        _say(f"登録しました: {TASK_NAME}（{start:%m/%d %H:%M} から 1 時間おきに {times} 回。終われば自動で削除）\n"
             f"記録先: {log_path()}\n集計: python scripts/measure_kb.py --report / 途中で止める: --unschedule")
    return 0 if proc.returncode == 0 else 1


def _remaining_path() -> str:
    return log_path() + ".schedule.json"


def _write_remaining(n: int) -> None:
    path = _remaining_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"remaining": n}, fh)
    os.replace(tmp, path)


def _scheduled_tick() -> bool:
    """定時実行 1 回分の残り回数を減らす。尽きていれば計測せず False（タスクも消す）。"""
    try:
        with open(_remaining_path(), encoding="utf-8") as fh:
            remaining = int(json.load(fh).get("remaining", 0))
    except (OSError, ValueError, TypeError):
        remaining = 0
    if remaining <= 0:
        unschedule()
        return False
    _write_remaining(remaining - 1)
    if remaining - 1 <= 0:
        unschedule()   # 最後の 1 回。計測はこのあと行う
    return True


def unschedule() -> int:
    proc = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                          capture_output=True, text=True, encoding=_CONSOLE_ENC, errors="replace")
    _say((proc.stdout or proc.stderr).strip())
    try:
        os.remove(_remaining_path())
    except OSError:
        pass
    return 0 if proc.returncode == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--interval", type=float, default=600.0)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--sources", nargs="+", choices=S.SOURCES, help="計測対象（既定は対応22ソース）")
    ap.add_argument("--datacite-kind", choices=("all", "arxiv", "dataset"), default="all")
    ap.add_argument("--schedule", type=int, metavar="HOURS")
    ap.add_argument("--unschedule", action="store_true")
    ap.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)  # タスクからの起動
    args = ap.parse_args(argv)
    if args.scheduled and not _scheduled_tick():
        return 0
    if args.report:
        return report()
    if args.schedule is not None:
        return schedule(args.schedule)
    if args.unschedule:
        return unschedule()
    for i in range(max(1, args.rounds)):
        if i:
            time.sleep(max(1.0, args.interval))
        rows = measure_round(sources=args.sources, datacite_kind=args.datacite_kind)
        append(rows)
        _say(" / ".join(f"{r['source']} {r['elapsed_s']:.2f}s{'' if r['ok'] else '✗'}" for r in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
