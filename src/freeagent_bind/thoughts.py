"""§2.6/§2.7 思考台帳（永続・改訂・分岐・仮説）

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import json
import math
import os
import threading

from . import server as S  # noqa: E402

_THOUGHTS: dict[str, dict] = {}




_THOUGHTS_MTIME = 0.0


_THOUGHTS_LOCK = threading.RLock()


_THOUGHTS_CLOCK: dict[str, float] = {}


THOUGHTS_ENABLED = S._env_on("FREEAGENT_THOUGHTS", True)


THOUGHT_TTL_S = S._env_float("FREEAGENT_THOUGHT_TTL", 7200.0)


THOUGHT_MAX = S._env_int("FREEAGENT_THOUGHT_MAX", 32)


THOUGHT_MAX_STEPS = max(1, min(200, S._env_int("FREEAGENT_THOUGHT_MAX_STEPS", 24)))


_THOUGHT_CHARS = max(100, min(8000, S._env_int("FREEAGENT_THOUGHT_CHARS", 2000)))


def thoughts_path() -> str:
    return os.environ.get("FREEAGENT_THOUGHTS_PATH") or os.path.join(S.state_dir(), "thoughts.json")


def _thoughts_prune(data: dict, now: float) -> dict:
    alive = {k: v for k, v in data.items()
             if isinstance(v, dict) and S.as_float(v.get("updated_at"), 0.0) > now - THOUGHT_TTL_S}
    if len(alive) > S.THOUGHT_MAX:  # 新しい順に上限まで残す
        keep = sorted(alive, key=lambda k: S.as_float(alive[k].get("updated_at"), 0.0), reverse=True)
        alive = {k: alive[k] for k in keep[:S.THOUGHT_MAX]}
    return alive


def _thoughts_disk_chains(path: str, *, strict: bool = False) -> dict:
    """Read chains; strict writes must never replace an unreadable ledger."""
    if strict:
        try:
            with open(path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise OSError("思考台帳を読めません。上書きしません") from exc
        rows = data.get("chains") if isinstance(data, dict) else None
        if not isinstance(rows, dict) or any(
            not isinstance(row, dict)
            or not isinstance(row.get("updated_at"), (int, float))
            or isinstance(row.get("updated_at"), bool)
            or not math.isfinite(row["updated_at"])
            or ("verifier_models" in row and
                (not isinstance(row["verifier_models"], list) or
                 any(not isinstance(model, str) for model in row["verifier_models"])))
            or ("verifier_generation" in row and
                (type(row["verifier_generation"]) is not int or row["verifier_generation"] < 0))
            or ("steps" in row and (not isinstance(row["steps"], list)
                                   or any(not isinstance(step, dict) for step in row["steps"])))
            for row in rows.values()
        ):
            raise OSError("思考台帳の形式が不正です。上書きしません")
        return rows
    data = S._load_json(path, {})
    rows = data.get("chains") if isinstance(data, dict) else None
    return rows if isinstance(rows, dict) else {}


def _thoughts_merge_row(base: dict, mine: dict) -> dict:
    """2 つの台帳行を統合する（別プロセスが同じ台帳へ同時に書いたときの last-writer-wins 防止）。

    **集合もの（`steps`）は番号の和集合**（同じ番号は新しい方が優先）で、
    **状態もの（`plan` / `plan_retired` / `branch_meta` / `total_history`）は
    `updated_at` が新しい方の値をそのまま使う**（計画の改訂は「差し替え」なので
    和集合にすると廃止した項目が復活してしまう）。
    """
    base = base or {}
    mine = mine or {}
    newer, older = (mine, base)
    if S.as_float(base.get("updated_at"), 0.0) > S.as_float(mine.get("updated_at"), 0.0):
        newer, older = base, mine
    out = S._deep_copy(older)
    # ステップは番号の和集合（古い方 → 新しい方 で上書き）。
    steps: dict[int, dict] = {}
    for s in list(older.get("steps") or []) + list(newer.get("steps") or []):
        if isinstance(s, dict):
            steps[S.as_int(s.get("n"), 0, 0, 9999)] = s
    out["steps"] = [steps[k] for k in sorted(steps)]
    # 状態もの（計画・分岐・見積り履歴）は新しい方の値をそのまま使う。
    for key in ("plan", "plan_retired", "branch_meta", "total_history"):
        if key in newer:
            out[key] = newer.get(key)
    for key in ("card", "question"):
        if newer.get(key):
            out[key] = newer[key]
    vms = [m for m in (out.get("verifier_models") or []) if isinstance(m, str)]
    newer_vms = list(dict.fromkeys(m for m in (newer.get("verifier_models") or []) if isinstance(m, str)))
    # Prefer the newer snapshot's order; retain all of its names even if a
    # single call used more than the ordinary 12-name rotation window.
    newer_vm_set = set(newer_vms)
    vms = [m for m in vms if m not in newer_vm_set] + newer_vms
    out["verifier_models"] = vms[-max(12, len(newer_vms)):]
    out["created_at"] = newer.get("created_at") or older.get("created_at") or S.now_ts()
    out["updated_at"] = max(S.as_float(base.get("updated_at"), 0.0),
                             S.as_float(mine.get("updated_at"), 0.0))
    return out


def _thoughts_write_locked(path: str, chains: dict) -> bool:
    """ロックの中で台帳を書き出す（**呼び出し側がロックを持っている**前提）。"""
    global _THOUGHTS_MTIME
    payload = {"version": 1, "saved_at": S.now_ts(), "chains": chains}
    ok = S._atomic_write(path, S._dumps(payload))
    if ok:
        _THOUGHTS_MTIME = S._file_mtime(path) or 0.0
    return ok


def _ensure_thoughts_loaded() -> None:
    """Refresh from the locked disk ledger, including deletions and read failures."""
    global _THOUGHTS_MTIME
    if not S.THOUGHTS_ENABLED:
        return
    path = thoughts_path()
    clock_key = os.path.normcase(os.path.abspath(path))
    with _THOUGHTS_LOCK:
        try:
            os.stat(path)
        except FileNotFoundError:
            # The absent observation linearizes before any concurrent creation.
            # A writer still takes the lock and re-reads before modifying the file.
            S._THOUGHTS.clear()
            S._THOUGHTS_LOADED = True
            _THOUGHTS_MTIME = 0.0
            return
        with S._file_lock(path) as locked:
            if not locked:
                raise OSError("思考台帳のロックを取得できませんでした")
            disk = S._thoughts_disk_chains(path, strict=True)
            effective_now = max(S.now_ts(), S._THOUGHTS_CLOCK.get(clock_key, 0.0))
            rows = _thoughts_prune(disk, effective_now)
            S._THOUGHTS_CLOCK[clock_key] = effective_now
            S._THOUGHTS.clear()
            S._THOUGHTS.update(rows)
            S._THOUGHTS_LOADED = True
            _THOUGHTS_MTIME = S._file_mtime(path) or 0.0


def new_thought_id() -> str:
    return f"t{int(S.now_ts()) % 100000000:08d}{os.getpid() % 1000:03d}{os.urandom(6).hex()}"


def thought_get(sid: str) -> dict | None:
    if not sid:
        return None
    with _THOUGHTS_LOCK:
        _ensure_thoughts_loaded()
        row = S._THOUGHTS.get(sid)
        if not row:
            return None
        result = S._deep_copy(row)
        if S.THOUGHTS_ENABLED:
            result["verifier_models"] = result.get("verifier_models") or []
            result["verifier_generation"] = result.get("verifier_generation", 0)
        return result


def _thought_verifier_update(row: dict, models: list | None, snapshot: list | None,
                             additions: list | None = None,
                             snapshot_generation: int | None = None) -> tuple[list, int]:
    """Apply this call's additions to the locked history, preserving concurrent additions."""
    current = [m for m in (row.get("verifier_models") or []) if isinstance(m, str)]
    if snapshot is None:
        return current, 0
    before = set(snapshot)
    candidates = additions if additions is not None else [m for m in (models or []) if m not in before]
    added = list(dict.fromkeys(m for m in candidates if isinstance(m, str) and m))
    combined = list(dict.fromkeys([m for m in current if m not in added] + added))
    # Unknown generations are conservative; equality alone cannot detect ABA.
    if (snapshot_generation is None or snapshot_generation != row.get("verifier_generation", 0)
            or current != snapshot):
        return combined, 0
    keep = max(12, len(set(added)))
    return combined[-keep:], max(0, len(combined) - keep)


