#!/usr/bin/env python3
"""
Fetch go.mod files from openshift/* repos and build a dependency cache.
Run this first to populate .cache/ before running build_graph.py
"""

import json
import subprocess
import base64
import sys
import time
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)


def gh_api(endpoint, paginate=False, jq=None):
    cmd = ["gh", "api", endpoint]
    if paginate:
        cmd.append("--paginate")
    if jq:
        cmd.extend(["--jq", jq])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # paginate with --jq returns newline-separated values
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def list_go_repos(org="openshift"):
    """List non-archived, non-fork Go repos in the org."""
    cache_file = CACHE_DIR / "repos.json"
    if cache_file.exists():
        print(f"Using cached repo list ({cache_file})")
        return json.loads(cache_file.read_text())

    print(f"Fetching repo list for {org}...")
    repos = gh_api(
        f"orgs/{org}/repos?type=public&per_page=100",
        paginate=True,
        jq='[.[] | select(.archived == false and .fork == false and .language == "Go") | {name: .name, language: .language}]',
    )
    if not repos:
        print("Failed to fetch repos", file=sys.stderr)
        return []

    # Flatten list-of-lists from paginated jq output
    flat = []
    for item in repos:
        if isinstance(item, list):
            flat.extend(item)
        else:
            flat.append(item)

    names = [r["name"] for r in flat]
    print(f"Found {len(names)} active Go repos")
    cache_file.write_text(json.dumps(names, indent=2))
    return names


def fetch_go_mod(org, repo):
    """Fetch go.mod content for a repo. Returns (module_path, content) or (None, None)."""
    cache_file = CACHE_DIR / f"{repo}.gomod"
    if cache_file.exists():
        content = cache_file.read_text()
        if content == "NONE":
            return None, None
        return _parse_module_name(content), content

    result = subprocess.run(
        ["gh", "api", f"repos/{org}/{repo}/contents/go.mod"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        cache_file.write_text("NONE")
        return None, None

    try:
        data = json.loads(result.stdout)
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        cache_file.write_text(content)
        return _parse_module_name(content), content
    except (json.JSONDecodeError, KeyError, Exception):
        cache_file.write_text("NONE")
        return None, None


def _parse_module_name(content):
    for line in content.splitlines():
        m = re.match(r"^module\s+(\S+)", line)
        if m:
            return m.group(1)
    return None


def parse_openshift_deps(content):
    """
    Extract direct openshift/* dependencies from go.mod content.
    Returns list of (module_path, version) tuples.
    """
    deps = []
    in_require = False
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("require ("):
            in_require = True
            continue
        if in_require and line == ")":
            in_require = False
            continue
        # single-line require
        if line.startswith("require ") and not line.endswith("("):
            line = line[len("require "):]
            in_require = False

        if in_require or line.startswith("github.com/openshift/"):
            # parse "github.com/openshift/something vX.Y.Z"
            m = re.match(r"(github\.com/openshift/[^\s]+)\s+(\S+)", line)
            if m:
                mod, ver = m.group(1), m.group(2)
                # Skip indirect/retract markers kept on same line in some formats
                if "// indirect" in line:
                    continue
                deps.append((mod, ver))
    return deps


def fetch_all(org="openshift", workers=10):
    repos = list_go_repos(org)
    print(f"\nFetching go.mod for {len(repos)} repos (workers={workers})...")

    results = {}
    done = 0

    def fetch(repo):
        mod_name, content = fetch_go_mod(org, repo)
        if content and mod_name:
            deps = parse_openshift_deps(content)
            return repo, mod_name, deps
        return repo, None, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch, r): r for r in repos}
        for fut in as_completed(futures):
            repo, mod_name, deps = fut.result()
            done += 1
            if mod_name:
                results[repo] = {"module": mod_name, "openshift_deps": deps}
                dep_count = len(deps) if deps else 0
                print(f"  [{done}/{len(repos)}] {repo}: {mod_name} ({dep_count} openshift deps)")
            else:
                print(f"  [{done}/{len(repos)}] {repo}: no go.mod")

    output = CACHE_DIR / "deps.json"
    output.write_text(json.dumps(results, indent=2))
    print(f"\nSaved dependency data to {output}")
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch OpenShift repo go.mod files")
    parser.add_argument("--org", default="openshift", help="GitHub org")
    parser.add_argument("--workers", type=int, default=10, help="Parallel fetch workers")
    parser.add_argument("--refresh-repos", action="store_true", help="Re-fetch repo list")
    args = parser.parse_args()

    if args.refresh_repos:
        (CACHE_DIR / "repos.json").unlink(missing_ok=True)

    fetch_all(args.org, args.workers)
