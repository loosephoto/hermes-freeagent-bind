#!/usr/bin/env python3
"""Compare paired freeagent_agent independent/shared_evidence runs (offline only)."""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _load_experiment(line: str, line_no: int) -> dict:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"line {line_no}: invalid JSON: {exc.msg}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"line {line_no}: expected a JSON object")
    data = record.get("structuredContent")
    if not isinstance(data, dict):
        data = record
    experiment = data.get("experiment")
    if not isinstance(experiment, dict):
        raise ValueError(f"line {line_no}: missing experiment metadata")
    task_id = experiment.get("task_id")
    mode = experiment.get("communication")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError(f"line {line_no}: missing experiment.task_id")
    task_fingerprint = experiment.get("task_fingerprint")
    if not isinstance(task_fingerprint, str) or not task_fingerprint:
        raise ValueError(f"line {line_no}: missing experiment.task_fingerprint")
    if mode not in {"independent", "shared_evidence"}:
        raise ValueError(f"line {line_no}: invalid communication mode")
    evaluation = experiment.get("objective_evaluation")
    metrics = experiment.get("metrics")
    criteria = experiment.get("criteria")
    if not isinstance(evaluation, dict) or not isinstance(metrics, dict) or not isinstance(criteria, dict):
        raise ValueError(f"line {line_no}: incomplete objective evaluation or metrics")
    score = evaluation.get("best_agent_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError(f"line {line_no}: invalid best_agent_score")
    coverage = evaluation.get("team_coverage_score", score)
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not math.isfinite(coverage) or not 0 <= coverage <= 1:
        raise ValueError(f"line {line_no}: invalid team_coverage_score")
    passed = evaluation.get("pass_any")
    if not isinstance(passed, bool):
        raise ValueError(f"line {line_no}: invalid pass_any")
    unexpected = evaluation.get("unexpected_citations") or []
    if not isinstance(unexpected, list):
        raise ValueError(f"line {line_no}: invalid unexpected_citations")
    return {"task_id": task_id, "mode": mode, "experiment": experiment,
            "score": float(score), "passed": passed,
            "coverage_score": float(coverage),
            "coverage_pass": bool(evaluation.get("team_coverage_pass")),
            "metrics": metrics, "unexpected_citations": unexpected}


def _mean(rows: list[dict], field: str):
    values = []
    for row in rows:
        value = row["metrics"].get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            values.append(float(value))
    return round(sum(values) / len(values), 4) if values else None


def compare_records(records: list[dict]) -> dict:
    grouped: dict[str, dict[str, dict]] = defaultdict(dict)
    for record in records:
        task_id, mode = record["task_id"], record["mode"]
        if mode in grouped[task_id]:
            raise ValueError(f"duplicate {mode} run for task_id={task_id!r}")
        grouped[task_id][mode] = record

    if not grouped:
        raise ValueError("no experiment records")
    for task_id, pair in grouped.items():
        if set(pair) != {"independent", "shared_evidence"}:
            raise ValueError(f"unpaired task_id={task_id!r}: provide independent and shared_evidence runs")
        baseline = pair["independent"]["experiment"]
        treatment = pair["shared_evidence"]["experiment"]
        for key in (
            "task_fingerprint", "models", "served_by_models", "served_by_by_agent",
            "max_steps", "max_tokens_per_step", "criteria"
        ):
            if baseline.get(key) != treatment.get(key):
                raise ValueError(f"{key} mismatch for paired task_id={task_id!r}")

    pairs = [grouped[key] for key in sorted(grouped)]
    baseline_rows = [pair["independent"] for pair in pairs]
    treatment_rows = [pair["shared_evidence"] for pair in pairs]
    per_task = []
    for pair in pairs:
        independent = pair["independent"]
        shared = pair["shared_evidence"]
        im, sm = independent["metrics"], shared["metrics"]
        per_task.append({
            "task_id": independent["task_id"],
            "best_agent_score": {"independent": independent["score"],
                                 "shared_evidence": shared["score"],
                                 "delta": round(shared["score"] - independent["score"], 4)},
            "pass_any": {"independent": independent["passed"], "shared_evidence": shared["passed"]},
            "team_coverage_score": {"independent": independent["coverage_score"],
                                    "shared_evidence": shared["coverage_score"]},
            "total_tokens": {"independent": im.get("total_tokens"),
                             "shared_evidence": sm.get("total_tokens"),
                             "delta": (sm["total_tokens"] - im["total_tokens"]
                                       if isinstance(sm.get("total_tokens"), (int, float))
                                       and isinstance(im.get("total_tokens"), (int, float)) else None)},
            "wall_time_s": {"independent": im.get("wall_time_s"),
                            "shared_evidence": sm.get("wall_time_s"),
                            "delta": (round(sm["wall_time_s"] - im["wall_time_s"], 4)
                                      if isinstance(sm.get("wall_time_s"), (int, float))
                                      and isinstance(im.get("wall_time_s"), (int, float)) else None)},
            "peer_evidence_injections": sm.get("peer_evidence_injections", 0),
            "unexpected_citations": len(shared["unexpected_citations"]),
        })

    def pass_rate(rows: list[dict]) -> float:
        return round(sum(row["passed"] for row in rows) / len(rows), 4)

    independent_score = round(sum(row["score"] for row in baseline_rows) / len(baseline_rows), 4)
    shared_score = round(sum(row["score"] for row in treatment_rows) / len(treatment_rows), 4)
    independent_coverage = round(sum(row["coverage_score"] for row in baseline_rows) / len(baseline_rows), 4)
    shared_coverage = round(sum(row["coverage_score"] for row in treatment_rows) / len(treatment_rows), 4)
    independent_pass = pass_rate(baseline_rows)
    shared_pass = pass_rate(treatment_rows)
    shared_unexpected = sum(len(row["unexpected_citations"]) for row in treatment_rows)
    independent_tokens = _mean(baseline_rows, "total_tokens")
    shared_tokens = _mean(treatment_rows, "total_tokens")
    independent_time = _mean(baseline_rows, "wall_time_s")
    shared_time = _mean(treatment_rows, "wall_time_s")
    return {
        "paired_tasks": len(pairs),
        "independent": {"mean_best_agent_score": independent_score, "pass_rate": independent_pass,
                        "mean_team_coverage_score": independent_coverage,
                        "mean_total_tokens": independent_tokens, "mean_wall_time_s": independent_time,
                        "mean_unique_citations": _mean(baseline_rows, "unique_citations"),
                        "mean_distinct_sources": _mean(baseline_rows, "distinct_sources")},
        "shared_evidence": {"mean_best_agent_score": shared_score, "pass_rate": shared_pass,
                             "mean_team_coverage_score": shared_coverage,
                             "mean_total_tokens": shared_tokens, "mean_wall_time_s": shared_time,
                             "mean_unique_citations": _mean(treatment_rows, "unique_citations"),
                             "mean_distinct_sources": _mean(treatment_rows, "distinct_sources"),
                             "mean_peer_evidence_injections": _mean(treatment_rows, "peer_evidence_injections"),
                             "unexpected_citation_count": shared_unexpected},
        "delta_shared_minus_independent": {
            "best_agent_score": round(shared_score - independent_score, 4),
            "team_coverage_score": round(shared_coverage - independent_coverage, 4),
            "pass_rate": round(shared_pass - independent_pass, 4),
            "total_tokens": round(shared_tokens - independent_tokens, 4)
                             if shared_tokens is not None and independent_tokens is not None else None,
            "wall_time_s": round(shared_time - independent_time, 4)
                          if shared_time is not None and independent_time is not None else None,
        },
        "per_task": per_task,
        "interpretation": ("not an automatic promotion: inspect repeated tasks, quality, tokens, latency, "
                           "unexpected citations, and whether the intermediate evidence was actually useful"),
    }


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] in {"-h", "--help"}:
        print("Usage: python scripts/compare_agent_experiments.py RUNS.jsonl")
        print("Each line is one freeagent_agent structuredContent result; no inference or network is used.")
        return 0 if args in ([], ["-h"], ["--help"]) else 2
    path = Path(args[0])
    try:
        records = [_load_experiment(line, i) for i, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1) if line.strip()]
        report = compare_records(records)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