def thought_merge(sid: str, step: dict, *, question: str = "", verifier_models: list | None = None,
                  assign_number: bool = False, ops: dict | None = None, card: str = "",
                  verifier_snapshot: list | None = None, verifier_additions: list | None = None,
                  verifier_snapshot_generation: int | None = None, require_existing: bool = False) -> dict:
    """台帳へ 1 ステップを**1 つのロック内で**統合する（読み・採番・書きを分けない）。

    読みと書きを分けると、並列に呼ばれた 2 つの思考が同じ番号を採番して**片方が上書きで消える**
    （メインは 1 ターンで複数ツールを並行に呼ぶ前提で書く。規約 14）。`assign_number=True` のとき
    番号をロック内で採番する。`sid` が未知・期限切れなら**古い内容は復活させず**新しい ID を採番する。
    上限（`S.THOUGHT_MAX_STEPS`）に達していたら**黙って捨てず** `refused` を立てて返す。

    **プロセス間でも同じ**ようにする: ファイルロック（`S._file_lock`）を取り、最新のディスク内容を
    正本として読み、採番・更新・保存まで直列化する。期限切れや削除済み ID は新規扱いにし、
    容量や期限の制約で更新行を残せない場合は何も保存せずエラーを返す。
    """
    with _THOUGHTS_LOCK:
        path = thoughts_path()
        clock_key = os.path.normcase(os.path.abspath(path))
        if not S.THOUGHTS_ENABLED:
            # 台帳が無効なら**ロックファイルも作らない**（状態ディレクトリを汚さない）。
            target = sid if sid in S._THOUGHTS else new_thought_id()
            row = S._deep_copy(S._THOUGHTS.get(target) or {})
            steps = [s for s in (row.get("steps") or []) if isinstance(s, dict)]
            step = dict(step)
            if not S._thought_subgoal_still_matches(row, steps, step, ops or {}):
                return {"error": "計画が並行して変更され、subgoal の参照先が変わりました。記録していません。台帳を確認して再試行してください。"}
            if assign_number:
                step["n"] = max([S.as_int(s.get("n"), 0, 0, 9999) for s in steps] + [0]) + 1
            number = S.as_int(step.get("n"), 0, 0, 9999)
            replaced = any(S.as_int(s.get("n"), 0, 0, 9999) == number for s in steps)
            if not replaced and len(steps) >= S.THOUGHT_MAX_STEPS:
                return {"session_id": target, "step": None, "steps_recorded": len(steps), "refused": True}
            old = next((s for s in steps if S.as_int(s.get("n"), 0, 0, 9999) == number), None)
            steps = [s for s in steps if S.as_int(s.get("n"), 0, 0, 9999) != number] + [step]
            steps.sort(key=lambda s: S.as_int(s.get("n"), 0, 0, 9999))
            _thought_apply_ops(row, steps, step, old, ops or {})
            history, omitted = _thought_verifier_update(row, verifier_models, verifier_snapshot, verifier_additions,
                                                         verifier_snapshot_generation)
            row.update({"steps": steps,
                        "card": card or row.get("card") or "",
                        "question": question or row.get("question") or "",
                        "verifier_models": history,
                        "verifier_generation": row.get("verifier_generation", 0) + (verifier_snapshot is not None),
                        "created_at": row.get("created_at") or S.now_ts(),
                        "updated_at": S.now_ts()})
            S._THOUGHTS[target] = row
            return {"session_id": target, "step": step, "steps_recorded": len(steps),
                    "refused": False, "replaced": replaced, "verifier_omitted": omitted}
        with S._file_lock(path) as locked:
            if not locked:
                return {"error": "思考台帳のロックを取得できませんでした。記録していません。"}
            try:
                disk = S._thoughts_disk_chains(path, strict=True)
            except OSError as exc:
                return {"error": str(exc)}
            effective_now = max(S.now_ts(), S._THOUGHTS_CLOCK.get(clock_key, 0.0))
            disk_row = disk.get(sid) if sid else None
            known = sid if (disk_row is not None and
                            S.as_float(disk_row.get("updated_at"), 0.0) > effective_now - THOUGHT_TTL_S) else ""
            if require_existing and not known:
                return {"error": "読み取り後に思考台帳が削除または期限切れになりました。記録していません。"}
            base = disk[known] if known else {}
            target = known or new_thought_id()
            row = S._deep_copy(base)
            steps = [s for s in (row.get("steps") or []) if isinstance(s, dict)]
            step = dict(step)
            if not S._thought_subgoal_still_matches(row, steps, step, ops or {}):
                return {"error": "計画が並行して変更され、subgoal の参照先が変わりました。記録していません。台帳を確認して再試行してください。"}
            if assign_number:
                step["n"] = max([S.as_int(s.get("n"), 0, 0, 9999) for s in steps] + [0]) + 1
            number = S.as_int(step.get("n"), 0, 0, 9999)
            replaced = any(S.as_int(s.get("n"), 0, 0, 9999) == number for s in steps)
            if not replaced and len(steps) >= S.THOUGHT_MAX_STEPS:
                return {"session_id": target, "step": None, "steps_recorded": len(steps), "refused": True}
            old = next((s for s in steps if S.as_int(s.get("n"), 0, 0, 9999) == number), None)
            steps = [s for s in steps if S.as_int(s.get("n"), 0, 0, 9999) != number] + [step]
            steps.sort(key=lambda s: S.as_int(s.get("n"), 0, 0, 9999))
            # 他ステップ・台帳メタへの波及（改訂済みの印・仮説の状態・分岐の状態・計画）も**同じロック内**。
            _thought_apply_ops(row, steps, step, old, ops or {})   # §2.7
            history, omitted = _thought_verifier_update(row, verifier_models, verifier_snapshot, verifier_additions,
                                                         verifier_snapshot_generation)
            row.update({
                "steps": steps,
                # KANBAN のカード ID（任意）。渡された値が優先で、未指定なら既存を保つ（§6.9 の card 引数）。
                "card": card or row.get("card") or "",
                "question": question or row.get("question") or "",
                "verifier_models": history,
                "verifier_generation": row.get("verifier_generation", 0) + (verifier_snapshot is not None),
                "created_at": row.get("created_at") or effective_now,
                "updated_at": max(S.as_float(base.get("updated_at"), 0.0), effective_now),
            })
            disk[target] = row
            retained = _thoughts_prune(disk, effective_now)
            if target not in retained:
                return {"error": "思考台帳の容量または期限により記録できませんでした。"}
            if not _thoughts_write_locked(path, retained):
                return {"error": "思考台帳の保存に失敗しました。記録していません。"}
            S._THOUGHTS_CLOCK[clock_key] = effective_now
            S._THOUGHTS.clear()
            S._THOUGHTS.update(retained)
            S._THOUGHTS_LOADED = True
    return {"session_id": target, "step": step, "steps_recorded": len(steps),
            "refused": False, "replaced": replaced, "verifier_omitted": omitted}


