"""
Traffic Generator — Publive MCP Server
=======================================
Each phase deliberately exercises a specific New Relic feature so every
dashboard panel, custom event type, and custom metric has real data.

  Phase 0  Login                  Auth/session_login transaction
  Phase 1  Auth endpoints         Auth/* transactions + auth.* attributes
  Phase 2  MCP handshake          MCP/initialize, MCP/tools_list, MCP/ping
  Phase 3  Unknown methods        MCPUnknownMethod custom events
  Phase 4  All 18 tools           every Tool/* function trace segment
  Phase 5  Prompt source variety  MCPPrompt with header / meta / arg / fallback
  Phase 6  Deliberate errors      MCPToolError events + error.* attributes
  Phase 7  Concurrent burst       mcp.thread_active_count metric pushed high
  Phase 8  SSE session            SSESessionOpen + SSESessionClose events
  Phase 9  Batch request          batch JSON-RPC dispatch path
  Phase 10 Sustained rounds       throughput for time-series charts

Usage:
    python generate_traffic.py                      # prompts for credentials
    python generate_traffic.py --rounds 3           # 3 sustained rounds
    python generate_traffic.py --concurrency 8      # threads for burst phase
    python generate_traffic.py --local              # localhost:8000
"""

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

DEFAULT_BASE        = "https://tests-production-8b04.up.railway.app"
LOCAL_BASE          = "http://127.0.0.1:8000"
DEFAULT_ROUNDS      = 3
DEFAULT_CONCURRENCY = 8
DELAY_MIN           = 0.2   # seconds between calls in sustained rounds
DELAY_MAX           = 0.6

# ── Terminal colours ───────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def ok(msg):             print(f"  {GREEN}✓ {msg}{RESET}")
def fail(msg):           print(f"  {RED}✗ {msg}{RESET}")
def warn(msg):           print(f"  {YELLOW}~ {msg}{RESET}")
def info(msg):           print(f"  {CYAN}· {msg}{RESET}")
def sub(msg):            print(f"  {DIM}  {msg}{RESET}")
def phase(n, title, nr): print(f"\n{BOLD}{BLUE}Phase {n}  —  {title}{RESET}\n  {DIM}NR: {nr}{RESET}")

# ── Simulated MCP client identities  →  mcp.client_name variety in NR ─────────

MCP_CLIENTS = [
    "claude-ai/1.0.0",
    "cursor/0.42.3",
    "vscode-mcp-extension/2.1.0",
    "mcp-inspector/1.0",
    "python-mcp-client/0.9",
]

# ── Realistic prompts  →  MCPPrompt.prompt_text variety in NR ─────────────────

PROMPTS = [
    "Show me the latest 5 articles on the site",
    "Get all posts about technology published this week",
    "What categories does this publication have?",
    "Find long-form articles with more than 500 words",
    "List all video content available",
    "Get me the site navigation structure",
    "What tags are available? I need to filter by topic",
    "Show me the publisher branding and social links",
    "Get the footer configuration for the site",
    "What newsletter groups can readers subscribe to?",
    "List all advertisement slots currently active",
    "What content types does this publication support?",
    "Get live blog updates for breaking news",
    "Find all articles in the business category",
    "Who are the authors on this site?",
    "Identify the content type at this URL path",
    "Get me posts filtered by both category and tag",
    "Show me posts from the last 30 days",
    "Get me articles authored by a specific contributor",
    "What does the contact form schema look like?",
]

# ── Thread-safe request ID counter ────────────────────────────────────────────

_req_id   = 0
_req_lock = threading.Lock()

def next_id() -> int:
    global _req_id
    with _req_lock:
        _req_id += 1
        return _req_id

# ── Low-level HTTP helpers ─────────────────────────────────────────────────────

