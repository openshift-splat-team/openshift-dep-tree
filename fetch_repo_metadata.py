#!/usr/bin/env python3
"""
Fetch GitHub metadata for all OpenShift repos and auto-classify them.
Writes .cache/repo_metadata.json (cache) and repo_metadata.json (root, for browser).

Usage:
  python fetch_repo_metadata.py              # fetch all
  python fetch_repo_metadata.py --refresh    # bust cache and re-fetch
  python fetch_repo_metadata.py --repo foo   # show one repo (no write)
  python fetch_repo_metadata.py --workers 5  # parallel fetch (default 5)
  python fetch_repo_metadata.py --overrides path/to/overrides.json
"""

import json
import subprocess
import sys
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
META_CACHE = CACHE_DIR / "repo_metadata.json"
META_ROOT = Path("repo_metadata.json")
DEPS_CACHE = CACHE_DIR / "deps.json"
ORG = "openshift"

PLATFORM_RULES = {
    "aws":       ["aws", "amazon", "ebs", "efs", "ec2"],
    "azure":     ["azure", "aks", "aad"],
    "gcp":       ["gcp", "google", "gke", "filestore"],
    "vsphere":   ["vsphere", "vmware", "vcenter"],
    "baremetal": ["baremetal", "bare-metal", "ironic", "bmc"],
    "openstack": ["openstack", "cinder", "nova"],
    "ibm":       ["ibm", "powervs"],
    "nutanix":   ["nutanix"],
    "ovirt":     ["ovirt", "rhv"],
    "kubevirt":  ["kubevirt", "cnv"],
}

CLASSIFICATION_RULES = {
    "storage":            ["storage", "csi", "volume", "disk", "nfs", "cinder"],
    "networking":         ["network", "cni", "dns", "ingress", "ovn", "sdn", "gateway", "multus", "frr", "routing"],
    "monitoring":         ["monitoring", "metrics", "prometheus", "telemetry", "logging", "alertmanager", "insights", "health-analyzer", "observability", "elasticsearch", "splunk", "problem-detector", "debug-tools"],
    "security":           ["security", "oauth", "cert", "tls", "rbac", "compliance", "encryption", "file-integrity", "zero-trust", "spire", "spiffe", "external-secrets"],
    "installation":       ["installer", "install", "bootstrap", "assisted", "agent-installer"],
    "machine-management": ["machine-api", "machine-config", "machineconfig", "machine-set"],
    "cloud-compute":      ["autoscal", "cluster-api", "machine-api-provider", "infra"],
    "operator-framework": ["olm", "csv", "operatorhub", "bundle", "catalogsource", "operator-framework", "operator-sdk"],
    "image-registry":     ["image-registry", "imagestream", "mirror"],
    "update":             ["cvo", "cluster-version", "upgrade", "update"],
    "scaling":            ["autoscal", "hpa", "vpa", "keda"],
    "console":            ["console", "web-console"],
    "etcd":               ["etcd"],
    "scheduling":         ["scheduler", "topology", "descheduler"],
    "build":              ["build", "s2i", "source-to-image"],
    "ci-testing":         ["ci-", "e2e", "conformance", "sippy", "prow", "osde2e", "test-platform", "openstack-test"],
    "backup-restore":     ["backup", "restore", "velero", "oadp", "disaster-recovery"],
    "node":               ["node-feature", "node-tuning", "tuned", "nfd", "node-observability"],
    "managed-services":   ["backplane", "ocm", "osd", "rosa", "addon", "managed-cluster", "must-gather", "pagerduty", "dedicated", "roks"],
    "ai-ml":              ["lightspeed", "analytics", "kueue", "instaslice", "lws-operator", "accelerat", "jobset"],
    "cluster-lifecycle":  ["hive", "hypershift", "capi", "release-controller", "provision", "cluster-config", "cluster-cloud-controller", "cloud-provider", "cluster-machine-approver", "karpenter", "migration"],
    "credentials":        ["credential", "cloud-credential", "account-operator"],
    "hardware":           ["sriov", "ptp", "linuxptp", "sandboxed", "special-resource", "gpu"],
    "edge":               ["microshift", "edge"],
    "cli":                ["oc ", "cli-manager", "osdctl", "backplane-cli"],
    "policy":             ["gatekeeper", "policy", "admission", "override"],
    "api-sdk":            ["library-go", "apiserver-library", "generic-admission", "openshift-api", "openshift-controller-manager", "controller-runtime", "crd-schema", "custom-resource-status", "kube-compare", "api definition"],
}