def thought_find_by_card(card: str) -> str:
    """カード ID に紐づく台帳の `session_id` を返す（無ければ ""）。新しい順に 1 つだけ。

    KANBAN のワーカーが `card` に自分のカード ID（t_xxxx）を渡すと、思考がカードに紐づく。台帳は
    上限 `S.THOUGHT_MAX` 件しか持たないので線形走査で足りる。**複数のワーカーが同じカードを触っても
    最後に更新された台帳を返す**（カード 1 枚 = 1 本の思考の道筋、という運用を想定）。
    """
    card = S.as_str(card).strip()
    if not card:
        return ""
    with _THOUGHTS_LOCK:
        _ensure_thoughts_loaded()
        hits = [(S.as_float((row or {}).get("updated_at"), 0.0), sid)
                for sid, row in S._THOUGHTS.items()
                if isinstance(row, dict) and S.as_str(row.get("card")).strip() == card]
    return max(hits)[1] if hits else ""


def thought_drop(sid: str) -> None:
    if not sid:
        return
    with _THOUGHTS_LOCK:
        if not S.THOUGHTS_ENABLED:
            S._THOUGHTS.pop(sid, None)
            return
        path = thoughts_path()
        clock_key = os.path.normcase(os.path.abspath(path))
        with S._file_lock(path) as locked:
            if not locked:
                raise OSError("思考台帳のロックを取得できませんでした")
            disk = S._thoughts_disk_chains(path, strict=True)
            disk.pop(sid, None)
            effective_now = max(S.now_ts(), S._THOUGHTS_CLOCK.get(clock_key, 0.0))
            retained = _thoughts_prune(disk, effective_now)
            if not _thoughts_write_locked(path, retained):
                raise OSError("思考台帳の保存に失敗しました")
            S._THOUGHTS_CLOCK[clock_key] = effective_now
            S._THOUGHTS.clear()
            S._THOUGHTS.update(retained)
            S._THOUGHTS_LOADED = True


