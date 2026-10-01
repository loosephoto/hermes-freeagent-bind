"""新規知識APIを実stdio経路で検証する（ネットワークあり・推論なし）。

python scripts/probe_knowledge_stdio.py
python scripts/probe_knowledge_stdio.py --sources datacite --datacite-kind dataset --query graphene
python scripts/probe_knowledge_stdio.py --sources zenodo ror --query CERN

各指定ソースの有効応答・出典・両チャネルを検査。失敗はexit 1（成功を捏造しない）。
OpenAIREは匿名60/h。同一IP上の他プロセスも含め、連続実行には60秒以上の間隔を空ける。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

for stream in (sys.stdout, sys.stderr):
    if stream is not None and hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from freeagent_bind import server as S


def validate_lookup(data: dict, sources: list[str], fallback: bool) -> list[dict]:
    """キーの有無だけでなく引用の値と代替取得の許可/取得元を検査する。"""
    requested = list(dict.fromkeys(sources))
    if not isinstance(data, dict) or data.get("sources") != requested:
        raise ValueError("requested sources mismatch")
    results = data.get("results")
    if not isinstance(results, dict):
        raise ValueError("results must be an object")
    served = set()
    for src in requested:
        row = results.get(src)
        if not isinstance(row, dict) or row.get("error") or not isinstance(row.get("items"), list) or not row["items"]:
            raise ValueError(f"{src}: invalid/empty/error result")
        acquisition = row.get("source")
        switch = row.get("fallback")
        if switch:
            if not (fallback is True and src == "arxiv" and isinstance(switch, dict)
                    and switch.get("requested_source") == "arxiv" and switch.get("served_by") == "datacite"
                    and acquisition == "datacite" and isinstance(switch.get("primary_error"), str)):
                raise ValueError("fallback provenance or permission mismatch")
        elif acquisition != src:
            raise ValueError("acquisition source mismatch")
        served.add(acquisition)
    cites = data.get("citations")
    if not isinstance(cites, list) or not cites or data.get("citation_count") != len(cites):
        raise ValueError("citation count mismatch or no citations")
    for cite in cites:
        if not isinstance(cite, dict) or not {"source", "title", "url", "summary"}.issubset(cite):
            raise ValueError("citation shape mismatch")
        if cite["source"] not in served or not isinstance(cite["title"], str) or not cite["title"].strip():
            raise ValueError("citation source/title mismatch")
        if not isinstance(cite["summary"], str):
            raise ValueError("citation summary must be a string")
        if cite["source"] in ("datacite", "openaire", "europepmc") and "year" not in cite:
            raise ValueError("new citation year field is missing")
        if "year" in cite and (type(cite["year"]) not in (str, int)):
            raise ValueError("citation year type is invalid")
        if cite["source"] in ("zenodo", "ror"):
            expected_kind = "metadata_description" if cite["source"] == "zenodo" else "structured_metadata"
            if cite.get("summary_kind") != expected_kind or cite.get("license") != "CC0-1.0":
                raise ValueError("open metadata evidence/license mismatch")
            if "year" not in cite or (cite["source"] == "ror" and cite["year"] != ""):
                raise ValueError("ROR establishment must not be a publication year")
            if cite["source"] == "zenodo" and not {"file_license", "access_right"}.issubset(cite):
                raise ValueError("Zenodo file conditions are missing")
        S._kb_http_url(cite["url"])
        if not set(cite.get("providers") or [cite["source"]]).issubset(served):
            raise ValueError("merged citation provider mismatch")
    return cites


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", nargs="+", choices=S.SOURCES,
                        default=["datacite", "openaire", "europepmc"])
    parser.add_argument("--datacite-kind", choices=("all", "arxiv", "dataset"), default="arxiv")
    parser.add_argument("--query", default="CRISPR gene editing")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--fallback", action="store_true")
    args = parser.parse_args()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["FREEAGENT_HARNESS"] = "hermes"
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "freeagent_bind", "server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", env=env, bufsize=1)
    replies = queue.Queue()
    errors = []

    def read_stdout():
        for line in proc.stdout:
            try:
                replies.put(json.loads(line))
            except ValueError as exc:
                replies.put({"reader_error": str(exc)})

    def read_stderr():
        for line in proc.stderr:
            errors.append(line.strip())

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()

    def request(ident, method, params):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method,
                                     "params": params}, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 35
        while True:
            reply = replies.get(timeout=max(0.001, deadline - time.monotonic()))
            if reply.get("reader_error"):
                raise ValueError(reply["reader_error"])
            if reply.get("id") == ident:
                if "error" in reply:
                    raise ValueError(str(reply["error"]))
                return reply["result"]
            if time.monotonic() >= deadline:
                raise TimeoutError("stdio response deadline")

    try:
        request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "knowledge-probe", "version": "1"}})
        proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n')
        proc.stdin.flush()
        tools = request(2, "tools/list", {})["tools"]
        if len(tools) != 11:
            raise ValueError("tool count mismatch")
        properties = next(t for t in tools if t["name"] == "freeagent_lookup")["inputSchema"]["properties"]
        if not {"datacite_kind", "fallback"}.issubset(properties):
            raise ValueError("new lookup schema is missing")
        result = request(3, "tools/call", {"name": "freeagent_lookup", "arguments": {
            "query": args.query, "sources": args.sources, "datacite_kind": args.datacite_kind,
            "fallback": args.fallback, "limit": args.limit}})
        data = result.get("structuredContent") or {}
        content = result.get("content") or []
        if not data or not content:
            raise ValueError("both result channels are required")
        print(content[0].get("text", ""))
        citations = validate_lookup(data, args.sources, args.fallback)
        print(json.dumps({"citation_count": len(citations), "timings": data.get("timings"),
                          "summaries": sum(bool(c["summary"]) for c in citations),
                          "acquisition_sources": sorted({c["source"] for c in citations})}, ensure_ascii=False))
        print("知識API stdio検証 OK（initialize / tools/list / 実API tools/call）")
        return 0
    except Exception as exc:
        print(f"知識API stdio検証失敗: {type(exc).__name__}: {exc}")
        if errors:
            print("stderr: " + " / ".join(errors[-3:]))
        return 1
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
