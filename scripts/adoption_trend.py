"""自発利用率の推移を 1 行だけ残す（定期実行用・**変わったときだけ喋る**）。

`scripts/measure_adoption.py --json` を呼び、`<state>/adoption.jsonl` に 1 サンプル追記する。
前回と同じ率なら**何も出力しない**（毎回喋ると注意を食う＝この repo の方針に反する）。

使い方:
    python scripts/adoption_trend.py            # 30 セッション窓で 1 サンプル
    python scripts/adoption_trend.py --sessions 50

出力（stdout・cron はこれをそのまま配る）:
    - 初回: 「基準値を記録: 採用率 10.0% / 必要場面 42.9%」
    - 変化: 「変化: 採用率 10.0% → 14.3% / 必要場面 42.9% → 50.0%（出典の見逃し 6 → 5）」
    - 変化なし: 何も出さない
    - 失敗: エラー行を出して exit 1（黙って死なせない）
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def state_dir() -> Path:
    env = os.environ.get("FREEAGENT_STATE_DIR", "").strip()
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "hermes-freeagent-bind"


def sample(sessions: int) -> dict:
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "measure_adoption.py"),
                          "--sessions", str(sessions), "--json"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=180)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "measure_adoption failed").strip()[:300])
    data = json.loads(out.stdout)
    needed = data.get("needed_scenes") or {}
    sources = next((c for c in (needed.get("cues") or []) if c.get("id") == "sources"), {})
    return {
        "sessions": data.get("sessions"),
        "adoption_rate": data.get("adoption_rate"),
        "needed_sessions": needed.get("needed_sessions"),
        "needed_rate": needed.get("needed_rate"),
        "sources_used": sources.get("used"),
        "sources_missed": sources.get("missed"),
    }


def fmt_pct(value) -> str:
    return f"{round(float(value) * 100, 1)}%" if isinstance(value, (int, float)) else "—"


def main() -> int:
    sessions = 30
    args = sys.argv[1:]
    if "--sessions" in args:
        try:
            sessions = int(args[args.index("--sessions") + 1])
        except (IndexError, ValueError):
            print("--sessions には整数を指定してください")
            return 1

    try:
        current = sample(sessions)
    except Exception as exc:  # noqa: BLE001 - cron に理由を出して止める
        print(f"⚠️ adoption の測定に失敗しました: {type(exc).__name__}: {exc}")
        return 1

    path = state_dir() / "adoption.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    if path.exists():
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                previous = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**current, "at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S")},
                            ensure_ascii=False) + "\n")

    if previous is None:
        print(f"基準値を記録: 採用率 {fmt_pct(current['adoption_rate'])} / "
              f"必要場面 {fmt_pct(current['needed_rate'])}"
              f"（出典の見逃し {current['sources_missed']}）")
        return 0

    moved = (previous.get("adoption_rate") != current["adoption_rate"]
             or previous.get("needed_rate") != current["needed_rate"]
             or previous.get("needed_sessions") != current["needed_sessions"])
    if not moved:
        return 0
    print(f"変化: 採用率 {fmt_pct(previous.get('adoption_rate'))} → {fmt_pct(current['adoption_rate'])}"
          f" / 必要場面 {fmt_pct(previous.get('needed_rate'))} → {fmt_pct(current['needed_rate'])}"
          f"（出典の見逃し {previous.get('sources_missed')} → {current['sources_missed']}、"
          f"窓 {current['sessions']} セッション）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
