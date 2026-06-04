#!/usr/bin/env python3
"""
Build and query the OpenShift project dependency graph from cached data.
Usage:
  python build_graph.py                     # summary stats
  python build_graph.py --dot               # emit DOT for graphviz
  python build_graph.py --focus api         # who depends on openshift/api?
  python build_graph.py --focus api client-go library-go --dot > graph.dot
  python build_graph.py --json > graph.json
"""

import json
import sys
import argparse
import re
from pathlib import Path
from collections import defaultdict


CACHE_FILE = Path(".cache/deps.json")
KEY_PROJECTS = ["openshift/api", "openshift/client-go", "openshift/library-go"]


def load_data():
    if not CACHE_FILE.exists():
        print("ERROR: .cache/deps.json not found. Run fetch_deps.py first.", file=sys.stderr)
        sys.exit(1)
    return json.loads(CACHE_FILE.read_text())


def short(module_path):
    """github.com/openshift/foo -> openshift/foo"""
    return re.sub(r"^github\.com/", "", module_path)


def build_graph(data):
    """
    Returns:
      edges: dict[src_repo] = list of openshift/* module paths depended on
      module_to_repo: dict[module_path] = repo_name
    """
    module_to_repo = {}
    for repo, info in data.items():
        if info and info.get("module"):
            module_to_repo[info["module"]] = repo

    # edges[repo] = set of dependency module paths (openshift/* only)
    edges = defaultdict(set)
    for repo, info in data.items():
        if not info:
            continue
        for mod, _ver in (info.get("openshift_deps") or []):
            # Normalize: strip /v2, /v3 etc for matching
            base_mod = re.sub(r"/v\d+$", "", mod)
            edges[repo].add(base_mod)

    return edges, module_to_repo


def reverse_graph(edges):
    """Who depends on each module?"""
    rev = defaultdict(set)
    for repo, deps in edges.items():
        for dep in deps:
            rev[dep].add(repo)
    return rev


def print_summary(data, edges, rev):
    total = len(data)
    with_gomod = sum(1 for v in data.values() if v and v.get("module"))
    with_os_deps = sum(1 for v in edges.values() if v)

    print(f"Repos scanned:            {total}")
    print(f"  with go.mod:            {with_gomod}")
    print(f"  with openshift/* deps:  {with_os_deps}")
    print()

    print("Top 20 most-depended-on openshift/* modules:")
    ranked = sorted(rev.items(), key=lambda x: -len(x[1]))
    for mod, dependents in ranked[:20]:
        print(f"  {len(dependents):4d}  {short(mod)}")

    print()
    print("Key project dependents:")
    for kp in KEY_PROJECTS:
        mod = f"github.com/{kp}"
        deps = rev.get(mod, set())
        print(f"  {kp}: {len(deps)} dependents")


def emit_dot(data, edges, rev, focus=None):
    """Emit a DOT graph. If focus is set, only show nodes reachable from/to focus modules."""
    lines = ["digraph openshift_deps {", '  rankdir=LR;', '  node [shape=box fontsize=10];']

    if focus:
        focus_mods = {f"github.com/openshift/{f}" for f in focus}
        # Include only repos that depend on a focused module
        include_repos = set()
        for mod in focus_mods:
            include_repos.update(rev.get(mod, set()))
        # Also include the focus modules themselves as nodes
        include_repos.update(focus)
    else:
        include_repos = set(edges.keys())

    # Style key projects differently
    key_mods = {f"github.com/openshift/{kp.split('/')[-1]}" for kp in KEY_PROJECTS}

    seen_nodes = set()

    def node_id(name):
        return re.sub(r"[^a-zA-Z0-9_]", "_", name)

    for repo in sorted(include_repos):
        nid = node_id(repo)
        info = data.get(repo, {}) or {}
        mod = info.get("module", f"github.com/openshift/{repo}")
        label = short(mod)
        style = ""
        if mod in key_mods or any(mod.endswith(f"/{kp.split('/')[-1]}") for kp in KEY_PROJECTS):
            style = ' style=filled fillcolor="#ffcc00"'
        if nid not in seen_nodes:
            lines.append(f'  {nid} [label="{label}"{style}];')
            seen_nodes.add(nid)

    for repo in sorted(include_repos):
        repo_deps = edges.get(repo, set())
        for dep in sorted(repo_deps):
            base = re.sub(r"/v\d+$", "", dep)
            dep_short = short(base)
            if focus:
                dep_mods = {f"github.com/openshift/{f}" for f in focus}
                if base not in dep_mods:
                    continue
            dep_repo = dep_short.replace("openshift/", "")
            nid_src = node_id(repo)
            nid_dst = node_id(dep_short)
            if nid_dst not in seen_nodes:
                style = ""
                if base in key_mods:
                    style = ' style=filled fillcolor="#ffcc00"'
                lines.append(f'  {nid_dst} [label="{dep_short}"{style}];')
                seen_nodes.add(nid_dst)
            lines.append(f"  {nid_src} -> {nid_dst};")

    lines.append("}")
    print("\n".join(lines))


def emit_json(data, edges, rev):
    """Emit a structured JSON dependency graph."""
    graph = {}
    for repo, deps in edges.items():
        info = data.get(repo, {}) or {}
        graph[repo] = {
            "module": info.get("module"),
            "depends_on": sorted(short(d) for d in deps),
        }
    # Add reverse index for key projects
    result = {
        "graph": graph,
        "key_project_dependents": {
            kp: sorted(rev.get(f"github.com/{kp}", set()))
            for kp in KEY_PROJECTS
        },
    }
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Build OpenShift dependency graph")
    parser.add_argument("--dot", action="store_true", help="Emit DOT format (graphviz)")
    parser.add_argument("--json", action="store_true", help="Emit JSON graph")
    parser.add_argument(
        "--focus",
        nargs="+",
        metavar="REPO",
        help="Focus on repos that depend on these openshift/* repos (e.g. api client-go)",
    )
    args = parser.parse_args()

    data = load_data()
    edges, module_to_repo = build_graph(data)
    rev = reverse_graph(edges)

    if args.dot:
        emit_dot(data, edges, rev, focus=args.focus)
    elif args.json:
        emit_json(data, edges, rev)
    else:
        print_summary(data, edges, rev)
        if args.focus:
            print()
            print(f"Repos depending on {args.focus}:")
            focus_mods = {f"github.com/openshift/{f}" for f in args.focus}
            dependents = set()
            for mod in focus_mods:
                dependents.update(rev.get(mod, set()))
            for dep in sorted(dependents):
                print(f"  {dep}")


if __name__ == "__main__":
    main()