def mcp_post(session, base_url, payload, *, prompt=None, ua=None) -> dict:
    headers = {}
    if prompt:
        headers["X-MCP-Prompt"] = str(prompt)[:500]
    if ua:
        headers["User-Agent"] = ua
    resp = session.post(f"{base_url}/mcp", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def mcp_call(session, base_url, method, params=None, *, prompt=None, ua=None) -> dict:
    payload = {"jsonrpc": "2.0", "id": next_id(), "method": method}
    if params:
        payload["params"] = params
    return mcp_post(session, base_url, payload, prompt=prompt, ua=ua)


def tool_call(session, base_url, tool_name, arguments=None, *,
              prompt=None, ua=None, prompt_via="header") -> dict:
    """
    prompt_via controls mcp.prompt_source in NR:
      "header"  →  X-MCP-Prompt HTTP header
      "meta"    →  body _meta.prompt
      "arg"     →  arguments._prompt (stripped before tool runs)
      None      →  no prompt → tool_args fallback
    """
    args = dict(arguments or {})
    payload = {
        "jsonrpc": "2.0",
        "id":      next_id(),
        "method":  "tools/call",
        "params":  {"name": tool_name, "arguments": args},
    }
    hdr_prompt = None
    if prompt:
        if prompt_via == "meta":
            payload["_meta"] = {"prompt": prompt}
        elif prompt_via == "arg":
            payload["params"]["arguments"]["_prompt"] = prompt
        else:
            hdr_prompt = prompt
    return mcp_post(session, base_url, payload, prompt=hdr_prompt, ua=ua)


def parse_result(resp) -> tuple[str, bool]:
    """Return (text, is_error) from a tools/call response."""
    content  = resp.get("result", {}).get("content", [{}])
    text     = content[0].get("text", "") if content else ""
    is_error = resp.get("result", {}).get("isError", False) or str(text).startswith("Error:")
    return str(text), is_error


# ── One tool call with output ──────────────────────────────────────────────────

def run_tool(session, base_url, tool_name, arguments=None, *,
             label="", prompt=None, ua=None, prompt_via="header") -> tuple[bool, int]:
    t0 = time.perf_counter()
    try:
        resp    = tool_call(session, base_url, tool_name, arguments,
                            prompt=prompt, ua=ua, prompt_via=prompt_via)
        ms      = round((time.perf_counter() - t0) * 1000)
        text, is_err = parse_result(resp)
        tag     = label or tool_name
        preview = text[:55].replace("\n", " ")
        if is_err:
            warn(f"[{ms:>5}ms]  {tool_name:<28}  {tag}  →  {text[:65]}")
            return False, ms
        else:
            ok(f"[{ms:>5}ms]  {tool_name:<28}  {tag}  →  {preview}…")
            return True, ms
    except Exception as exc:
        ms = round((time.perf_counter() - t0) * 1000)
        fail(f"[{ms:>5}ms]  {tool_name:<28}  {label}  →  {exc}")
        return False, ms


# ── Auth ───────────────────────────────────────────────────────────────────────

def login(base_url, publisher_id, api_key, api_secret) -> requests.Session:
    session = requests.Session()
    resp    = session.post(
        f"{base_url}/auth/login",
        json={"publisherId": publisher_id, "apiKey": api_key, "apiSecret": api_secret},
        timeout=15,
    )
    if not resp.ok or not resp.json().get("success"):
        raise RuntimeError(f"Login failed ({resp.status_code}): {resp.text[:200]}")
    return session


# ── ID discovery ───────────────────────────────────────────────────────────────

def discover_ids(session, base_url) -> tuple[list, list, list, list]:
    post_ids = category_ids = tag_ids = author_ids = []
    for name, tool, key in [
        ("post",     "list_posts",      "id"),
        ("category", "list_categories", "id"),
        ("tag",      "list_tags",       "id"),
        ("author",   "list_authors",    "id"),
    ]:
        try:
            resp = tool_call(session, base_url, tool, {"limit": 5}, prompt=None)
            text, _ = parse_result(resp)
            data = json.loads(text) if isinstance(text, str) else {}
            ids  = [item[key] for item in (data.get("data") or [])[:5]
                    if isinstance(item, dict) and key in item]
            info(f"Discovered {len(ids)} {name} IDs: {ids[:3]}")
            if name == "post":     post_ids     = ids
            if name == "category": category_ids = ids
            if name == "tag":      tag_ids      = ids
            if name == "author":   author_ids   = ids
        except Exception as e:
            warn(f"Could not discover {name} IDs: {e}")
    return post_ids, category_ids, tag_ids, author_ids


# ── SSE session ────────────────────────────────────────────────────────────────

def run_sse_session(base_url, publisher_id, api_key, api_secret):
    """
    Open the legacy SSE transport, send 3 tool calls through it, then close.
    Server-side: fires SSESessionOpen on connect, SSESessionClose on disconnect.
    """
    sse = requests.Session()
    try:
        resp = sse.post(
            f"{base_url}/auth/login",
            json={"publisherId": publisher_id, "apiKey": api_key, "apiSecret": api_secret},
            timeout=10,
        )
        if not resp.ok:
            warn("SSE login failed — skipping SSE phase")
            return
    except Exception as e:
        warn(f"SSE login error: {e}")
        return

    endpoint_url   = None
    endpoint_ready = threading.Event()
    stop_reading   = threading.Event()

    def stream_reader():
        nonlocal endpoint_url
        try:
            stream = sse.get(f"{base_url}/mcp", stream=True, timeout=60)
            for raw in stream.iter_lines(decode_unicode=True):
                if stop_reading.is_set():
                    break
                if raw.startswith("data:"):
                    data = raw[5:].strip()
                    if data.startswith("http"):
                        endpoint_url = data
                        endpoint_ready.set()
        except Exception:
            pass

    t = threading.Thread(target=stream_reader, daemon=True)
    t.start()

    if not endpoint_ready.wait(timeout=12):
        warn("SSE endpoint URL not received within 12s — skipping")
        stop_reading.set()
        return

    ok(f"SSE endpoint ready: {endpoint_url}")

    for tool_name in ["get_navbar", "get_footer", "list_categories"]:
        try:
            payload = {
                "jsonrpc": "2.0",
                "id":      next_id(),
                "method":  "tools/call",
                "params":  {"name": tool_name, "arguments": {}},
            }
            r = sse.post(endpoint_url, json=payload,
                         headers={"X-MCP-Prompt": f"SSE transport test: {tool_name}"},
                         timeout=15)
            ok(f"SSE message sent → {tool_name}  (status {r.status_code})")
        except Exception as e:
            warn(f"SSE call {tool_name}: {e}")
        time.sleep(0.5)

    time.sleep(2)          # let the server record SSESessionOpen in NR
    stop_reading.set()
    sse.close()            # closing the session disconnects → triggers SSESessionClose
    time.sleep(1)          # give server a moment to fire the close event


# ── Main ───────────────────────────────────────────────────────────────────────

def run(base_url, publisher_id, api_key, api_secret, rounds, concurrency):
    start  = datetime.now()
    total  = 0
    passed = 0
    failed = 0
    stats: dict[str, list[int]] = {}   # tool → [latency_ms, ...]

    def record(tool_name, success, ms):
        nonlocal total, passed, failed
        total  += 1
        if success: passed += 1
        else:       failed += 1
        stats.setdefault(tool_name, []).append(ms)

    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  Publive MCP  ·  New Relic Traffic Generator{RESET}")
    print(f"{'═'*60}")
    print(f"  Target      : {base_url}")
    print(f"  Publisher   : {publisher_id}")
    print(f"  Rounds      : {rounds}  (Phase 10)")
    print(f"  Concurrency : {concurrency} threads  (Phase 7)")
    print(f"  Started     : {start.strftime('%H:%M:%S')}")

    # ── Phase 0: Login ─────────────────────────────────────────────────────────
    phase(0, "Login",
          "Auth/session_login transaction · auth.flow=session · auth.result=success/failure")
    try:
        session = login(base_url, publisher_id, api_key, api_secret)
        ok("Authenticated  →  session cookie stored")
    except RuntimeError as e:
        fail(str(e)); sys.exit(1)

    # ── Phase 1: Auth endpoints ────────────────────────────────────────────────
    phase(1, "Auth endpoints",
          "Auth/session_verify (Apdex suppressed) · Auth/oauth_register · auth.cds_validation_ms")

    try:
        r = session.get(f"{base_url}/auth/status", timeout=10)
        ok(f"GET /auth/status  →  Auth/session_verify  authenticated={r.json().get('authenticated')}")
    except Exception as e:
        warn(f"auth/status: {e}")

    try:
        r = session.post(f"{base_url}/register",
                         json={"redirect_uris": ["https://example.com/callback",
                                                  "https://app.example.com/oauth"]},
                         timeout=10)
        cid = r.json().get("client_id", "?")[:20] if r.ok else f"failed {r.status_code}"
        ok(f"POST /register   →  Auth/oauth_register  client_id={cid}…")
    except Exception as e:
        warn(f"register: {e}")

    # ── Phase 2: MCP handshake ─────────────────────────────────────────────────
    phase(2, "MCP handshake",
          "MCP/initialize · MCP/tools_list · MCP/ping transactions · mcp.protocol_version attr")

    try:
        r  = mcp_call(session, base_url, "initialize")
        sv = r.get("result", {}).get("serverInfo", {})
        pv = r.get("result", {}).get("protocolVersion", "?")
        ok(f"initialize  →  {sv.get('name')} v{sv.get('version')}  protocol={pv}")
    except Exception as e: warn(f"initialize: {e}")

    try:
        r = mcp_call(session, base_url, "tools/list")
        n = len(r.get("result", {}).get("tools", []))
        ok(f"tools/list  →  {n} tools registered")
    except Exception as e: warn(f"tools/list: {e}")

    try:
        mcp_call(session, base_url, "ping")
        ok("ping  →  MCP/ping transaction")
    except Exception as e: warn(f"ping: {e}")

    # ── Phase 3: Unknown methods ───────────────────────────────────────────────
    phase(3, "Unknown JSON-RPC methods",
          "MCPUnknownMethod custom event · mcp.jsonrpc_error_code=-32601 · mcp.unknown_method attr")

    for method in ["sampling/createMessage", "roots/list", "prompts/get"]:
        try:
            r    = mcp_call(session, base_url, method)
            code = r.get("error", {}).get("code", "?")
            ok(f"{method:<30}  →  error {code}  (MCPUnknownMethod event fired)")
        except Exception as e: warn(f"{method}: {e}")
        time.sleep(0.2)

    # ── Phase 4: All 18 tools ──────────────────────────────────────────────────
    phase(4, "All 18 tools — one call each",
          "Every Tool/* function trace segment · cds.* attributes · varied mcp.client_name")
    sub("Each call uses a different simulated MCP client User-Agent")

    post_ids, category_ids, tag_ids, author_ids = discover_ids(session, base_url)

    def pid(ids, fallback="1"):
        return str(ids[0]) if ids else fallback

    all_18 = [
        ("get_publisher_data",    {},                                    "Publisher profile"),
        ("get_navbar",            {},                                    "Navigation menu"),
        ("get_footer",            {},                                    "Footer config"),
        ("get_content_types",     {},                                    "Content types"),
        ("get_active_slots",      {},                                    "Ad slots"),
        ("get_newsletter_groups", {},                                    "Newsletter groups"),
        ("list_posts",            {"limit": 5},                         "Posts default"),
        ("list_categories",       {"limit": 10},                        "Categories"),
        ("list_tags",             {"limit": 10},                        "Tags"),
        ("list_authors",          {"limit": 5},                         "Authors"),
        ("get_post",              {"identifier": pid(post_ids)},        "Post by ID"),
        ("get_post_by_url",       {"legacy_url": "/"},                  "Post by URL"),
        ("get_category",          {"identifier": pid(category_ids)},    "Category by ID"),
        ("get_tag",               {"identifier": pid(tag_ids)},         "Tag by ID"),
        ("get_author",            {"identifier": pid(author_ids)},      "Author by ID"),
        ("identify_content",      {"legacy_url": "/"},                  "Identify URL"),
        ("get_live_blog_updates", {"post_id": int(pid(post_ids, "1"))}, "Live blog updates"),
        ("get_form_schema",       {"schema_id": "000000000000000000000000"}, "Form schema"),
    ]

    for i, (tool_name, args, label) in enumerate(all_18):
        ua     = MCP_CLIENTS[i % len(MCP_CLIENTS)]
        prompt = random.choice(PROMPTS)
        s, ms  = run_tool(session, base_url, tool_name, args,
                          label=label, prompt=prompt, ua=ua)
        record(tool_name, s, ms)
        time.sleep(random.uniform(0.2, 0.4))

    # ── Phase 5: Prompt source variety ────────────────────────────────────────
    phase(5, "Prompt source variety",
          "MCPPrompt.prompt_source: header / meta / arg / tool_args(fallback)")

    prompt_cases = [
        ("list_posts",      {"limit": 3}, "header", "X-MCP-Prompt header"),
        ("list_tags",       {"limit": 3}, "meta",   "_meta.prompt in request body"),
        ("list_categories", {"limit": 3}, "arg",    "arguments._prompt (stripped before tool)"),
        ("get_navbar",      {},            None,     "no prompt  →  tool_args fallback"),
    ]

    for tool_name, args, via, label in prompt_cases:
        prompt = random.choice(PROMPTS)
        s, ms  = run_tool(session, base_url, tool_name, args,
                          label=f"source={via or 'tool_args'}",
                          prompt=prompt if via else None,
                          prompt_via=via or "header")
        record(tool_name, s, ms)
        source_shown = via or "tool_args"
        if s:
            ok(f"  ↳  prompt_source={source_shown:<12}  text: \"{prompt[:45]}…\"")
        time.sleep(0.3)

    # ── Phase 6: Deliberate errors ────────────────────────────────────────────
    phase(6, "Deliberate errors",
          "MCPToolError events · error.layer=cds · error_type · error_message · duration_ms")
    sub("These are intentional — they populate the error rate chart and MCPToolError table")

    error_cases = [
        ("get_post",       {"identifier": "nonexistent-post-99999"},    "Bad post slug"),
        ("get_category",   {"identifier": "nonexistent-category-99999"},"Bad category ID"),
        ("get_tag",        {"identifier": "nonexistent-tag-99999"},     "Bad tag ID"),
        ("get_author",     {"identifier": "nonexistent-author-00000"},  "Bad author ID"),
        ("get_post_by_url",{"legacy_url": "/this-url-does-not-exist/"}, "Bad URL path"),
    ]

    for tool_name, args, label in error_cases:
        s, ms = run_tool(session, base_url, tool_name, args,
                         label=f"ERR: {label}",
                         prompt=f"Intentional error: {label}")
        record(tool_name, s, ms)
        time.sleep(0.3)

    # ── Phase 7: Concurrent burst ─────────────────────────────────────────────
    phase(7, f"Concurrent burst  ({concurrency} simultaneous threads)",
          "Custom/MCP/active_threads metric · mcp.thread_active_count attribute pushed high")
    sub(f"Sends {concurrency} requests at the same time — watch the thread metric spike in NR")

    fast_tools = [
        "get_navbar", "get_footer", "list_tags", "list_categories",
        "get_content_types", "get_newsletter_groups", "get_active_slots", "get_publisher_data",
    ]

    def burst_call(i):
        tool   = fast_tools[i % len(fast_tools)]
        prompt = random.choice(PROMPTS)
        ua     = MCP_CLIENTS[i % len(MCP_CLIENTS)]
        return tool, *run_tool(session, base_url, tool, {},
                               label=f"burst-{i+1}", prompt=prompt, ua=ua)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(burst_call, i) for i in range(concurrency)]
        for f in as_completed(futures):
            tool_name, s, ms = f.result()
            record(tool_name, s, ms)

    ok(f"Burst complete  →  check Custom/MCP/active_threads in NR Metrics")

    # ── Phase 8: SSE session ──────────────────────────────────────────────────
    phase(8, "SSE session  (legacy transport)",
          "SSESessionOpen event (on connect) · SSESessionClose event (on disconnect) · duration_ms")

    run_sse_session(base_url, publisher_id, api_key, api_secret)

    # ── Phase 9: Batch JSON-RPC ───────────────────────────────────────────────
    phase(9, "Batch JSON-RPC request",
          "Single POST dispatches multiple JSON-RPC calls — each gets its own MCP/* transaction")

    batch = [
        {"jsonrpc": "2.0", "id": next_id(), "method": "tools/call",
         "params": {"name": "get_navbar",       "arguments": {}}},
        {"jsonrpc": "2.0", "id": next_id(), "method": "tools/call",
         "params": {"name": "get_footer",        "arguments": {}}},
        {"jsonrpc": "2.0", "id": next_id(), "method": "tools/call",
         "params": {"name": "get_content_types", "arguments": {}}},
        {"jsonrpc": "2.0", "id": next_id(), "method": "ping"},
    ]
    try:
        t0   = time.perf_counter()
        resp = session.post(
            f"{base_url}/mcp", json=batch,
            headers={"X-MCP-Prompt": "Batch request test — 4 calls in one POST"},
            timeout=30,
        )
        ms      = round((time.perf_counter() - t0) * 1000)
        results = resp.json() if resp.ok else []
        ok(f"Batch: {len(batch)} requests → {len(results)} responses  [{ms}ms]")
        total  += len(batch)
        passed += len(results)
    except Exception as e:
        warn(f"Batch request failed: {e}")

    # ── Phase 10: Sustained traffic rounds ────────────────────────────────────
    phase(10, f"Sustained traffic  ({rounds} rounds)",
          "Throughput, latency, error-rate time-series  ·  Publisher + client breakdown charts")

    round_scenarios: list[tuple] = [
        ("list_posts",            {"limit": 10},                          "posts default"),
        ("list_posts",            {"type__eq": "Article", "limit": 5},   "posts articles"),
        ("list_posts",            {"type__eq": "Video",   "limit": 5},   "posts videos"),
        ("list_posts",            {"word_count__gt": 300, "limit": 5},   "posts long-form"),
        ("list_posts",            {"type__in": "Article,Video", "limit": 5}, "posts art+vid"),
        ("list_categories",       {"limit": 20},                          "categories"),
        ("list_tags",             {"limit": 20},                          "tags"),
        ("list_authors",          {"limit": 10},                          "authors"),
        ("get_navbar",            {},                                      "navbar"),
        ("get_footer",            {},                                      "footer"),
        ("get_publisher_data",    {},                                      "publisher data"),
        ("get_content_types",     {},                                      "content types"),
        ("get_active_slots",      {},                                      "ad slots"),
        ("get_newsletter_groups", {},                                      "newsletters"),
    ]
    if post_ids:
        for pid_ in post_ids[:2]:
            round_scenarios.append(("get_post", {"identifier": str(pid_)}, f"post #{pid_}"))
    if category_ids:
        for cid_ in category_ids[:2]:
            round_scenarios.append(("get_category", {"identifier": str(cid_)}, f"cat #{cid_}"))
    if tag_ids:
        round_scenarios.append(("get_tag", {"identifier": str(tag_ids[0])}, f"tag #{tag_ids[0]}"))

    # Always include a couple of errors per round so error rate stays visible
    round_errors = [
        ("get_post",     {"identifier": "bad-slug-round-error"},    "ERR: bad post"),
        ("get_category", {"identifier": "bad-category-round-error"},"ERR: bad category"),
    ]

    for rnd in range(1, rounds + 1):
        combined = round_scenarios + round_errors
        random.shuffle(combined)
        print(f"\n  {BOLD}Round {rnd}/{rounds}{RESET}  ({len(combined)} calls)")

        for tool_name, args, label in combined:
            prompt = random.choice(PROMPTS)
            ua     = random.choice(MCP_CLIENTS)
            s, ms  = run_tool(session, base_url, tool_name, args,
                              label=f"r{rnd} {label}", prompt=prompt, ua=ua)
            record(tool_name, s, ms)
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    # ── Final summary ──────────────────────────────────────────────────────────
    elapsed = round((datetime.now() - start).total_seconds(), 1)

    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  Run complete{RESET}")
    print(f"{'═'*60}")
    print(f"  Total calls  :  {total}")
    print(f"  {GREEN}Success      :  {passed}{RESET}")
    print(f"  {RED}Errors       :  {failed}  ← intentional, populates error charts{RESET}")
    print(f"  Elapsed      :  {elapsed}s  (~{round(total/elapsed, 1) if elapsed else '?'} calls/sec)")

    if stats:
        print(f"\n{BOLD}Per-tool average latency:{RESET}")
        for name, times in sorted(stats.items(), key=lambda x: -(sum(x[1]) / max(len(x[1]), 1))):
            avg   = round(sum(times) / len(times))
            count = len(times)
            bar   = "█" * min(avg // 60, 18)
            print(f"  {name:<28}  {avg:>5}ms  {bar}  ({count} calls)")

    print(f"\n{BOLD}What to verify in New Relic:{RESET}")
    checks = [
        ("APM → Summary",               "Throughput + error rate time-series populated"),
        ("APM → Transactions",           "MCP/*, Auth/*, Transport/SSE all present"),
        ("APM → Transaction Traces",     "Waterfall: mcp_endpoint → call_tool → cds_get"),
        ("APM → Errors",                 "Errors grouped by tool, error_type visible"),
        ("APM → Logs",                   "Log lines correlated to traces (trace.id linked)"),
        ("Events → MCPPrompt",           "prompt_text, prompt_source, session_id, publisher_id"),
        ("Events → MCPToolError",        "error_message, error_type, duration_ms per tool"),
        ("Events → MCPUnknownMethod",    "sampling/createMessage, roots/list, prompts/get"),
        ("Events → SSESessionOpen",      "session_id, publisher_id, active_threads"),
        ("Events → SSESessionClose",     "session_id, publisher_id, duration_ms"),
        ("Metrics → Custom/Tool/*",      "Per-tool latency histograms (13-month retention)"),
        ("Metrics → Custom/MCP/*",       "active_threads spike in Phase 7 visible"),
        ("Metrics → Custom/CDS/*",       "latency_ms, response_size_bytes, error_count"),
        ("Distributed Tracing",          "CDS span shows cds.url, cds.path, cds.publisher_id"),
        ("Attributes → mcp.client_name", "5 different simulated clients in FACET queries"),
        ("Attributes → mcp.prompt_source","header / meta / arg / tool_args all present"),
    ]
    col = max(len(loc) for loc, _ in checks)
    for location, what in checks:
        print(f"  {CYAN}{location:<{col}}{RESET}   {what}")
    print()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate MCP traffic to populate every New Relic dashboard panel"
    )
    parser.add_argument(
        "--rounds", type=int, default=DEFAULT_ROUNDS,
        help=f"Sustained traffic rounds in Phase 10 (default: {DEFAULT_ROUNDS})",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"Parallel threads for burst phase (default: {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--local", action="store_true",
        help="Target localhost:8000 instead of Railway",
    )
    args = parser.parse_args()

    base_url = LOCAL_BASE if args.local else DEFAULT_BASE

    publisher_id = os.getenv("PUBLISHER_ID") or os.getenv("publisherId") or ""
    api_key      = os.getenv("API_KEY")      or os.getenv("apiKey")      or ""
    api_secret   = os.getenv("API_SECRET")   or os.getenv("apiSecret")   or ""

    if not all([publisher_id, api_key, api_secret]):
        print(f"\n{BOLD}Enter Publive credentials:{RESET}")
        publisher_id = publisher_id or input("  Publisher ID : ").strip()
        api_key      = api_key      or input("  API Key      : ").strip()
        api_secret   = api_secret   or input("  API Secret   : ").strip()

    run(base_url, publisher_id, api_key, api_secret,
        rounds=args.rounds, concurrency=args.concurrency)
