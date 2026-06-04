# OpenShift Dependency Tree

Maps Go module dependencies, openshift/api usage, and repo metadata across ~250 OpenShift repositories. Answers questions like "which repos need to change for feature X?" using keyword-scored impact analysis.

## Prerequisites

- Python 3.10+
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated with access to the `openshift` org
- `mcp` Python package (for the MCP server only): `pip install mcp`

## Pipeline

Run the scripts in order. Each step caches its output so re-runs are fast.

```bash
# 1. Fetch go.mod files from all openshift/* repos → .cache/deps.json
python fetch_deps.py

# 2. Build the dependency graph from cached go.mod data
python build_graph.py --json > graph.json

# 3. Map which openshift/api packages and CRD kinds each repo uses
python analyze_api_usage.py --json > api_usage.json

# 4. Fetch GitHub metadata and auto-classify repos by platform/category
python fetch_repo_metadata.py

# 5. Score repos by relevance to a feature description
python feature_impact.py --feature "storage encryption at rest"
```

## Scripts

### fetch_deps.py

Fetches `go.mod` files from every `openshift/*` repo via the GitHub API and caches them under `.cache/`. This populates the raw dependency data that all other scripts build on.

### build_graph.py

Builds the inter-repo dependency graph from cached `go.mod` data. Supports multiple output formats.

```bash
python build_graph.py                          # summary stats
python build_graph.py --dot                    # DOT output for Graphviz
python build_graph.py --focus api client-go    # who depends on these repos?
python build_graph.py --json > graph.json      # JSON for other tools
```

### analyze_api_usage.py

Maps which `openshift/api` packages (API groups/versions) and CRD kinds each repo imports. Uses GitHub code search with caching.

```bash
python analyze_api_usage.py                    # build cache and print summary
python analyze_api_usage.py --repo cluster-etcd-operator
python analyze_api_usage.py --package config/v1
python analyze_api_usage.py --top-packages     # most-imported packages
python analyze_api_usage.py --json > api_usage.json
```

### fetch_repo_metadata.py

Fetches GitHub metadata (description, topics, fork status, stars) for every repo and auto-classifies them by platform and category using keyword rules.

**Platforms:** aws, azure, gcp, vsphere, baremetal, openstack, ibm, nutanix, ovirt, kubevirt

**Classifications:** storage, networking, monitoring, security, installation, machine-management, cloud-compute, operator-framework, image-registry, update, scaling, console, etcd, scheduling, build, ci-testing, backup-restore, node, managed-services, ai-ml, cluster-lifecycle, credentials, hardware, edge, cli, policy, api-sdk

```bash
python fetch_repo_metadata.py                 # fetch all repos
python fetch_repo_metadata.py --refresh       # bust cache and re-fetch
python fetch_repo_metadata.py --repo installer # inspect one repo
```

Supports a `repo_metadata_overrides.json` file for manual corrections to auto-classification.

### feature_impact.py

Scores repos by relevance to a feature description. Combines keyword matching across repo names, descriptions, topics, dependency graphs, and API package usage.

```bash
python feature_impact.py --feature "storage encryption at rest"
python feature_impact.py --feature "dual stack IPv6" --platform aws azure
python feature_impact.py --feature "etcd backup" --classification etcd --top 20
python feature_impact.py --feature "ingress TLS" --json
python feature_impact.py --feature "NVMe support" --output impact.json --min-score 20
```

### view.html

Browser-based visualization. Open directly in a browser — no server required. Loads `graph.json`, `api_usage.json`, and `repo_metadata.json` to display an interactive dependency map with repo detail panels, platform/classification chips, and a Feature Impact tab.

## MCP Server

`mcp_server.py` exposes the analysis as a [Model Context Protocol](https://modelcontextprotocol.io/) server, allowing LLMs to query the dependency tree directly.

### Tools

| Tool | Description |
|------|-------------|
| `feature_impact_tool` | Score repos by relevance to a feature description |
| `get_repo_info` | Full detail for one repo (metadata, deps, API usage) |
| `list_repos` | List repos with platform/classification/fork filters |
| `get_repo_dependencies` | Forward and reverse dependency relationships |
| `get_repo_api_usage` | openshift/api packages and CRD kinds used by a repo |
| `search_repos` | Substring search across repo names, descriptions, topics |

### Resources

| Resource | Description |
|----------|-------------|
| `openshift://filters` | Valid platform and classification filter values |
| `openshift://data-freshness` | Data file existence, freshness, and record counts |

### Setup

Register with Claude Code:

```bash
claude mcp add openshift-dep-tree python3 /path/to/mcp_server.py
```

Or add to `.claude/settings.local.json`:

```json
{
  "mcpServers": {
    "openshift-dep-tree": {
      "command": "python3",
      "args": ["/path/to/mcp_server.py"]
    }
  }
}
```

### Container

Build and run the MCP server in a UBI 9 container. Data files are mounted at runtime so you can refresh them without rebuilding.

```bash
# Build
podman build -t openshift-dep-tree -f Containerfile .

# Run (mount project root as the data directory)
podman run -i --rm -v "$(pwd):/opt/app-root/src/data:Z" openshift-dep-tree
```

Register the container with Claude Code:

```bash
claude mcp add openshift-dep-tree \
  podman run -i --rm -v /path/to/openshift-dep-tree:/opt/app-root/src/data:Z openshift-dep-tree
```

The `MCP_DATA_DIR` environment variable controls where the server looks for data files (`graph.json`, `api_usage.json`, `repo_metadata.json`, `.cache/`). It defaults to the script directory when running outside a container.

### Testing

Use the [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) for interactive testing:

```bash
npx @modelcontextprotocol/inspector python3 mcp_server.py
```

## Data Files

| File | Generated By | Description |
|------|-------------|-------------|
| `.cache/deps.json` | `fetch_deps.py` | Raw go.mod dependency data |
| `graph.json` | `build_graph.py` | Inter-repo dependency graph |
| `api_usage.json` | `analyze_api_usage.py` | openshift/api package and CRD kind usage |
| `repo_metadata.json` | `fetch_repo_metadata.py` | GitHub metadata with platform/classification tags |
