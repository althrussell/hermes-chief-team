#!/usr/bin/env python3
"""Phase-1 Unity Gateway wire probe: one real tool-calling round trip per candidate route.

Mints a short-lived token with `databricks auth token --profile <profile>`, never prints or
stores it, and writes redacted evidence (status, latency, request ids, tool-call shape) as JSON.
Usage: probe_gateway.py --host https://<workspace> --profile <profile> --out <output.json> [route ...]
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

import argparse

HOST = PROFILE = ""
DATABRICKS = "databricks"
TAGS = json.dumps({"source": "hermes-chief-team", "purpose": "gateway-probe"})
CA = os.environ.get("SSL_CERT_FILE", "")

WEATHER_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}
PROMPT = "Use the get_weather tool to get the weather for Paris, then answer in one short sentence."
TOOL_RESULT = {"city": "Paris", "temp_c": 17, "sky": "cloudy"}


def mint_token() -> str:
    out = subprocess.run(
        [DATABRICKS, "auth", "token", "--profile", PROFILE, "-o", "json"],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    return json.loads(out)["access_token"]


def ssl_ctx():
    return ssl.create_default_context(cafile=CA) if CA and os.path.exists(CA) else ssl.create_default_context()


def post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Databricks-Ai-Gateway-Request-Tags": TAGS, **headers})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180, context=ssl_ctx()) as resp:
            raw, status, rh = resp.read(), resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw, status, rh = exc.read(), exc.code, dict(exc.headers)
    ids = {k: v for k, v in rh.items() if "request-id" in k.lower() or k.lower().startswith("x-databricks")}
    try:
        data = json.loads(raw)
    except ValueError:
        data = {"_non_json": raw[:400].decode("utf-8", "replace")}
    return status, round(time.time() - t0, 2), ids, data


def responses_route(base, model):
    def run(auth):
        tools = [{"type": "function", "name": "get_weather", "description": "Current weather for a city.",
                  "parameters": WEATHER_SCHEMA}]
        inp = [{"role": "user", "content": PROMPT}]
        s1, l1, i1, d1 = post(f"{base}/responses", {"model": model, "input": inp, "tools": tools,
                                                    "store": False, "max_output_tokens": 2048}, auth)
        calls = [o for o in d1.get("output", []) if isinstance(o, dict) and o.get("type") == "function_call"]
        step1 = {"status": s1, "latency_s": l1, "request_ids": i1, "model": d1.get("model"),
                 "output_types": [o.get("type") for o in d1.get("output", []) if isinstance(o, dict)],
                 "tool_call": calls[0].get("name") if calls else None, "error": d1.get("error")}
        if not calls:
            return {"step1": step1, "round_trip": False}
        inp2 = inp + d1["output"] + [{"type": "function_call_output", "call_id": calls[0]["call_id"],
                                      "output": json.dumps(TOOL_RESULT)}]
        s2, l2, i2, d2 = post(f"{base}/responses", {"model": model, "input": inp2, "tools": tools,
                                                    "store": False, "max_output_tokens": 2048}, auth)
        text = "".join(c.get("text", "") for o in d2.get("output", []) if isinstance(o, dict) and o.get("type") == "message"
                       for c in o.get("content", []) if isinstance(c, dict))
        step2 = {"status": s2, "latency_s": l2, "request_ids": i2, "final_text": text[:200], "error": d2.get("error")}
        return {"step1": step1, "step2": step2, "round_trip": s2 == 200 and "17" in text}
    return run


def anthropic_route(base, model):
    def run(auth):
        tools = [{"name": "get_weather", "description": "Current weather for a city.", "input_schema": WEATHER_SCHEMA}]
        msgs = [{"role": "user", "content": PROMPT}]
        hdr = {**auth, "anthropic-version": "2023-06-01"}
        s1, l1, i1, d1 = post(f"{base}/v1/messages", {"model": model, "max_tokens": 1024, "messages": msgs, "tools": tools}, hdr)
        uses = [b for b in d1.get("content", []) if isinstance(b, dict) and b.get("type") == "tool_use"]
        step1 = {"status": s1, "latency_s": l1, "request_ids": i1, "model": d1.get("model"),
                 "content_types": [b.get("type") for b in d1.get("content", []) if isinstance(b, dict)],
                 "tool_call": uses[0].get("name") if uses else None, "error": d1.get("error")}
        if not uses:
            return {"step1": step1, "round_trip": False}
        msgs2 = msgs + [{"role": "assistant", "content": d1["content"]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": uses[0]["id"],
                                                      "content": json.dumps(TOOL_RESULT)}]}]
        s2, l2, i2, d2 = post(f"{base}/v1/messages", {"model": model, "max_tokens": 1024, "messages": msgs2, "tools": tools}, hdr)
        text = "".join(b.get("text", "") for b in d2.get("content", []) if isinstance(b, dict) and b.get("type") == "text")
        step2 = {"status": s2, "latency_s": l2, "request_ids": i2, "final_text": text[:200], "error": d2.get("error")}
        return {"step1": step1, "step2": step2, "round_trip": s2 == 200 and "17" in text}
    return run


def gemini_route(base, model):
    def run(auth):
        tools = [{"functionDeclarations": [{"name": "get_weather", "description": "Current weather for a city.",
                                            "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                                           "required": ["city"]}}]}]
        contents = [{"role": "user", "parts": [{"text": PROMPT}]}]
        url = f"{base}/models/{model}:generateContent"
        s1, l1, i1, d1 = post(url, {"contents": contents, "tools": tools}, auth)
        parts = (((d1.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        calls = [p for p in parts if isinstance(p, dict) and "functionCall" in p]
        step1 = {"status": s1, "latency_s": l1, "request_ids": i1, "model": d1.get("modelVersion"),
                 "part_keys": [sorted(p.keys()) for p in parts if isinstance(p, dict)],
                 "tool_call": calls[0]["functionCall"].get("name") if calls else None,
                 "thought_signature_present": any("thoughtSignature" in p for p in parts if isinstance(p, dict)),
                 "error": d1.get("error")}
        if not calls:
            return {"step1": step1, "round_trip": False}
        contents2 = contents + [{"role": "model", "parts": parts},
                                {"role": "user", "parts": [{"functionResponse": {"name": "get_weather",
                                                                                 "response": TOOL_RESULT}}]}]
        s2, l2, i2, d2 = post(url, {"contents": contents2, "tools": tools}, auth)
        parts2 = (((d2.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        text = "".join(p.get("text", "") for p in parts2 if isinstance(p, dict) and not p.get("thought"))
        step2 = {"status": s2, "latency_s": l2, "request_ids": i2, "final_text": text[:200], "error": d2.get("error")}
        return {"step1": step1, "step2": step2, "round_trip": s2 == 200 and "17" in text}
    return run


def routes(host, models):
    return {
        "chief-openai-native": (responses_route(f"{host}/ai-gateway/openai/v1", models["chief"]), "bearer"),
        "chief-mlflow-unified": (responses_route(f"{host}/ai-gateway/mlflow/v1", models["chief"]), "bearer"),
        "claude-anthropic-native": (anthropic_route(f"{host}/ai-gateway/anthropic", models["claude"]), "bearer"),
        "gemini-native-bearer": (gemini_route(f"{host}/ai-gateway/gemini/v1beta", models["gemini"]), "bearer"),
        "gemini-native-goog-key": (gemini_route(f"{host}/ai-gateway/gemini/v1beta", models["gemini"]), "goog"),
    }


def main():
    global HOST, PROFILE, DATABRICKS
    ap = argparse.ArgumentParser(description="Unity Gateway tool-calling round trip per route")
    ap.add_argument("--host", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--databricks-bin", default="databricks")
    ap.add_argument("--chief-model", default="system.ai.gpt-6-1-sol")
    ap.add_argument("--claude-model", default="system.ai.claude-opus-5-5")
    ap.add_argument("--gemini-model", default="system.ai.gemini-4-argon")
    ap.add_argument("--out", required=True)
    ap.add_argument("route", nargs="*")
    args = ap.parse_args()
    HOST, PROFILE, DATABRICKS = args.host.rstrip("/"), args.profile, args.databricks_bin
    table = routes(HOST, {"chief": args.chief_model, "claude": args.claude_model, "gemini": args.gemini_model})
    wanted = args.route or list(table)
    token = mint_token()
    results = {"host": HOST, "profile": PROFILE, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "routes": {}}
    for name in wanted:
        fn, style = table[name]
        auth = {"Authorization": f"Bearer {token}"} if style == "bearer" else {"x-goog-api-key": token}
        try:
            results["routes"][name] = fn(auth)
        except Exception as exc:  # evidence must survive one route failing
            results["routes"][name] = {"exception": f"{type(exc).__name__}: {exc}"[:300]}
        print(f"{name}: round_trip={results['routes'][name].get('round_trip')}", flush=True)
    serialized = json.dumps(results, indent=2)
    if token in serialized:
        sys.exit("refusing to write evidence containing the bearer token")
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(serialized)


if __name__ == "__main__":
    main()
