"""osv（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

_OSV_ECOSYSTEMS = ("PyPI", "npm", "crates.io", "Go", "Maven", "RubyGems", "NuGet", "Packagist",
                   "Hex", "Pub")
_OSV_ID_PATTERNS = (
    re.compile(r"^CVE-\d{4}-\d{4,7}$", re.I),
    re.compile(r"^GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}$", re.I),
    re.compile(r"^[A-Z]{2,12}-\d{4}-\d{2,6}$"),
    re.compile(r"^[A-Z]{2,12}-\d{3,6}$"),
)


def _osv_ecosystem(name: str) -> str:
    """1 リクエストで全生態系を照合し、最初に該当したものを返す（無ければ空文字）。"""
    payload = {"queries": [{"package": {"name": name, "ecosystem": eco}} for eco in _OSV_ECOSYSTEMS]}
    data, err = S._kb_new_json("https://api.osv.dev/v1/querybatch", 0.5, payload=payload,
                             budget_key="api.osv.dev/v1/querybatch")
    if err or not isinstance(data, dict):
        return ""
    rows = data.get("results")
    if not isinstance(rows, list):
        return ""
    for eco, row in zip(_OSV_ECOSYSTEMS, rows):
        vulns = row.get("vulns") if isinstance(row, dict) else None
        if isinstance(vulns, list) and vulns:
            return eco
    return ""


def _osv_item(row: dict) -> dict | None:
    vid = S.as_str(row.get("id"))
    if not vid:
        return None
    summary = S._plain_text(S.as_str(row.get("summary")) or S.as_str(row.get("details")), 600)
    aliases = [S.as_str(a) for a in S._kb_array(row.get("aliases"), "aliases") if S.as_str(a)]
    packages = []
    for affected in S._kb_array(row.get("affected"), "affected"):
        pkg = S._kb_object(affected.get("package") if isinstance(affected, dict) else None, "package")
        pname, peco = S.as_str(pkg.get("name")), S.as_str(pkg.get("ecosystem"))
        if pname:
            packages.append(f"{peco}/{pname}" if peco else pname)
    severity = ""
    for sev in S._kb_array(row.get("severity"), "severity"):
        if isinstance(sev, dict) and S.as_str(sev.get("score")):
            severity = S.as_str(sev.get("score"))
            break
    if not severity:
        severity = S.as_str(S._kb_object(row.get("database_specific"), "database_specific").get("severity"))
    return {"title": f"{vid}: {summary[:160]}" if summary else vid,
            "url": "https://osv.dev/vulnerability/" + urllib.parse.quote(vid, safe=""),
            "summary": summary, "identifier": vid, "aliases": aliases[:6],
            "package": sorted(set(packages))[:6], "severity": severity,
            "year": S.as_str(row.get("published"))[:4], "kind": "vulnerability"}


def kb_osv(query: str, limit: int = 5) -> dict:
    """OSV.dev の既知脆弱性。パッケージ名（生態系を自動照合）か脆弱性ID（CVE-/GHSA-/PYSEC- 等）。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "osv", "error": "query は必須です"}
    attribution = "データ提供: OSV.dev（OpenSSF OSV スキーマの集約） https://osv.dev/"

    def produce() -> dict:
        if any(pattern.match(query) for pattern in _OSV_ID_PATTERNS):
            data, err = S._kb_new_json("https://api.osv.dev/v1/vulns/"
                                     + urllib.parse.quote(query, safe=""), 0.5,
                                     budget_key="api.osv.dev/v1/vulns")
            if err:
                return {"source": "osv", "error": err}
            rows, ecosystem = [data], ""
        else:
            ecosystem = _osv_ecosystem(query)
            if not ecosystem:
                return {"source": "osv", "error":
                        "該当なし: OSV に登録されたパッケージ名（PyPI / npm / crates.io / Go / Maven / "
                        "RubyGems / NuGet / Packagist / Hex / Pub の順に照合）か、CVE- / GHSA- / PYSEC- 等の"
                        "脆弱性IDを指定してください。Maven は groupId:artifactId の形（例 "
                        "org.apache.logging.log4j:log4j-core）で指定します"}
            data, err = S._kb_new_json("https://api.osv.dev/v1/query", 0.5,
                                     payload={"package": {"name": query, "ecosystem": ecosystem}},
                                     budget_key="api.osv.dev/v1/query")
            if err:
                return {"source": "osv", "error": err}
            rows = S._kb_array(S._kb_object(data, "OSV応答").get("vulns"), "vulns")
        items = [item for item in (_osv_item(row) for row in rows[:limit]
                                   if isinstance(row, dict)) if item]
        return S._kb_generic_result("osv", items, attribution, ecosystem=ecosystem)

    return S._kb_new_cached(f"osv:{query}:{limit}", "osv", produce)



EXPORTS = {"_OSV_ECOSYSTEMS": _OSV_ECOSYSTEMS, "_OSV_ID_PATTERNS": _OSV_ID_PATTERNS, "_osv_ecosystem": _osv_ecosystem, "_osv_item": _osv_item, "kb_osv": kb_osv}


def backend(q, limit, opts):
    return S.kb_osv(q, limit)


register("osv", kb_osv, backend)