THOUGHT_KINDS = ("step", "hypothesis", "test", "conclusion")


HYPOTHESIS_STATES = ("open", "supported", "refuted", "inconclusive")


BRANCH_STATES = ("open", "adopted", "abandoned", "merged")


THOUGHT_PLAN_MAX = 12


def _plan_merge(old_items: list[dict], old_retired: list[dict],
                new_texts: list[str]) -> tuple[list[dict], list[dict]]:
    """計画の差し替え（新しい plan と、廃止された項目）。

    **同じ文面は同じ id を保つ**。id は「そのサブ目標の同一性」なので、差し替えで
    入れ替わると、それまで `subgoal` で参照していたステップが**別の項目を指すようになる**
    （実測: `plan=["A","B"]` で A 完了 → `plan=["B"]` で B が id 1 になり、ステップ 1 が
    A についてのものだったのに B に付け替わる）。

    * 同じ文面は元の id と `done_at`（達成済みの印）を引き継ぐ。
    * 新しい文面は**使っていない id**を割り当てる（別項目の id を再利用しない）。
    * 抜けた項目は**廃止**として残す（達成済みの記録と参照を黙って消さない）。
      廃止項目も `subgoal` の参照先として有効なので、参照が宙に浮かない。
    """
    by_text: dict[str, dict] = {}
    for item in list(old_items) + list(old_retired):
        text = S.as_str(item.get("text"))
        if text and text not in by_text:
            by_text[text] = item
    used_ids = {S.as_int(item.get("id"), 0, 0, 9999)
                for item in list(old_items) + list(old_retired)}
    next_id = max(used_ids or {0}) + 1
    new_plan: list[dict] = []
    for text in new_texts:
        prev = by_text.get(text)
        if prev is not None:
            new_plan.append({"id": S.as_int(prev.get("id"), 0, 0, 9999), "text": text,
                             "done_at": prev.get("done_at")})
        else:
            new_plan.append({"id": next_id, "text": text, "done_at": None})
            next_id += 1
    wanted = set(new_texts)
    new_retired = [dict(item) for item in old_retired
                   if S.as_str(item.get("text")) not in wanted]
    for item in old_items:
        if S.as_str(item.get("text")) not in wanted:
            new_retired.append(dict(item))
    return new_plan, new_retired


