#!/usr/bin/env python3
"""Probe Unity Gateway MCP services: MCP initialize + tools/list, optionally one read-only tool call.

The bearer comes from `databricks auth token --profile <profile>`. It is never printed or written.
Evidence is written as JSON (mode 0600). Example:

  probe_mcp.py --host https://<workspace> --profile <profile> --schema system.ai --out mcp.json
  probe_mcp.py ... --call github=get_me --call slack=slack_read_user_profile
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"


def mint_token(databricks_bin, profile):
    out = subprocess.run([databricks_bin, "auth", "token", "--profile", profile, "-o", "json"],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    return json.loads(out)["access_token"]


def list_services(databricks_bin, profile, schema):
    out = subprocess.run([databricks_bin, "ai-gateway", "list-mcp-services", "--parent", f"schemas/{schema}",
                          "--profile", profile, "-o", "json"], capture_output=True, text=True, timeout=90,
                         check=True).stdout
    data = json.loads(out)
    items = data if isinstance(data, list) else (data.get("mcp_services") or [])
    return [i["name"].split("/", 1)[1] for i in items]


def _parse_body(raw, content_type):
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in (content_type or ""):
        messages = [json.loads(line[5:].strip()) for line in text.splitlines()
                    if line.startswith("data:") and line[5:].strip()]
        return messages[-1] if messages else {}
    return json.loads(text) if text.strip() else {}


class Session:
    def __init__(self, url, token, tags):
        self.url, self.token, self.tags, self.session_id, self.next_id = url, token, tags, None, 1

    def send(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = self.next_id
            self.next_id += 1
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self.token}", "MCP-Protocol-Version": PROTOCOL_VERSION,
                   "Databricks-Ai-Gateway-Request-Tags": self.tags}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                status, ctype, raw = resp.status, resp.headers.get("Content-Type"), resp.read()
                self.session_id = resp.headers.get("Mcp-Session-Id") or self.session_id
        except urllib.error.HTTPError as exc:
            return exc.code, {"http_error": exc.read()[:400].decode("utf-8", "replace")}
        if notify:
            return status, {}
        try:
            return status, _parse_body(raw, ctype)
        except ValueError:
            return status, {"non_json": raw[:300].decode("utf-8", "replace")}


def probe(host, token, service, tags, call=None):
    url = f"{host}/ai-gateway/mcp-services/{service}"
    s = Session(url, token, tags)
    result = {"service": service, "url": url}
    t0 = time.time()
    status, init = s.send("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                         "clientInfo": {"name": "hermes-chief-team-probe", "version": "1"}})
    result["initialize"] = {"status": status, "server": (init.get("result") or {}).get("serverInfo"),
                            "error": init.get("error") or init.get("http_error")}
    if status != 200 or "result" not in init:
        result["ok"] = False
        return result
    s.send("notifications/initialized", notify=True)
    tools, cursor = [], None
    for _ in range(20):
        status, page = s.send("tools/list", {"cursor": cursor} if cursor else {})
        res = page.get("result") or {}
        if status != 200 or "result" not in page:
            result["tools_list_error"] = {"status": status, "error": page.get("error") or page.get("http_error")}
            break
        tools += res.get("tools", [])
        cursor = res.get("nextCursor")
        if not cursor:
            break
    result["tool_count"] = len(tools)
    result["tools"] = [{"name": t.get("name"), "required": (t.get("inputSchema") or {}).get("required", []),
                        "read_only_hint": (t.get("annotations") or {}).get("readOnlyHint")} for t in tools]
    if call:
        name, _, raw_args = call.partition(":")
        status, resp = s.send("tools/call", {"name": name, "arguments": json.loads(raw_args) if raw_args else {}})
        res = resp.get("result") or {}
        text = "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
        result["call"] = {"tool": name, "status": status, "is_error": res.get("isError"),
                          "error": resp.get("error") or resp.get("http_error"), "text_preview": text[:300]}
    result["latency_s"] = round(time.time() - t0, 2)
    result["ok"] = "tools_list_error" not in result and (not call or (result["call"]["status"] == 200
                                                                         and not result["call"]["is_error"]
                                                                         and not result["call"]["error"]))
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--schema", default="system.ai")
    ap.add_argument("--databricks-bin", default="databricks")
    ap.add_argument("--service", action="append", default=[], help="Limit to these services (full names)")
    ap.add_argument("--call", action="append", default=[], help="service_short=tool[:json-args] for one read-only call")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    host = args.host.rstrip("/")
    calls = dict(c.split("=", 1) for c in args.call)
    services = args.service or list_services(args.databricks_bin, args.profile, args.schema)
    token = mint_token(args.databricks_bin, args.profile)
    tags = json.dumps({"source": "hermes-chief-team", "purpose": "mcp-probe"})
    report = {"host": host, "profile": args.profile, "schema": args.schema,
              "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "services": []}
    for svc in services:
        try:
            r = probe(host, token, svc, tags, calls.get(svc.rsplit(".", 1)[-1]))
        except Exception as exc:  # one service failing must not hide the others
            r = {"service": svc, "ok": False, "exception": f"{type(exc).__name__}: {exc}"[:300]}
        report["services"].append(r)
        print(f"{svc}: ok={r.get('ok')} tools={r.get('tool_count')}", flush=True)
    serialized = json.dumps(report, indent=2)
    if token in serialized:
        sys.exit("refusing to write evidence containing the bearer token")
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(serialized)


if __name__ == "__main__":
    main()
