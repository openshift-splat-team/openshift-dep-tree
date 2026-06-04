#!/usr/bin/env python3
"""
MCP server exposing OpenShift dependency tree analysis.

Provides tools for feature impact analysis, repo lookup, dependency
graph queries, and API usage search over pre-generated JSON data.

Usage:
  python mcp_server.py                    # start stdio server
  pip install mcp                         # install dependency (if needed)
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent))

from feature_impact import load_json, run_impact, tokenize
from fetch_repo_metadata import CLASSIFICATION_RULES, PLATFORM_RULES

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

mcp = FastMCP("openshift-dep-tree")

_BASE_DIR = Path(os.environ.get("MCP_DATA_DIR", Path(__file__).resolve().parent))

_graph = {}
_api_usage = {}
_repo_meta = {}
_deps = {}
_reverse_deps: dict[str, list[str]] = {}


def _load_data():
    global _graph, _api_usage, _repo_meta, _deps, _reverse_deps
    _graph = load_json(_BASE_DIR / "graph.json", "graph.json")
    _api_usage = load_json(_BASE_DIR / "api_usage.json", "api_usage.json")
    _repo_meta = load_json(_BASE_DIR / "repo_metadata.json", "repo_metadata.json")
    _deps = load_json(_BASE_DIR / ".cache" / "deps.json", "deps.json")

    _reverse_deps = {}
    if _graph and "graph" in _graph:
        for repo, info in _graph["graph"].items():
            for dep in info.get("depends_on", []):
                short = dep.split("/")[-1] if "/" in dep else dep
                _reverse_deps.setdefault(short, []).append(repo)

    total = len(_all_repos())
    log.info("Loaded data for %d repos", total)


def _normalize_repo(repo: str) -> str:
    if repo.startswith("openshift/"):
        repo = repo[len("openshift/"):]
    return repo.strip()


def _all_repos() -> set[str]:
    repos: set[str] = set()
    if _graph and "graph" in _graph:
        repos.update(_graph["graph"].keys())
    if _api_usage and "repo_usage" in _api_usage:
        repos.update(_api_usage["repo_usage"].keys())
    if _repo_meta and "repos" in _repo_meta:
        repos.update(_repo_meta["repos"].keys())
    if _deps:
        repos.update(_deps.keys())
    return repos


def _parse_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def _file_info(path: Path, data: dict, count_key: str | None = None) -> dict:
    info: dict = {"exists": path.exists()}
    if path.exists():
        stat = path.stat()
        info["last_modified"] = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat()
        info["size_bytes"] = stat.st_size
        if count_key and data and count_key in data:
            info["record_count"] = len(data[count_key])
    else:
        info["note"] = f"Run the pipeline to generate. See --help on the relevant script."
    return info


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def feature_impact_tool(
    feature: str,
    platform: str = "",
    classification: str = "",
    top: int = 30,
    min_score: float = 10.0,
) -> str:
    """Find OpenShift repos most likely impacted by a feature change.

    Scores ~250 repos by relevance using keyword matching across repo names,
    descriptions, topics, dependency graphs, and openshift/api package usage.

    Args:
        feature: Description of the feature or change (e.g. "storage encryption at rest",
                 "dual stack IPv6 networking", "etcd backup and restore").
        platform: Optional comma-separated platform filter. Valid values:
                  aws, azure, gcp, vsphere, baremetal, openstack, ibm, nutanix, ovirt, kubevirt.
        classification: Optional comma-separated classification filter. Valid values:
                        storage, networking, monitoring, security, installation,
                        machine-management, cloud-compute, operator-framework,
                        image-registry, update, scaling, console, etcd, scheduling, build.
        top: Maximum number of results to return (default 30).
        min_score: Minimum relevance score threshold 0-100 (default 10).
    """
    tokens = tokenize(feature)
    if not tokens:
        return json.dumps({
            "error": "Feature description produced no searchable tokens after filtering stopwords. Try a more specific description."
        })

    plat = _parse_csv(platform)
    cls = _parse_csv(classification)

    results = run_impact(feature, plat, cls, _graph, _api_usage, _repo_meta, top, min_score)

    return json.dumps({
        "query": {"feature": feature, "platform": plat, "classification": cls},
        "result_count": len(results),
        "results": results,
    }, indent=2)


@mcp.tool()
async def get_repo_info(repo: str) -> str:
    """Get comprehensive information about a specific OpenShift repo.

    Combines metadata (description, platforms, classifications, upstream status),
    dependency graph, and API usage data from all available sources.

    Args:
        repo: Repository name, with or without org prefix
              (e.g. "cluster-etcd-operator" or "openshift/installer").
    """
    repo = _normalize_repo(repo)

    if repo not in _all_repos():
        return json.dumps({
            "error": f"Repo '{repo}' not found. Use search_repos to find repos by keyword, or list_repos to see all available repos."
        })

    result: dict = {"repo": repo}

    meta = (_repo_meta or {}).get("repos", {}).get(repo)
    if meta:
        result["metadata"] = {
            "description": meta.get("description", ""),
            "summary": meta.get("summary", ""),
            "topics": meta.get("topics", []),
            "platforms": meta.get("platforms", []),
            "classifications": meta.get("classifications", []),
            "fork": meta.get("fork", False),
            "upstream": meta.get("upstream"),
            "stars": meta.get("stars", 0),
        }
    else:
        result["metadata"] = None
        result["metadata_note"] = "No metadata available. Run fetch_repo_metadata.py to generate."

    graph_data = (_graph or {}).get("graph", {}).get(repo)
    if graph_data:
        result["dependencies"] = {
            "module": graph_data.get("module", ""),
            "depends_on": graph_data.get("depends_on", []),
            "depends_on_count": len(graph_data.get("depends_on", [])),
        }
    else:
        result["dependencies"] = None

    result["depended_on_by"] = sorted(_reverse_deps.get(repo, []))
    result["depended_on_by_count"] = len(result["depended_on_by"])

    api_info = (_api_usage or {}).get("repo_usage", {}).get(repo)
    if api_info:
        result["api_usage"] = {
            "packages": api_info.get("packages", []),
            "kinds": api_info.get("kinds", []),
            "package_count": len(api_info.get("packages", [])),
            "kind_count": len(api_info.get("kinds", [])),
            "is_known_go_dep": api_info.get("is_known_go_dep", False),
        }
    else:
        result["api_usage"] = None

    dep_info = (_deps or {}).get(repo)
    if dep_info:
        result["go_module"] = dep_info.get("module", "")

    return json.dumps(result, indent=2)


@mcp.tool()
async def list_repos(
    platform: str = "",
    classification: str = "",
    has_api_usage: bool = False,
    is_fork: bool | None = None,
) -> str:
    """List OpenShift repos with optional filters.

    Good for discovering repos before drilling into details with get_repo_info
    or running feature_impact_tool.

    Args:
        platform: Filter to repos matching this platform (e.g. "aws", "vsphere").
        classification: Filter to repos matching this classification (e.g. "networking", "storage").
        has_api_usage: If true, only show repos that import openshift/api packages.
        is_fork: If set, filter by fork status (true = forks only, false = non-forks only).
    """
    plat = _parse_csv(platform)
    cls = _parse_csv(classification)
    repos_meta = (_repo_meta or {}).get("repos", {})
    api_repo_usage = (_api_usage or {}).get("repo_usage", {})

    matches = []
    for repo in sorted(_all_repos()):
        meta = repos_meta.get(repo, {})

        if plat:
            repo_plats = set(meta.get("platforms", []))
            if not repo_plats & set(plat):
                continue

        if cls:
            repo_cls = set(meta.get("classifications", []))
            if not repo_cls & set(cls):
                continue

        if has_api_usage and repo not in api_repo_usage:
            continue

        if is_fork is not None:
            if meta.get("fork", False) != is_fork:
                continue

        matches.append({
            "repo": repo,
            "description": meta.get("summary") or meta.get("description", ""),
            "platforms": meta.get("platforms", []),
            "classifications": meta.get("classifications", []),
        })

    filters_desc = []
    if plat:
        filters_desc.append(f"platform={','.join(plat)}")
    if cls:
        filters_desc.append(f"classification={','.join(cls)}")
    if has_api_usage:
        filters_desc.append("has_api_usage=true")
    if is_fork is not None:
        filters_desc.append(f"is_fork={is_fork}")

    return json.dumps({
        "filters": " | ".join(filters_desc) if filters_desc else "none",
        "count": len(matches),
        "repos": matches,
    }, indent=2)


@mcp.tool()
async def get_repo_dependencies(repo: str) -> str:
    """Get dependency relationships for an OpenShift repo.

    Shows which openshift/* modules this repo depends on (forward) and which
    repos depend on it (reverse).

    Args:
        repo: Repository name, with or without org prefix (e.g. "library-go", "api").
    """
    repo = _normalize_repo(repo)

    graph_entry = (_graph or {}).get("graph", {}).get(repo)
    rev = sorted(_reverse_deps.get(repo, []))

    if not graph_entry and not rev:
        if repo not in _all_repos():
            return json.dumps({
                "error": f"Repo '{repo}' not found. Use search_repos or list_repos to discover valid repo names."
            })
        return json.dumps({
            "repo": repo,
            "note": "No dependency data available in graph.json for this repo.",
            "depends_on": [],
            "depended_on_by": rev,
        })

    fwd = graph_entry.get("depends_on", []) if graph_entry else []
    return json.dumps({
        "repo": repo,
        "module": (graph_entry or {}).get("module", ""),
        "depends_on": fwd,
        "depends_on_count": len(fwd),
        "depended_on_by": rev,
        "depended_on_by_count": len(rev),
    }, indent=2)


@mcp.tool()
async def get_repo_api_usage(repo: str) -> str:
    """Get the openshift/api packages and CRD kinds used by a repo.

    Shows which API groups/versions (e.g. config/v1, machine/v1beta1) the repo
    imports and which CRD kinds (e.g. ClusterOperator, Infrastructure) it references.

    Args:
        repo: Repository name, with or without org prefix (e.g. "cluster-etcd-operator").
    """
    repo = _normalize_repo(repo)
    api_info = (_api_usage or {}).get("repo_usage", {}).get(repo)

    if not api_info:
        if repo not in _all_repos():
            return json.dumps({
                "error": f"Repo '{repo}' not found. Use search_repos or list_repos to discover valid repo names."
            })
        return json.dumps({
            "repo": repo,
            "note": "No openshift/api usage found for this repo.",
            "packages": [],
            "kinds": [],
        })

    return json.dumps({
        "repo": repo,
        "packages": api_info.get("packages", []),
        "kinds": api_info.get("kinds", []),
        "package_count": len(api_info.get("packages", [])),
        "kind_count": len(api_info.get("kinds", [])),
        "is_known_go_dep": api_info.get("is_known_go_dep", False),
    }, indent=2)


@mcp.tool()
async def search_repos(query: str) -> str:
    """Search for OpenShift repos by name, description, or topic.

    Simple substring matching (case-insensitive). For scored relevance ranking
    against a feature description, use feature_impact_tool instead.

    Args:
        query: Search text to match against repo names, descriptions, and topics.
    """
    if not query or not query.strip():
        return json.dumps({"error": "Query must not be empty."})

    q = query.strip().lower()
    repos_meta = (_repo_meta or {}).get("repos", {})
    matches = []

    for repo in sorted(_all_repos()):
        meta = repos_meta.get(repo, {})
        matched_in = []

        if q in repo.lower():
            matched_in.append("name")
        desc = (meta.get("description") or "").lower()
        if desc and q in desc:
            matched_in.append("description")
        topics = [t.lower() for t in (meta.get("topics") or [])]
        if any(q in t for t in topics):
            matched_in.append("topic")

        if matched_in:
            matches.append({
                "repo": repo,
                "matched_in": matched_in,
                "description": meta.get("summary") or meta.get("description", ""),
                "platforms": meta.get("platforms", []),
                "classifications": meta.get("classifications", []),
            })

    matches.sort(key=lambda m: (0 if "name" in m["matched_in"] else 1, m["repo"]))

    return json.dumps({
        "query": query,
        "count": len(matches),
        "matches": matches,
    }, indent=2)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("openshift://filters")
def get_available_filters() -> str:
    """Available platform and classification filter values for feature_impact_tool and list_repos."""
    return json.dumps({
        "platforms": {k: v for k, v in PLATFORM_RULES.items()},
        "classifications": {k: v for k, v in CLASSIFICATION_RULES.items()},
    }, indent=2)


@mcp.resource("openshift://data-freshness")
def get_data_freshness() -> str:
    """Shows when each data file was last generated and whether it exists."""
    graph_path = _BASE_DIR / "graph.json"
    api_path = _BASE_DIR / "api_usage.json"
    meta_path = _BASE_DIR / "repo_metadata.json"
    deps_path = _BASE_DIR / ".cache" / "deps.json"

    return json.dumps({
        "graph.json": _file_info(graph_path, _graph, "graph"),
        "api_usage.json": _file_info(api_path, _api_usage, "repo_usage"),
        "repo_metadata.json": _file_info(meta_path, _repo_meta, "repos"),
        ".cache/deps.json": _file_info(deps_path, _deps, None),
    }, indent=2)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

os.chdir(_BASE_DIR)
_load_data()


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