def _thought_apply_ops(row: dict, steps: list[dict], step: dict, old: dict | None, ops: dict) -> None:
    n = S.as_int(step.get("n"), 0, 0, 9999)
    by_n = {S.as_int(s.get("n"), 0, 0, 9999): s for s in steps}
    # 同じ番号の再送（置き換え）でも、他ステップが付けた印は失わない。
    if old:
        if old.get("superseded_by") and not step.get("superseded_by"):
            step["superseded_by"] = old["superseded_by"]
        if old.get("kind") == "hypothesis" and step.get("kind") == "hypothesis":
            for key in ("tested_by", "hypothesis_status", "status_at"):
                if old.get(key):
                    step[key] = old[key]
    rev = S.as_int(ops.get("revises"), 0, 0, 9999)
    if rev and rev != n and rev in by_n:
        by_n[rev]["superseded_by"] = n
    hyp = S.as_int(ops.get("tests_hypothesis"), 0, 0, 9999)
    if hyp and hyp in by_n and by_n[hyp].get("kind") == "hypothesis":
        target = by_n[hyp]
        target["tested_by"] = [t for t in (target.get("tested_by") or []) if t != n] + [n]
        if ops.get("hypothesis_status") in HYPOTHESIS_STATES:
            target["hypothesis_status"] = ops["hypothesis_status"]
            target["status_at"] = n
    meta = row.get("branch_meta") if isinstance(row.get("branch_meta"), dict) else {}
    bid = step.get("branch_id")
    if bid and bid not in meta:
        meta[bid] = {"from": step.get("branch_from_thought"), "status": "open",
                     "opened_at": n, "resolved_at": None}
    res = ops.get("resolve_branch")
    if res and res in meta and ops.get("branch_status") in BRANCH_STATES:
        meta[res]["status"] = ops["branch_status"]
        meta[res]["resolved_at"] = None if ops["branch_status"] == "open" else n
    row["branch_meta"] = meta
    plan = [p for p in (row.get("plan") or []) if isinstance(p, dict)]
    retired = [p for p in (row.get("plan_retired") or []) if isinstance(p, dict)]
    if ops.get("plan"):
        # 同じ文面は id と達成済みの印を引き継ぐ。新しい文面には**未使用の id**を割り当てる
        # （id を再利用すると、既存の subgoal 参照が別の項目を指すようになる）。
        plan, retired = _plan_merge(plan, retired, list(ops["plan"][:THOUGHT_PLAN_MAX]))
        # ここに来る前に `_think_args` が上限超過を fail で止めるので、この切り詰めは
        # **防御用**（呼び出し経路が増えても台帳が壊れないように）。規約 63 の対象は
        # 「実行数を減らして黙って続行する」箇所で、ここは契約違反として拒否する側。
        row["plan_revised_at"] = n
    # `subgoal` は**解決済みの plan 項目の id**（§6.10 の `_think_structure` が解決する）。
    # id は差し替えをまたいで 12 を超えることがあるので上限を広く取る（THOUGHT_PLAN_MAX は位置数）。
    sub = S.as_int(step.get("subgoal"), 0, 0, 9999)
    if sub and ops.get("subgoal_done"):
        for item in plan:
            if S.as_int(item.get("id"), 0, 0, 9999) == sub:
                item["done_at"] = n
    row["plan"] = plan
    row["plan_retired"] = retired
    total = S.as_int(ops.get("total"), 0, 0, 999)
    history = [h for h in (row.get("total_history") or []) if isinstance(h, dict)]
    if total and (not history or history[-1].get("total") != total):
        history.append({"at": n, "total": total, "auto": bool(ops.get("total_auto"))})
    row["total_history"] = history[-24:]


