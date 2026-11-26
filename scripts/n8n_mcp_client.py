#!/usr/bin/env python3
"""
n8n MCP Client (Sanitized CLI Helper)
Connects via Streamable HTTP JSON-RPC 2.0 to inspect, search, and run n8n workflows.

Requires environment variables:
  N8N_MCP_URL       e.g. https://n8n.yourdomain.com/mcp-server/http
  N8N_BEARER_TOKEN  Bearer JWT generated in n8n MCP settings
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def get_config() -> tuple[str, str]:
    """Load and validate required MCP endpoint configuration from environment."""
    url = os.environ.get("N8N_MCP_URL", "").strip()
    token = os.environ.get("N8N_BEARER_TOKEN", "").strip()
    if not url or not token:
        print(
            "Error: N8N_MCP_URL and N8N_BEARER_TOKEN environment variables must be set.",
            file=sys.stderr,
        )
        sys.exit(1)
    return url, token


def call_tool(tool_name: str, arguments: dict | None = None) -> dict:
    """Invoke an n8n MCP tool over JSON-RPC 2.0."""
    url, token = get_config()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments or {},
        },
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "HomeLab-n8n-MCP/1.0",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            for line in content.splitlines():
                if line.startswith("data:"):
                    res_json = json.loads(line[5:].strip())
                    if "error" in res_json:
                        return {"error": res_json["error"]}
                    return res_json.get("result", {})
    except urllib.error.HTTPError as err:
        return {"error": f"HTTP {err.code}: {err.reason}"}
    except Exception as err:
        return {"error": str(err)}
    return {"error": "No response data received from n8n MCP server"}


def main() -> None:
    parser = argparse.ArgumentParser(description="n8n MCP CLI Client")
    subparsers = parser.add_subparsers(dest="command")

    sub_search = subparsers.add_parser("search", help="Search workflows")
    sub_search.add_argument("--query", "-q", default=None, help="Filter string")
    sub_search.add_argument("--limit", "-l", type=int, default=20, help="Max results")

    sub_get = subparsers.add_parser("get", help="Get workflow details")
    sub_get.add_argument("workflow_id", help="Workflow ID")

    sub_run = subparsers.add_parser("run", help="Execute a workflow")
    sub_run.add_argument("workflow_id", help="Workflow ID")
    sub_run.add_argument("--chat", default=None, help="Chat input text")
    sub_run.add_argument("--json-input", default=None, help="JSON input payload")

    subparsers.add_parser("credentials", help="List configured credentials")

    sub_nodes = subparsers.add_parser("nodes", help="Search node catalog")
    sub_nodes.add_argument("query", help="Node query, e.g. 'slack' or 'webhook'")

    args = parser.parse_args()

    if args.command == "search":
        search_args: dict = {"limit": args.limit}
        if args.query:
            search_args["query"] = args.query
        result = call_tool("search_workflows", search_args)
    elif args.command == "get":
        result = call_tool("get_workflow_details", {"workflowId": args.workflow_id})
    elif args.command == "run":
        inputs = None
        if args.chat:
            inputs = {"type": "chat", "chatInput": args.chat}
        elif args.json_input:
            inputs = json.loads(args.json_input)
        run_args: dict = {"workflowId": args.workflow_id}
        if inputs is not None:
            run_args["inputs"] = inputs
        result = call_tool("execute_workflow", run_args)
    elif args.command == "credentials":
        result = call_tool("list_credentials", {})
    elif args.command == "nodes":
        result = call_tool("search_nodes", {"queries": [args.query]})
    else:
        result = call_tool("search_workflows", {"limit": 20})

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