PLATFORM_SDK_MODULES = {
    "aws":       ["github.com/aws/aws-sdk-go"],
    "azure":     ["github.com/Azure/azure-sdk-for-go"],
    "gcp":       ["cloud.google.com/go", "google.golang.org/api"],
    "vsphere":   ["github.com/vmware/govmomi"],
    "openstack": ["github.com/gophercloud/gophercloud"],
    "ibm":       ["github.com/IBM/vpc-go-sdk", "github.com/IBM/platform-services-go-sdk"],
    "nutanix":   ["github.com/nutanix-cloud-native/prism-go-client"],
    "ovirt":     ["github.com/ovirt/go-ovirt"],
    "kubevirt":  ["kubevirt.io/api", "kubevirt.io/containerized-data-importer-api"],
    "baremetal": ["github.com/metal3-io/baremetal-operator"],
}


def gh(endpoint, accept=None):
    cmd = ["gh", "api", endpoint]
    if accept:
        cmd += ["-H", f"Accept: {accept}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def classify_by_gomod(repo):
    """Detect platforms from SDK Go modules in the cached go.mod file."""
    gomod_path = CACHE_DIR / f"{repo}.gomod"
    if not gomod_path.exists():
        return []
    try:
        content = gomod_path.read_text()
    except OSError:
        return []
    if content == "NONE":
        return []

    platforms = set()
    in_require = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("require ("):
            in_require = True
            continue
        if in_require and stripped == ")":
            in_require = False
            continue
        if "// indirect" in stripped:
            continue
        if in_require or stripped.startswith("require "):
            for platform, prefixes in PLATFORM_SDK_MODULES.items():
                if any(prefix in stripped for prefix in prefixes):
                    platforms.add(platform)
    return sorted(platforms)


def classify(repo_name, description, topics, gomod_platforms=None):
    """Auto-classify a repo into platforms and categories based on keyword rules and SDK modules."""
    haystack = " ".join([repo_name, description or "", " ".join(topics or [])]).lower()

    platforms = set(p for p, kws in PLATFORM_RULES.items() if any(kw in haystack for kw in kws))
    if gomod_platforms:
        platforms.update(gomod_platforms)
    classifications = [c for c, kws in CLASSIFICATION_RULES.items() if any(kw in haystack for kw in kws)]

    return sorted(platforms), classifications


def first_sentence(text):
    """Extract the first sentence from a description."""
    if not text:
        return ""
    for sep in [". ", "! ", "? ", "\n"]:
        idx = text.find(sep)
        if idx != -1:
            return text[:idx + 1].strip()
    return text.strip()


def fetch_repo_meta(org, repo):
    """Fetch GitHub metadata for a single repo. Returns dict or None on error."""
    data = gh(
        f"repos/{org}/{repo}",
        accept="application/vnd.github.mercy-preview+json",
    )
    if not data:
        return None

    description = data.get("description") or ""
    topics = data.get("topics") or []
    fork = bool(data.get("fork", False))
    upstream = None
    if fork and data.get("parent"):
        upstream = data["parent"].get("full_name")
    stars = data.get("stargazers_count", 0)

    gomod_platforms = classify_by_gomod(repo)
    platforms, classifications = classify(repo, description, topics, gomod_platforms)

    return {
        "description": description,
        "topics": topics,
        "fork": fork,
        "upstream": upstream,
        "platforms": platforms,
        "classifications": classifications,
        "summary": first_sentence(description),
        "stars": stars,
        "source": "github",
    }


def load_repos():
    """Load repo list from deps.json cache."""
    if not DEPS_CACHE.exists():
        print(f"ERROR: {DEPS_CACHE} not found. Run fetch_deps.py first.", file=sys.stderr)
        sys.exit(1)
    deps = json.loads(DEPS_CACHE.read_text())
    return sorted(deps.keys())


def load_overrides(overrides_path):
    """Load manual override file. Returns empty dict if file doesn't exist."""
    p = Path(overrides_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: could not read overrides file {p}: {e}", file=sys.stderr)
        return {}


def apply_overrides(repos_meta, overrides):
    """Merge overrides into metadata. Override fields replace auto-detected counterparts."""
    for repo, override_fields in overrides.items():
        if repo not in repos_meta:
            repos_meta[repo] = {
                "description": "", "topics": [], "fork": False, "upstream": None,
                "platforms": [], "classifications": [], "summary": "", "stars": 0,
                "source": "override",
            }
        for field, value in override_fields.items():
            repos_meta[repo][field] = value
        repos_meta[repo]["source"] = "override"
    return repos_meta


def fetch_all(repos, workers=5):
    """Fetch metadata for all repos in parallel. Returns {repo: meta_dict}."""
    results = {}
    done = 0
    total = len(repos)

    def fetch_one(repo):
        time.sleep(0.1)  # mild rate limiting per worker
        return repo, fetch_repo_meta(ORG, repo)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_one, r): r for r in repos}
        for fut in as_completed(futures):
            repo, meta = fut.result()
            done += 1
            if meta:
                results[repo] = meta
                plat_str = ",".join(meta["platforms"]) or "—"
                cls_str = ",".join(meta["classifications"]) or "—"
                print(f"  [{done}/{total}] {repo}: platforms=[{plat_str}] cls=[{cls_str}]")
            else:
                print(f"  [{done}/{total}] {repo}: fetch failed")

    return results