EXPORTS = {
    "_THOUGHTS": _THOUGHTS,
    "_THOUGHTS_MTIME": _THOUGHTS_MTIME,
    "_THOUGHTS_LOCK": _THOUGHTS_LOCK,
    "_THOUGHTS_CLOCK": _THOUGHTS_CLOCK,
    "THOUGHTS_ENABLED": THOUGHTS_ENABLED,
    "THOUGHT_TTL_S": THOUGHT_TTL_S,
    "THOUGHT_MAX": THOUGHT_MAX,
    "THOUGHT_MAX_STEPS": THOUGHT_MAX_STEPS,
    "_THOUGHT_CHARS": _THOUGHT_CHARS,
    "thoughts_path": thoughts_path,
    "_thoughts_prune": _thoughts_prune,
    "_thoughts_disk_chains": _thoughts_disk_chains,
    "_thoughts_merge_row": _thoughts_merge_row,
    "_thoughts_write_locked": _thoughts_write_locked,
    "_ensure_thoughts_loaded": _ensure_thoughts_loaded,
    "new_thought_id": new_thought_id,
    "thought_get": thought_get,
    "_thought_verifier_update": _thought_verifier_update,
    "thought_merge": thought_merge,
    "thought_find_by_card": thought_find_by_card,
    "thought_drop": thought_drop,
    "THOUGHT_KINDS": THOUGHT_KINDS,
    "HYPOTHESIS_STATES": HYPOTHESIS_STATES,
    "BRANCH_STATES": BRANCH_STATES,
    "THOUGHT_PLAN_MAX": THOUGHT_PLAN_MAX,
    "_plan_merge": _plan_merge,
    "_thought_apply_ops": _thought_apply_ops,
}
