#!/usr/bin/env python3
"""
Map which openshift/api packages (API groups/versions) and CRD kinds each
OpenShift project relies on.

Workflow:
  1. Enumerate openshift/api packages + extract Kind names from types files
  2. Use GitHub code search (rate-limited) to find which repos import each package
  3. Cache everything under .cache/; re-run is fast if cache exists

Usage:
  python analyze_api_usage.py                # build cache & print summary
  python analyze_api_usage.py --repo cluster-autoscaler-operator
  python analyze_api_usage.py --package config/v1
  python analyze_api_usage.py --json > api_usage.json
  python analyze_api_usage.py --top-packages   # most-imported packages
"""

import json
import subprocess
import base64
import re
import sys
import time
import argparse
from pathlib import Path
from collections import defaultdict

CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)

API_PACKAGES_CACHE = CACHE_DIR / "openshift_api_packages.json"
USAGE_CACHE_DIR = CACHE_DIR / "api_usage_by_package"
USAGE_CACHE_DIR.mkdir(exist_ok=True)
COMBINED_CACHE = CACHE_DIR / "api_usage_combined.json"

ORG = "openshift"
API_REPO = "openshift/api"

# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh(endpoint, jq=None, paginate=False, accept=None):
    cmd = ["gh", "api", endpoint]
    if paginate:
        cmd.append("--paginate")
    if jq:
        cmd += ["--jq", jq]
    if accept:
        cmd += ["-H", f"Accept: {accept}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        parsed = []
        for l in lines:
            try:
                parsed.append(json.loads(l))
            except Exception:
                pass
        return parsed if parsed else None


def search_code(query, per_page=100, page=1):
    """Call GitHub code search API. Returns (items, total_count, ok) tuple.
    ok=False means the call failed (rate limit or error); caller should not cache."""
    # Encode manually: encode everything including slashes so GitHub search gets exact tokens.
    # Use + for spaces (standard for search query strings).
    encoded = (
        query
        .replace("%", "%25")   # must be first
        .replace('"', "%22")
        .replace("/", "%2F")
        .replace(":", "%3A")
        .replace(" ", "+")
    )
    endpoint = f"search/code?q={encoded}&per_page={per_page}&page={page}"
    result = gh(endpoint)
    if not result or not isinstance(result, dict):
        return [], 0, False
    # Distinguish API returning 0 legitimately vs a failed call
    if "total_count" not in result:
        return [], 0, False
    return result.get("items", []), result.get("total_count", 0), True


# ---------------------------------------------------------------------------
# Phase 1: Enumerate openshift/api packages and CRD kinds
# ---------------------------------------------------------------------------

_ACRONYMS = {"api", "dns", "ip", "tls", "oauth", "kms", "csi", "pki", "crd", "olm", "ocm", "ptp"}

def _snake_to_pascal(name):
    """types_cluster_operator -> ClusterOperator; handles common acronyms."""
    parts = name.split("_")
    result = []
    for part in parts:
        if part.lower() in _ACRONYMS:
            result.append(part.upper())
        else:
            result.append(part.capitalize())
    return "".join(result)


def _kinds_from_filename(filename):
    """Heuristically derive Kind name(s) from a types_*.go filename."""
    base = filename.replace(".go", "")
    if base == "types":
        return []  # catch-all file, handled separately
    if base.startswith("types_"):
        kind = _snake_to_pascal(base[len("types_"):])
        # Filter test types
        if "test" in kind.lower():
            return []
        # Drop List variant if we somehow produce one
        return [kind]
    return []


def _fetch_types_file(path):
    """Fetch a Go types file from openshift/api and extract exported struct names."""
    r = subprocess.run(
        ["gh", "api", f"repos/{API_REPO}/contents/{path}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        data = json.loads(r.stdout)
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        return []

    kinds = []
    # Look for exported struct names that likely represent root API objects.
    # Heuristic: exported struct with DeepCopyObject or +kubebuilder:object:root annotation,
    # or structs that appear right after a // +genclient comment.
    lines = content.splitlines()
    prev_genclient = False
    for line in lines:
        stripped = line.strip()
        if "// +genclient" in stripped or "// +kubebuilder:object:root=true" in stripped:
            prev_genclient = True
        m = re.match(r"^type\s+([A-Z][A-Za-z0-9]+)\s+struct\s*\{", stripped)
        if m:
            name = m.group(1)
            if name.endswith("List"):
                prev_genclient = False
                continue
            if prev_genclient:
                kinds.append(name)
                prev_genclient = False
    return kinds


def get_api_packages(fetch_kinds=True):
    """
    Returns dict: {
      "config/v1": {
        "module_path": "github.com/openshift/api/config/v1",
        "kinds": ["APIServer", "Authentication", ...],
        "type_files": ["config/v1/types_apiserver.go", ...],
      },
      ...
    }
    """
    if API_PACKAGES_CACHE.exists():
        return json.loads(API_PACKAGES_CACHE.read_text())

    print("Fetching openshift/api tree...")
    tree_data = gh("repos/openshift/api/git/trees/HEAD?recursive=1")
    if not tree_data:
        print("ERROR: could not fetch openshift/api tree", file=sys.stderr)
        sys.exit(1)

    # Group types files by package
    packages = {}
    for item in tree_data.get("tree", []):
        path = item["path"]
        m = re.match(r"^([a-z][a-z0-9]+/v[0-9]+[a-z0-9]*)/((types[^/]*|zz_generated\.deepcopy)\.go)$", path)
        if not m:
            continue
        pkg = m.group(1)
        filename = m.group(2)
        if pkg not in packages:
            packages[pkg] = {
                "module_path": f"github.com/openshift/api/{pkg}",
                "type_files": [],
                "kinds": [],
            }
        packages[pkg]["type_files"].append(path)

    # Derive kind names by fetching all types files for accurate struct names
    if fetch_kinds:
        print(f"Extracting Kind names from {len(packages)} packages (fetching types files)...")
        for pkg, info in packages.items():
            kinds_set = set()
            for tf in info["type_files"]:
                if "zz_generated" in tf or "_test" in tf:
                    continue
                # Fetch each types file and parse +genclient-annotated structs
                fetched = _fetch_types_file(tf)
                if fetched:
                    kinds_set.update(fetched)
                else:
                    # Fallback: derive from filename when parsing finds nothing
                    fname = tf.split("/")[-1]
                    kinds_set.update(_kinds_from_filename(fname.replace(".go", "")))
                time.sleep(0.05)
            info["kinds"] = sorted(kinds_set)
            print(f"  {pkg}: {info['kinds']}")

    API_PACKAGES_CACHE.write_text(json.dumps(packages, indent=2))
    print(f"Saved {len(packages)} packages to {API_PACKAGES_CACHE}")
    return packages


# ---------------------------------------------------------------------------
# Phase 2: Search which repos import each package
# ---------------------------------------------------------------------------

def search_package_usage(pkg_path, module_path, max_pages=5):
    """
    Search GitHub for files in the openshift org that import this package.
    Returns set of repo names.  Returns None if the search call failed (caller
    should not cache and should retry later).
    """
    cache_file = USAGE_CACHE_DIR / (pkg_path.replace("/", "_") + ".json")
    if cache_file.exists():
        return set(json.loads(cache_file.read_text()))

    query = f'"{module_path}" org:{ORG} language:Go'
    repos = set()
    total_count = None
    had_error = False

    for page in range(1, max_pages + 1):
        items, count, ok = search_code(query, per_page=100, page=page)
        if not ok:
            had_error = True
            break
        if total_count is None:
            total_count = count
        for item in items:
            repos.add(item["repository"]["name"])
        fetched_so_far = (page - 1) * 100 + len(items)
        if fetched_so_far >= min(total_count, max_pages * 100):
            break
        if page < max_pages:
            time.sleep(2.1)  # stay under 30 req/min for search

    if had_error:
        return None  # don't cache; caller will retry

    cache_file.write_text(json.dumps(sorted(repos)))
    return repos


def build_usage_map(packages, force=False):
    """
    Returns combined map: {
      "repo_name": {
        "packages": ["config/v1", "route/v1", ...],
        "kinds": ["ClusterOperator", "Route", ...],   # union of kinds from used packages
      }
    }
    """
    if COMBINED_CACHE.exists() and not force:
        return json.loads(COMBINED_CACHE.read_text())

    # Load our known go.mod dependent repos for cross-reference
    deps_file = CACHE_DIR / "deps.json"
    known_api_repos = set()
    if deps_file.exists():
        deps = json.loads(deps_file.read_text())
        for repo, info in deps.items():
            if info and any(
                "openshift/api" in d[0]
                or "openshift/client-go" in d[0]
                for d in (info.get("openshift_deps") or [])
            ):
                known_api_repos.add(repo)
    print(f"\n{len(known_api_repos)} repos known to depend on openshift/api or client-go")

    # Skip example/legacy packages that aren't real CRDs
    SKIP_PKGS = {"example/v1", "example/v1alpha1", "legacyconfig/v1", "osin/v1",
                 "securityinternal/v1", "servicecertsigner/v1alpha1"}

    repo_to_packages = defaultdict(set)
    pkg_list = sorted(p for p in packages if p not in SKIP_PKGS)
    total = len(pkg_list)

    for i, pkg in enumerate(pkg_list):
        module_path = packages[pkg]["module_path"]
        print(f"[{i+1}/{total}] Searching usage of {module_path}...", end=" ", flush=True)
        # Retry up to 3 times with backoff on rate-limit errors
        repos = None
        for attempt in range(3):
            repos = search_package_usage(pkg, module_path)
            if repos is not None:
                break
            wait = 62 * (attempt + 1)
            print(f"  rate-limited, waiting {wait}s...", end=" ", flush=True)
            time.sleep(wait)
        if repos is None:
            print("FAILED (skipped)")
            continue
        print(f"{len(repos)} repos")
        for repo in repos:
            repo_to_packages[repo].add(pkg)
        if i < total - 1:
            time.sleep(2.1)  # rate limit: 30 search req/min

    # Build the final combined map
    combined = {}
    for repo in sorted(repo_to_packages):
        pkgs_used = sorted(repo_to_packages[repo])
        kinds_used = sorted(set(
            k
            for p in pkgs_used
            for k in packages.get(p, {}).get("kinds", [])
        ))
        combined[repo] = {
            "packages": pkgs_used,
            "kinds": kinds_used,
            "is_known_go_dep": repo in known_api_repos,
        }

    COMBINED_CACHE.write_text(json.dumps(combined, indent=2))
    print(f"\nSaved usage map for {len(combined)} repos to {COMBINED_CACHE}")
    return combined


# ---------------------------------------------------------------------------
# Output / query functions
# ---------------------------------------------------------------------------

def print_summary(packages, combined):
    print(f"\n=== openshift/api Usage Summary ===")
    print(f"API packages indexed: {len(packages)}")
    print(f"Repos using openshift/api (via code search): {len(combined)}")

    # Package popularity
    pkg_counts = defaultdict(int)
    for info in combined.values():
        for p in info["packages"]:
            pkg_counts[p] += 1

    print(f"\nTop 20 most-imported openshift/api packages:")
    for pkg, cnt in sorted(pkg_counts.items(), key=lambda x: -x[1])[:20]:
        kinds = packages.get(pkg, {}).get("kinds", [])
        kind_str = ", ".join(kinds[:4]) + ("…" if len(kinds) > 4 else "")
        print(f"  {cnt:4d}  {pkg:35s}  [{kind_str}]")


def print_repo(repo, packages, combined):
    info = combined.get(repo)
    if not info:
        print(f"Repo '{repo}' not found in usage map.")
        return
    print(f"\n{repo}")
    print(f"  openshift/api packages used ({len(info['packages'])}):")
    for pkg in info["packages"]:
        kinds = packages.get(pkg, {}).get("kinds", [])
        print(f"    {pkg:35s}  {', '.join(kinds)}")
    print(f"\n  All referenced kinds ({len(info['kinds'])}):")
    for chunk in [info["kinds"][i:i+8] for i in range(0, len(info["kinds"]), 8)]:
        print(f"    {', '.join(chunk)}")


def print_package(pkg, packages, combined):
    info = packages.get(pkg)
    if not info:
        print(f"Package '{pkg}' not found.")
        return
    print(f"\ngithub.com/openshift/api/{pkg}")
    print(f"  Kinds defined: {', '.join(info.get('kinds', []))}")
    dependents = sorted(r for r, ri in combined.items() if pkg in ri["packages"])
    print(f"  Used by {len(dependents)} repos:")
    for r in dependents:
        print(f"    {r}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", help="Show details for a specific repo")
    parser.add_argument("--package", help="Show details for a specific package (e.g. config/v1)")
    parser.add_argument("--json", action="store_true", help="Emit full JSON to stdout")
    parser.add_argument("--top-packages", action="store_true", help="Show most-imported packages")
    parser.add_argument("--force", action="store_true", help="Rebuild combined cache")
    parser.add_argument("--no-kinds", action="store_true", help="Skip fetching kind names (faster)")
    args = parser.parse_args()

    packages = get_api_packages(fetch_kinds=not args.no_kinds)
    combined = build_usage_map(packages, force=args.force)

    if args.json:
        out = {
            "packages": packages,
            "repo_usage": combined,
        }
        print(json.dumps(out, indent=2))
    elif args.repo:
        print_repo(args.repo, packages, combined)
    elif args.package:
        print_package(args.package, packages, combined)
    else:
        print_summary(packages, combined)
        if args.top_packages:
            pass  # already printed in summary


if __name__ == "__main__":
    main()
