"""
Traffic Generator for Publive MCP Server
=========================================
Generates realistic MCP tool calls to produce visible data in:
  - New Relic  : APM throughput, response times, error rate, traces
  - Langfuse   : Tool call traces, durations, errors

Usage:
    python generate_traffic.py                        # prompts for credentials
    python generate_traffic.py --rounds 5             # 5 full rounds (~100 calls)
    python generate_traffic.py --local                # hit localhost:8000 instead

Requirements:
    pip install requests python-dotenv
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_BASE   = "https://tests-production-8b04.up.railway.app"
LOCAL_BASE     = "http://127.0.0.1:8000"
DEFAULT_ROUNDS = 3       # 1 round ≈ 20 tool calls
DELAY_MIN      = 0.3     # seconds between calls (min)
DELAY_MAX      = 0.8     # seconds between calls (max)

# ── Colour helpers ────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):    print(f"  {GREEN}✅ {msg}{RESET}")
def err(msg):   print(f"  {RED}❌ {msg}{RESET}")
def warn(msg):  print(f"  {YELLOW}⚠️  {msg}{RESET}")
def info(msg):  print(f"  {CYAN}ℹ  {msg}{RESET}")
def header(msg):print(f"\n{BOLD}{msg}{RESET}")


# ── Auth ──────────────────────────────────────────────────────────────────────

def login(base_url: str, publisher_id: str, api_key: str, api_secret: str) -> requests.Session:
    """POST /auth/login and return an authenticated session."""
    session = requests.Session()
    resp = session.post(
        f"{base_url}/auth/login",
        json={"publisherId": publisher_id, "apiKey": api_key, "apiSecret": api_secret},
        timeout=15,
    )
    if not resp.ok or not resp.json().get("success"):
        raise RuntimeError(f"Login failed ({resp.status_code}): {resp.text[:200]}")
    return session


# ── MCP helpers ───────────────────────────────────────────────────────────────

def mcp_call(session: requests.Session, base_url: str, method: str,
             params: dict = None, req_id: int = 1) -> dict:
    """Send a single JSON-RPC request to /mcp."""
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params:
        payload["params"] = params
    resp = session.post(f"{base_url}/mcp", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def tool_call(session: requests.Session, base_url: str,
              tool_name: str, arguments: dict = None, req_id: int = 1) -> dict:
    """Call a tools/call method."""
    return mcp_call(session, base_url, "tools/call",
                    {"name": tool_name, "arguments": arguments or {}},
                    req_id=req_id)


# ── Tool scenarios ────────────────────────────────────────────────────────────

def build_scenarios(post_ids: list, category_ids: list, tag_ids: list) -> list:
    """
    Build a varied list of (tool_name, args, label) tuples.
    Mix of fast, medium and slow calls + deliberate error cases.
    """
    scenarios = []

    # --- Fast / cached-like calls ---
    scenarios += [
        ("get_navbar",       {},                          "Navigation menu"),
        ("get_footer",       {},                          "Footer config"),
        ("get_content_types",{},                          "Content types"),
        ("list_categories",  {"limit": 10},               "Categories p1"),
        ("list_tags",        {"limit": 10},               "Tags p1"),
        ("list_tags",        {"limit": 10, "page": 2},    "Tags p2"),
    ]

    # --- Post listing with various filters ---
    scenarios += [
        ("list_posts", {"limit": 5},                                      "Posts default"),
        ("list_posts", {"limit": 10, "page": 1},                          "Posts p1 limit10"),
        ("list_posts", {"limit": 5,  "page": 2},                          "Posts p2"),
        ("list_posts", {"type__eq": "Article", "limit": 5},               "Posts Articles"),
        ("list_posts", {"type__eq": "Video",   "limit": 5},               "Posts Videos"),
        ("list_posts", {"type__in": "Article,Video", "limit": 5},         "Posts Art+Vid"),
        ("list_posts", {"word_count__gt": 500, "limit": 5},               "Posts long-form"),
    ]

    # --- Fetch specific posts (if we discovered any IDs) ---
    for pid in post_ids[:3]:
        scenarios.append(("get_post", {"identifier": str(pid)}, f"Post #{pid}"))

    # --- Fetch specific categories ---
    for cid in category_ids[:2]:
        scenarios.append(("get_category", {"identifier": str(cid)}, f"Category #{cid}"))

    # --- Fetch specific tags ---
    for tid in tag_ids[:2]:
        scenarios.append(("get_tag", {"identifier": str(tid)}, f"Tag #{tid}"))

    # --- Error-producing calls (intentional — shows up red in dashboards) ---
    scenarios += [
        ("get_post",     {"identifier": "nonexistent-slug-99999"}, "ERROR: bad post slug"),
        ("get_category", {"identifier": "nonexistent-cat-99999"},  "ERROR: bad category"),
        ("get_tag",      {"identifier": "nonexistent-tag-99999"},  "ERROR: bad tag"),
    ]

    return scenarios


# ── Main runner ───────────────────────────────────────────────────────────────

def run(base_url: str, publisher_id: str, api_key: str, api_secret: str, rounds: int):
    start_time = datetime.now()
    total_calls = ok_count = error_count = 0

    header(f"🚀  Publive MCP Traffic Generator")
    print(f"     Target  : {base_url}")
    print(f"     Publisher: {publisher_id}")
    print(f"     Rounds   : {rounds}")
    print(f"     Started  : {start_time.strftime('%H:%M:%S')}")

    # ── Step 1: Login ─────────────────────────────────────────────────────────
    header("Step 1 — Authenticating")
    try:
        session = login(base_url, publisher_id, api_key, api_secret)
        ok("Session established")
    except RuntimeError as e:
        err(str(e))
        sys.exit(1)

    # ── Step 2: MCP initialize ────────────────────────────────────────────────
    header("Step 2 — MCP handshake (initialize + tools/list)")
    try:
        init_resp = mcp_call(session, base_url, "initialize", req_id=0)
        server_info = init_resp.get("result", {}).get("serverInfo", {})
        ok(f"Server: {server_info.get('name')} v{server_info.get('version')}")
    except Exception as e:
        err(f"initialize failed: {e}")
        sys.exit(1)

    try:
        tools_resp = mcp_call(session, base_url, "tools/list", req_id=1)
        tools = tools_resp.get("result", {}).get("tools", [])
        ok(f"{len(tools)} tools registered")
    except Exception as e:
        warn(f"tools/list failed: {e}")
        tools = []

    # ── Step 3: Discover IDs for deeper calls ─────────────────────────────────
    header("Step 3 — Discovering IDs for richer scenarios")
    post_ids = category_ids = tag_ids = []

    try:
        r = tool_call(session, base_url, "list_posts", {"limit": 5}, req_id=2)
        posts = r.get("result", {}).get("content", [{}])[0].get("text", "[]")
        data  = json.loads(posts) if isinstance(posts, str) else posts
        post_ids = [p["id"] for p in (data.get("data") or data or [])[:5] if isinstance(p, dict) and "id" in p]
        ok(f"Found post IDs: {post_ids[:3]}")
    except Exception as e:
        warn(f"Could not fetch posts for ID discovery: {e}")

    try:
        r = tool_call(session, base_url, "list_categories", {"limit": 5}, req_id=3)
        cats = r.get("result", {}).get("content", [{}])[0].get("text", "[]")
        data = json.loads(cats) if isinstance(cats, str) else cats
        category_ids = [c["id"] for c in (data.get("data") or data or [])[:5] if isinstance(c, dict) and "id" in c]
        ok(f"Found category IDs: {category_ids[:3]}")
    except Exception as e:
        warn(f"Could not fetch categories for ID discovery: {e}")

    try:
        r = tool_call(session, base_url, "list_tags", {"limit": 5}, req_id=4)
        tags = r.get("result", {}).get("content", [{}])[0].get("text", "[]")
        data = json.loads(tags) if isinstance(tags, str) else tags
        tag_ids = [t["id"] for t in (data.get("data") or data or [])[:5] if isinstance(t, dict) and "id" in t]
        ok(f"Found tag IDs: {tag_ids[:3]}")
    except Exception as e:
        warn(f"Could not fetch tags for ID discovery: {e}")

    # ── Step 4: Traffic rounds ────────────────────────────────────────────────
    header(f"Step 4 — Generating traffic ({rounds} rounds)")
    scenarios = build_scenarios(post_ids, category_ids, tag_ids)
    random.shuffle(scenarios)

    req_id = 10
    for rnd in range(1, rounds + 1):
        print(f"\n  {BOLD}Round {rnd}/{rounds}{RESET}  ({len(scenarios)} calls)")
        random.shuffle(scenarios)   # different order each round

        for tool_name, args, label in scenarios:
            total_calls += 1
            req_id += 1
            t0 = time.perf_counter()
            try:
                resp = tool_call(session, base_url, tool_name, args, req_id=req_id)
                ms   = round((time.perf_counter() - t0) * 1000)

                content = resp.get("result", {}).get("content", [{}])
                text    = content[0].get("text", "") if content else ""
                is_err  = resp.get("result", {}).get("isError", False) or text.startswith("Error:")

                if is_err:
                    error_count += 1
                    warn(f"[{ms:>5}ms]  {tool_name:<22}  {label}  → {text[:60]}")
                else:
                    ok_count += 1
                    # Show a tiny preview of returned data
                    preview = text[:60].replace("\n", " ")
                    ok(f"[{ms:>5}ms]  {tool_name:<22}  {label}  → {preview}…")

            except Exception as exc:
                ms = round((time.perf_counter() - t0) * 1000)
                error_count += 1
                err(f"[{ms:>5}ms]  {tool_name:<22}  {label}  → EXCEPTION: {exc}")

            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    # ── Step 5: Summary ───────────────────────────────────────────────────────
    elapsed = round((datetime.now() - start_time).total_seconds(), 1)
    header("Summary")
    print(f"  Total calls : {total_calls}")
    print(f"  {GREEN}Successful  : {ok_count}{RESET}")
    print(f"  {RED}Errors      : {error_count}  (expected — these show error rate in dashboards){RESET}")
    print(f"  Elapsed     : {elapsed}s")
    print()
    print(f"  {BOLD}New Relic dashboard:{RESET}")
    print(f"    https://one.newrelic.com/redirect/entity/ODA5MzY1N3xBUE18QVBQTElDQVRJT058NTkyMTg0NjQy")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate MCP traffic for New Relic + Langfuse")
    parser.add_argument("--rounds",  type=int, default=DEFAULT_ROUNDS,
                        help=f"Number of full scenario rounds (default: {DEFAULT_ROUNDS})")
    parser.add_argument("--local",   action="store_true",
                        help="Target localhost:8000 instead of Railway")
    args = parser.parse_args()

    base_url = LOCAL_BASE if args.local else DEFAULT_BASE

    # Credentials — env vars first, then interactive prompt
    publisher_id = os.getenv("PUBLISHER_ID") or os.getenv("publisherId") or ""
    api_key      = os.getenv("API_KEY")      or os.getenv("apiKey")      or ""
    api_secret   = os.getenv("API_SECRET")   or os.getenv("apiSecret")   or ""

    if not all([publisher_id, api_key, api_secret]):
        print(f"\n{BOLD}Enter Publive credentials:{RESET}")
        publisher_id = publisher_id or input("  Publisher ID : ").strip()
        api_key      = api_key      or input("  API Key      : ").strip()
        api_secret   = api_secret   or input("  API Secret   : ").strip()

    run(base_url, publisher_id, api_key, api_secret, rounds=args.rounds)
