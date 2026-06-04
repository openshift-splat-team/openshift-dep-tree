FROM registry.access.redhat.com/ubi9/python-311:latest

COPY mcp_server.py feature_impact.py fetch_repo_metadata.py ./

RUN pip install --no-cache-dir mcp

RUN mkdir -p .cache

VOLUME /opt/app-root/src/data

ENV MCP_DATA_DIR=/opt/app-root/src/data

CMD ["python3", "mcp_server.py"]
