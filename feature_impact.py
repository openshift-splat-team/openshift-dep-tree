#!/usr/bin/env python3
"""
Score OpenShift repos by relevance to a given feature description.
Combines metadata keyword matching, dependency graph, and API usage.

Usage:
  python feature_impact.py --feature "storage encryption at rest"
  python feature_impact.py --feature "dual stack IPv6" --platform aws azure
  python feature_impact.py --feature "etcd backup" --classification etcd --top 20
  python feature_impact.py --feature "ingress TLS" --json
  python feature_impact.py --feature "..." --output feature_impact.json
  python feature_impact.py --feature "..." --min-score 20
"""

import json
import re
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

STOPWORDS = {
    "a", "an", "the", "for", "of", "to", "in", "at", "on", "with",
    "using", "that", "is", "are", "which", "and", "or", "not", "any",
    "all", "this", "from", "by", "be", "been", "has", "have",
}


def tokenize(text):
    """Split feature text into lowercase tokens, filtering stopwords and short tokens."""
    tokens = re.split(r"\W+", text.lower())
    return [t for t in tokens if len(t) >= 3 and t not in STOPWORDS]


def load_json(path, label):
    p = Path(path)
    if not p.exists():
        print(f"WARNING: {label} not found at {path}", file=sys.stderr)
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: could not parse {path}: {e}", file=sys.stderr)
        return {}


def score_repo(repo, tokens, graph_data, api_info, meta, platform_filter, class_filter):
    """Compute relevance score for a repo against feature tokens. Returns (score, reasons)."""
    score = 0.0
    reasons = []

    if meta:
        repo_desc = (meta.get("description") or "").lower()
        repo_topics = " ".join(t.lower() for t in (meta.get("topics") or []))

        for tok in tokens:
            if tok in repo.lower():
                score += 25
                reasons.append(f"name:{tok}")
                break

        for tok in tokens:
            if tok in repo_desc:
                score += 20
                reasons.append(f"desc:{tok}")
                break

        for tok in tokens:
            if tok in repo_topics:
                score += 15
                reasons.append(f"topic:{tok}")
                break

        if class_filter:
            repo_cls = set(meta.get("classifications") or [])
            matched_cls = repo_cls & set(class_filter)
            if matched_cls:
                score += 20
                reasons.append(f"class:{','.join(sorted(matched_cls))}")
            else:
                score -= 10

        if platform_filter:
            repo_plat = set(meta.get("platforms") or [])
            matched_plat = repo_plat & set(platform_filter)
            if matched_plat:
                score += 15
                reasons.append(f"platform:{','.join(sorted(matched_plat))}")
            else:
                score -= 10
    else:
        # No metadata — apply filter penalties so filtered queries still exclude unknown repos
        if class_filter:
            score -= 10
        if platform_filter:
            score -= 10

    if api_info:
        api_pkgs = api_info.get("packages") or []
        api_kinds = api_info.get("kinds") or []

        for tok in tokens:
            if any(tok in p.lower() for p in api_pkgs):
                score += 10
                reasons.append(f"api-pkg:{tok}")
                break

        for tok in tokens:
            if any(tok in k.lower() for k in api_kinds):
                score += 10
                reasons.append(f"api-kind:{tok}")
                break

    if graph_data:
        deps = graph_data.get("depends_on") or []
        for tok in tokens:
            if any(tok in d.lower() for d in deps):
                score += 10
                reasons.append(f"dep:{tok}")
                break

    return max(0.0, min(100.0, score)), reasons


def run_impact(feature, platform_filter, class_filter, graph, api_usage, repo_meta, top, min_score):
    tokens = tokenize(feature)
    if not tokens:
        print("ERROR: feature description produced no tokens after filtering.", file=sys.stderr)
        sys.exit(1)

    all_repos = set()
    if graph and "graph" in graph:
        all_repos.update(graph["graph"].keys())
    if api_usage and "repo_usage" in api_usage:
        all_repos.update(api_usage["repo_usage"].keys())
    if repo_meta and "repos" in repo_meta:
        all_repos.update(repo_meta["repos"].keys())

    results = []
    for repo in sorted(all_repos):
        graph_data = (graph or {}).get("graph", {}).get(repo)
        api_info = (api_usage or {}).get("repo_usage", {}).get(repo)
        meta = (repo_meta or {}).get("repos", {}).get(repo)

        score, reasons = score_repo(
            repo, tokens, graph_data, api_info, meta, platform_filter, class_filter
        )
        if score >= min_score:
            results.append({
                "repo": repo,
                "score": score,
                "reasons": reasons,
                "metadata": {
                    "platforms": (meta or {}).get("platforms", []),
                    "classifications": (meta or {}).get("classifications", []),
                    "upstream": (meta or {}).get("upstream"),
                    "summary": (meta or {}).get("summary", ""),
                },
                "api_packages": (api_info or {}).get("packages", []),
                "depends_on": (graph_data or {}).get("depends_on", []),
            })

    results.sort(key=lambda r: -r["score"])
    return results[:top] if top else results


def print_table(feature, platform_filter, class_filter, results):
    print(f'\nFeature Impact: "{feature}"')
    filters = []
    if platform_filter:
        filters.append(f"platform={','.join(platform_filter)}")
    if class_filter:
        filters.append(f"classification={','.join(class_filter)}")
    if filters:
        print(f"Filters: {' | '.join(filters)}")
    print(f"\n{'#':>3}  {'REPO':<45}  {'SCORE':>5}  REASONS")
    print("-" * 80)
    for i, r in enumerate(results, 1):
        reasons_str = ", ".join(r["reasons"]) if r["reasons"] else "—"
        print(f"{i:>3}  {r['repo']:<45}  {r['score']:>5.0f}  {reasons_str}")
    if not results:
        print("  (no repos matched the query)")


def main():
    parser = argparse.ArgumentParser(description="Identify repos impacted by a feature")
    parser.add_argument("--feature", required=True, help="Feature description or keywords")
    parser.add_argument("--platform", nargs="+", help="Filter by cloud platform(s) e.g. aws azure")
    parser.add_argument("--classification", nargs="+", help="Filter by classification(s) e.g. networking")
    parser.add_argument("--top", type=int, default=30, help="Max results to show (default 30)")
    parser.add_argument("--min-score", type=float, default=10.0, help="Minimum score threshold (default 10)")
    parser.add_argument("--json", action="store_true", dest="emit_json", help="Emit JSON to stdout")
    parser.add_argument("--output", help="Write JSON results to file")
    parser.add_argument("--graph", default="graph.json", help="Path to graph.json")
    parser.add_argument("--api-usage", default="api_usage.json", help="Path to api_usage.json")
    parser.add_argument("--metadata", default="repo_metadata.json", help="Path to repo_metadata.json")
    args = parser.parse_args()

    graph = load_json(args.graph, "graph.json")
    api_usage = load_json(args.api_usage, "api_usage.json")
    repo_meta = load_json(args.metadata, "repo_metadata.json")

    results = run_impact(
        args.feature,
        args.platform or [],
        args.classification or [],
        graph,
        api_usage,
        repo_meta,
        args.top,
        args.min_score,
    )

    if args.emit_json or args.output:
        out = {
            "query": {
                "feature": args.feature,
                "platform": args.platform or [],
                "classification": args.classification or [],
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "results": results,
        }
        if args.output:
            Path(args.output).write_text(json.dumps(out, indent=2))
            print(f"Wrote {len(results)} results to {args.output}")
        else:
            print(json.dumps(out, indent=2))
    else:
        print_table(args.feature, args.platform or [], args.classification or [], results)


if __name__ == "__main__":
    main()