def write_output(repos_meta, fetched_at):
    data = {"fetched_at": fetched_at, "repos": repos_meta}
    META_CACHE.write_text(json.dumps(data, indent=2))
    META_ROOT.write_text(json.dumps(data, indent=2))
    print(f"\nWrote {len(repos_meta)} repos to {META_CACHE} and {META_ROOT}")


def main():
    parser = argparse.ArgumentParser(description="Fetch GitHub repo metadata and auto-classify")
    parser.add_argument("--refresh", action="store_true", help="Delete cache and re-fetch")
    parser.add_argument("--repo", help="Show metadata for a single repo (no write)")
    parser.add_argument("--workers", type=int, default=5, help="Parallel fetch workers (default 5)")
    parser.add_argument("--overrides", default="repo_metadata_overrides.json",
                        help="Path to manual overrides JSON (default: repo_metadata_overrides.json)")
    args = parser.parse_args()

    if args.repo:
        meta = fetch_repo_meta(ORG, args.repo)
        if meta:
            print(json.dumps({args.repo: meta}, indent=2))
        else:
            print(f"Failed to fetch metadata for {args.repo}", file=sys.stderr)
        return

    if args.refresh and META_CACHE.exists():
        META_CACHE.unlink()
        print(f"Deleted cache {META_CACHE}")

    repos = load_repos()
    print(f"Fetching metadata for {len(repos)} repos (workers={args.workers})...")

    repos_meta = fetch_all(repos, args.workers)

    overrides = load_overrides(args.overrides)
    if overrides:
        print(f"\nApplying {len(overrides)} overrides from {args.overrides}")
        repos_meta = apply_overrides(repos_meta, overrides)

    fetched_at = datetime.now(timezone.utc).isoformat()
    write_output(repos_meta, fetched_at)


if __name__ == "__main__":
    main()
