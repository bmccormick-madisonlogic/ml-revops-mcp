import asyncio
import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import uvicorn
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

load_dotenv()

server = Server("gong")

def _client() -> httpx.Client:
    key = os.environ["GONG_ACCESS_KEY"]
    secret = os.environ["GONG_SECRET"]
    base_url = os.environ.get("GONG_BASE_URL", "https://api.gong.io").rstrip("/") + "/v2"
    auth = "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": auth, "Content-Type": "application/json"},
        timeout=30,
    )


# ── Tool definitions ────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_calls",
            description=(
                "List Gong calls within a date range. Returns call ID, title, date, "
                "duration, and primary participants. Use this to find a call ID before "
                "pulling a transcript or details."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from_date": {
                        "type": "string",
                        "description": "Start datetime ISO 8601, e.g. 2024-01-01T00:00:00Z",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "End datetime ISO 8601, e.g. 2024-01-31T23:59:59Z",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max calls to return (default 50, max 500). Paginates automatically.",
                    },
                },
                "required": ["from_date", "to_date"],
            },
        ),
        Tool(
            name="get_call_transcript",
            description=(
                "Get the full transcript of a Gong call by call ID. Returns speaker-labelled "
                "sentences. Use get_calls first to find the call ID."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "call_id": {"type": "string", "description": "Gong call ID"},
                },
                "required": ["call_id"],
            },
        ),
        Tool(
            name="get_call_details",
            description=(
                "Get structured details for a call: participants, talk-time breakdown, "
                "Gong trackers, topics discussed, and AI-generated brief. Good for deal "
                "summaries without reading the full transcript."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "call_id": {"type": "string", "description": "Gong call ID"},
                },
                "required": ["call_id"],
            },
        ),
        Tool(
            name="get_calls_by_account",
            description=(
                "Get all Gong calls linked to a Salesforce account ID. Returns calls in "
                "reverse chronological order. Useful for account research and deal reviews. "
                "Searches the last 90 days by default; pass from_date/to_date to widen the "
                "search if no calls are found. IMPORTANT: for dormant/inactive/churned "
                "accounts, win-back or reactivation research, or any query where the account "
                "may not have recent activity by definition, set from_date back 1-2+ years "
                "from the start — the 90-day default will look empty for exactly these accounts, "
                "which is a search-scope artifact, not evidence of no Gong history."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "crm_account_id": {
                        "type": "string",
                        "description": "Salesforce account ID (18-char)",
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Optional start datetime ISO 8601",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "Optional end datetime ISO 8601",
                    },
                },
                "required": ["crm_account_id"],
            },
        ),
        Tool(
            name="get_user_by_email",
            description=(
                "Look up a Gong user by their email address. Returns their Gong user ID, "
                "name, and title. Use this to resolve a rep's email to a Gong ID before "
                "calling get_calls_by_rep or get_rep_stats."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Rep's email address, e.g. jsmith@madisonlogic.com",
                    },
                },
                "required": ["email"],
            },
        ),
        Tool(
            name="get_calls_by_rep",
            description=(
                "Get all Gong calls where a specific rep was the primary host, looked up by "
                "email. Much more efficient than get_calls + scanning parties — resolves the "
                "rep's Gong user ID then filters server-side. Use for rep-level call reviews. "
                "IMPORTANT: the response includes a `truncated` field. If `truncated` is true, "
                "you have NOT seen the full date range — do not answer the user's question yet. "
                "Re-call this tool with `from_date` set to the `next_from_date` value given in "
                "the response and keep going until `truncated` is false, THEN answer."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Rep's email address",
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Start datetime ISO 8601",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "End datetime ISO 8601",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max calls to return in this page (default 200, max 3000)",
                    },
                },
                "required": ["email", "from_date", "to_date"],
            },
        ),
        Tool(
            name="get_rep_stats",
            description=(
                "Get activity stats for a rep over a date range: number of calls, total talk "
                "time, listening time, and average talk ratio. Useful for coaching briefs and "
                "manager 1:1s. Looks up the rep by email."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Rep's email address",
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Start datetime ISO 8601",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "End datetime ISO 8601",
                    },
                },
                "required": ["email", "from_date", "to_date"],
            },
        ),
        Tool(
            name="get_call_trackers",
            description=(
                "Get tracker hits for a call — competitor mentions, pricing discussions, "
                "risk signals, objections, and any custom trackers your team has configured. "
                "Faster than reading the full transcript for deal intelligence."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "call_id": {"type": "string", "description": "Gong call ID"},
                },
                "required": ["call_id"],
            },
        ),
        Tool(
            name="get_account_call_summary",
            description=(
                "Get a synthesized summary of the last N Gong calls for a Salesforce account. "
                "For each call returns: date, duration, external participants, Gong AI brief, "
                "key points, topics discussed, and tracker hits (competitor mentions, objections, etc.). "
                "Use this for meeting prep and account reviews — much faster than pulling individual transcripts. "
                "Searches the last 90 days by default; pass from_date/to_date to widen the search "
                "if no calls are found. IMPORTANT: for dormant/inactive/churned accounts, win-back "
                "or reactivation research, or any query where the account may not have recent "
                "activity by definition, set from_date back 1-2+ years from the start — the 90-day "
                "default will look empty for exactly these accounts, which is a search-scope "
                "artifact, not evidence of no Gong history."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "crm_account_id": {
                        "type": "string",
                        "description": "Salesforce account ID (18-char)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of recent calls to summarize (default 3, max 5)",
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Optional start datetime ISO 8601. Defaults to 90 days ago.",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "Optional end datetime ISO 8601. Defaults to now.",
                    },
                },
                "required": ["crm_account_id"],
            },
        ),
    ]


# ── Tool implementations ─────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    with _client() as client:
        if name == "get_calls":
            return _get_calls(client, arguments)
        if name == "get_call_transcript":
            return _get_call_transcript(client, arguments)
        if name == "get_call_details":
            return _get_call_details(client, arguments)
        if name == "get_calls_by_account":
            return _get_calls_by_account(client, arguments)
        if name == "get_user_by_email":
            return _get_user_by_email(client, arguments)
        if name == "get_calls_by_rep":
            return _get_calls_by_rep(client, arguments)
        if name == "get_rep_stats":
            return _get_rep_stats(client, arguments)
        if name == "get_call_trackers":
            return _get_call_trackers(client, arguments)
        if name == "get_account_call_summary":
            return _get_account_call_summary(client, arguments)
    raise ValueError(f"Unknown tool: {name}")


def _get_calls(client: httpx.Client, args: dict) -> list[TextContent]:
    limit = min(int(args.get("limit", 50)), 500)
    params = {"fromDateTime": args["from_date"], "toDateTime": args["to_date"]}
    calls = []
    while len(calls) < limit:
        resp = client.get("/calls", params=params)
        resp.raise_for_status()
        data = resp.json()
        calls.extend(data.get("calls", []))
        cursor = data.get("records", {}).get("cursor")
        if not cursor:
            break
        params["cursor"] = cursor
    summary = [
        {
            "id": c.get("id"),
            "title": c.get("title"),
            "started": c.get("started"),
            "duration_seconds": c.get("duration"),
            "direction": c.get("direction"),
        }
        for c in calls[:limit]
    ]
    return [TextContent(type="text", text=json.dumps(summary, indent=2))]


def _get_call_transcript(client: httpx.Client, args: dict) -> list[TextContent]:
    body = {"filter": {"callIds": [args["call_id"]]}}
    resp = client.post("/calls/transcript", json=body)
    resp.raise_for_status()
    data = resp.json()
    transcripts = data.get("callTranscripts", [])
    if not transcripts:
        return [TextContent(type="text", text="No transcript found for this call ID.")]
    # Flatten to readable text
    lines = []
    for sentence in transcripts[0].get("transcript", []):
        speaker = sentence.get("speakerId", "Unknown")
        for s in sentence.get("sentences", []):
            lines.append(f"[{speaker}] {s.get('text', '')}")
    return [TextContent(type="text", text="\n".join(lines))]


def _get_call_details(client: httpx.Client, args: dict) -> list[TextContent]:
    body = {
        "filter": {"callIds": [args["call_id"]]},
        "contentSelector": {
            "exposedFields": {
                "parties": True,
                "content": {
                    "trackers": True,
                    "topics": True,
                    "brief": True,
                    "keyPoints": True,
                },
            }
        },
    }
    resp = client.post("/calls/extensive", json=body)
    resp.raise_for_status()
    calls = resp.json().get("calls", [])
    if not calls:
        return [TextContent(type="text", text="No call found for this ID.")]
    return [TextContent(type="text", text=json.dumps(calls[0], indent=2))]


def _call_matches_account(call: dict, crm_account_id: str) -> bool:
    # The CRM Account/Opportunity link lives on the call's own "context" field,
    # not on parties[].context (which only links parties to Salesforce Users).
    for ctx in call.get("context", []):
        for obj in ctx.get("objects", []):
            if obj.get("objectType") == "Account" and obj.get("objectId") == crm_account_id:
                return True
    return False


def _get_calls_by_account(client: httpx.Client, args: dict) -> list[TextContent]:
    # Gong's /calls/extensive filter has no crmAccountIds field (per Gong API docs),
    # so it's ignored server-side and every account query returned the same unfiltered
    # list. Account matching has to happen client-side against each call's own CRM
    # context (call.context[], not parties[].context, which only links to SF Users).
    crm_account_id = args["crm_account_id"]
    from_date = args.get(
        "from_date", (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    body: dict = {
        "filter": {"fromDateTime": from_date},
        "contentSelector": {
            "context": "Extended",
            "exposedFields": {"parties": True},
        },
    }
    if "to_date" in args:
        body["filter"]["toDateTime"] = args["to_date"]

    matched = []
    scanned = 0
    scan_limit = 5000
    truncated = False
    while True:
        resp = client.post("/calls/extensive", json=body)
        resp.raise_for_status()
        data = resp.json()
        page = data.get("calls", [])
        scanned += len(page)
        matched.extend(c for c in page if _call_matches_account(c, crm_account_id))
        cursor = data.get("records", {}).get("cursor")
        if not cursor:
            break
        if scanned >= scan_limit:
            truncated = True
            break
        body["cursor"] = cursor

    matched.sort(key=lambda c: c.get("metaData", {}).get("started", ""), reverse=True)
    result = {
        "date_range_scanned": {
            "from": from_date,
            "to": args.get("to_date", "now"),
            "note": "Defaults to the last 90 days; pass from_date/to_date to widen or narrow this.",
        },
        "call_count": len(matched),
        "calls": [
            {
                "id": c.get("metaData", {}).get("id"),
                "title": c.get("metaData", {}).get("title"),
                "started": c.get("metaData", {}).get("started"),
                "duration_seconds": c.get("metaData", {}).get("duration"),
            }
            for c in matched
        ],
    }
    if truncated:
        result["truncated"] = True
        result["note"] = (
            f"Scan stopped after {scanned} calls in range without reaching the end of the "
            "window; results may be incomplete. Narrow with from_date/to_date to see all matches."
        )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _get_user_by_email(client: httpx.Client, args: dict) -> list[TextContent]:
    email = args["email"].lower().strip()
    cursor = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        resp = client.get("/users", params=params)
        resp.raise_for_status()
        data = resp.json()
        users = data.get("users", [])
        match = next((u for u in users if u.get("emailAddress", "").lower() == email), None)
        if match:
            result = {
                "id": match.get("id"),
                "name": f"{match.get('firstName', '')} {match.get('lastName', '')}".strip(),
                "email": match.get("emailAddress"),
                "title": match.get("title"),
                "active": match.get("active"),
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        cursor = data.get("records", {}).get("cursor")
        if not cursor:
            break
    return [TextContent(type="text", text=f"No Gong user found with email {email}.")]


def _get_calls_by_rep(client: httpx.Client, args: dict) -> list[TextContent]:
    # Resolve email → Gong user ID
    user_result = _get_user_by_email(client, {"email": args["email"]})
    user_text = user_result[0].text
    if "No Gong user found" in user_text:
        return user_result
    user = json.loads(user_text)
    user_id = user["id"]

    limit = min(int(args.get("limit", 200)), 3000)
    body = {
        "filter": {
            "primaryUserId": [user_id],
            "fromDateTime": args["from_date"],
            "toDateTime": args["to_date"],
        }
    }
    calls = []
    truncated = False
    while True:
        resp = client.post("/calls/extensive", json=body)
        resp.raise_for_status()
        data = resp.json()
        calls.extend(data.get("calls", []))
        cursor = data.get("records", {}).get("cursor")
        if len(calls) >= limit:
            truncated = bool(cursor)
            break
        if not cursor:
            break
        body["cursor"] = cursor

    calls = calls[:limit]
    summary = [
        {
            "id": c.get("metaData", {}).get("id"),
            "title": c.get("metaData", {}).get("title"),
            "started": c.get("metaData", {}).get("started"),
            "duration_seconds": c.get("metaData", {}).get("duration"),
            "direction": c.get("metaData", {}).get("direction"),
        }
        for c in calls
    ]

    result = {
        "rep": user["name"],
        "period_requested": {"from": args["from_date"], "to": args["to_date"]},
        "calls_returned": len(summary),
        "truncated": truncated,
        "calls": summary,
    }
    if truncated:
        last_started = summary[-1]["started"] if summary else args["from_date"]
        result["next_from_date"] = last_started
        result["note"] = (
            f"Hit the {limit}-call page limit before reaching {args['to_date']}. "
            f"Only calls through {last_started} are included. This range has NOT been "
            f"fully covered — call get_calls_by_rep again with from_date="
            f"'{last_started}' (and the same to_date) to get the rest before answering."
        )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _get_rep_stats(client: httpx.Client, args: dict) -> list[TextContent]:
    # Resolve email → Gong user ID
    user_result = _get_user_by_email(client, {"email": args["email"]})
    user_text = user_result[0].text
    if "No Gong user found" in user_text:
        return user_result
    user = json.loads(user_text)
    user_id = user["id"]

    params = {
        "fromDateTime": args["from_date"],
        "toDateTime": args["to_date"],
        "userIds": user_id,
    }
    resp = client.get("/stats/activity/users", params=params)
    resp.raise_for_status()
    records = resp.json().get("usersStats", [])
    if not records:
        return [TextContent(type="text", text=f"No stats found for {args['email']} in this date range.")]
    stats = records[0]
    # Calculate talk ratio if raw times available
    talk = stats.get("talkingDuration", 0)
    listen = stats.get("listeningDuration", 0)
    total = talk + listen
    talk_ratio = round(talk / total * 100) if total > 0 else None
    result = {
        "rep": user["name"],
        "email": args["email"],
        "period": {"from": args["from_date"], "to": args["to_date"]},
        "calls": stats.get("numberOfCalls"),
        "talk_ratio_pct": talk_ratio,
        "avg_monologue_seconds": stats.get("avgMonologueDuration"),
        "avg_customer_engagement": stats.get("avgCustomerEngagement"),
        "interactivity": stats.get("avgInteractivity"),
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _get_call_trackers(client: httpx.Client, args: dict) -> list[TextContent]:
    body = {
        "filter": {"callIds": [args["call_id"]]},
        "contentSelector": {
            "exposedFields": {
                "content": {"trackers": True, "topics": True},
            }
        },
    }
    resp = client.post("/calls/extensive", json=body)
    resp.raise_for_status()
    calls = resp.json().get("calls", [])
    if not calls:
        return [TextContent(type="text", text="No call found for this ID.")]
    content = calls[0].get("content", {})
    trackers = content.get("trackers", [])
    topics = content.get("topics", [])
    result = {
        "call_id": args["call_id"],
        "trackers": [
            {"name": t.get("name"), "count": t.get("count"), "phrases": t.get("phrases", [])}
            for t in trackers
        ],
        "topics": [
            {"name": t.get("name"), "duration_pct": t.get("duration")}
            for t in topics
        ],
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _get_account_call_summary(client: httpx.Client, args: dict) -> list[TextContent]:
    # Same client-side CRM matching as _get_calls_by_account (no server-side account
    # filter exists). Scans the full window before sorting so "most recent N" is
    # accurate rather than just the first N matches encountered during pagination.
    limit = min(int(args.get("limit", 3)), 5)
    crm_account_id = args["crm_account_id"]
    from_date = args.get(
        "from_date", (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    body = {
        "filter": {"fromDateTime": from_date},
        "contentSelector": {
            "context": "Extended",
            "exposedFields": {
                "parties": True,
                "content": {
                    "brief": True,
                    "keyPoints": True,
                    "trackers": True,
                    "topics": True,
                },
            },
        },
    }
    if "to_date" in args:
        body["filter"]["toDateTime"] = args["to_date"]
    calls = []
    scanned = 0
    scan_limit = 5000
    truncated = False
    while True:
        resp = client.post("/calls/extensive", json=body)
        resp.raise_for_status()
        data = resp.json()
        page = data.get("calls", [])
        scanned += len(page)
        calls.extend(c for c in page if _call_matches_account(c, crm_account_id))
        cursor = data.get("records", {}).get("cursor")
        if not cursor:
            break
        if scanned >= scan_limit:
            truncated = True
            break
        body["cursor"] = cursor

    calls_sorted = sorted(
        calls,
        key=lambda c: c.get("metaData", {}).get("started", ""),
        reverse=True,
    )[:limit]

    summaries = []
    for c in calls_sorted:
        meta = c.get("metaData", {})
        content = c.get("content", {})
        parties = c.get("parties", [])

        external = [
            p.get("name") for p in parties
            if p.get("affiliation") == "External" and p.get("name")
        ]

        summaries.append({
            "call_id": meta.get("id"),
            "title": meta.get("title"),
            "date": meta.get("started"),
            "duration_minutes": round((meta.get("duration") or 0) / 60),
            "external_participants": external,
            "brief": content.get("brief"),
            "key_points": content.get("keyPoints", []),
            "topics": [
                {"name": t.get("name"), "duration_pct": t.get("duration")}
                for t in content.get("topics", [])
            ],
            "tracker_hits": [
                {"tracker": t.get("name"), "count": t.get("count")}
                for t in content.get("trackers", [])
                if (t.get("count") or 0) > 0
            ],
        })

    result = {
        "account_id": args["crm_account_id"],
        "date_range_scanned": {
            "from": from_date,
            "to": args.get("to_date", "now"),
            "note": "Defaults to the last 90 days; pass from_date/to_date to widen or narrow this.",
        },
        "calls_summarized": len(summaries),
        "calls": summaries,
    }
    if truncated:
        result["note"] = (
            f"Scan stopped after {scanned} calls in range without reaching the end; "
            "there may be more recent matching calls. Narrow with from_date/to_date if needed."
        )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


# ── Auth middleware ──────────────────────────────────────────────────────────

# Paths that must stay public: MCP protocol (/sse, /messages/), health, and the
# OAuth-shim handshake endpoints below.
_PUBLIC_PREFIXES = (
    "/messages/", "/health", "/sse",
    "/.well-known/", "/register", "/authorize", "/token",
)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES):
            api_key = os.getenv("MCP_API_KEY")
            if api_key:
                provided = request.headers.get("X-API-Key") or request.query_params.get("api_key")
                if provided != api_key:
                    return Response("Unauthorized", status_code=401)
        return await call_next(request)


# ── OAuth shim (CLOSED PILOT ONLY) ────────────────────────────────────────────
# Claude's connector requires an OAuth handshake before it will connect to a
# remote MCP. These endpoints satisfy that handshake with NO real authentication
# — everyone is granted access. This is no less secure than the endpoint already
# is (fully open). REPLACE with real IdP-backed OAuth before opening beyond the
# closed pilot.

def _public_base(request: Request) -> str:
    # Advertise the public https URL (respect Railway's proxy headers).
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"{proto}://{host}"


async def oauth_protected_resource(request: Request):
    base = _public_base(request)
    return JSONResponse({"resource": base, "authorization_servers": [base]})


async def oauth_authorization_server(request: Request):
    base = _public_base(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def oauth_register(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse({
        "client_id": "pilot-client",
        "client_id_issued_at": int(time.time()),
        "redirect_uris": body.get("redirect_uris", []),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }, status_code=201)


async def oauth_authorize(request: Request):
    p = request.query_params
    redirect_uri = p.get("redirect_uri")
    if not redirect_uri:
        return Response("missing redirect_uri", status_code=400)
    q = {"code": "pilot-code"}
    if p.get("state") is not None:
        q["state"] = p["state"]
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(q)}", status_code=302)


async def oauth_token(request: Request):
    return JSONResponse({
        "access_token": "pilot-token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "mcp",
    })


# ── SSE transport + app ──────────────────────────────────────────────────────

sse = SseServerTransport("/messages/")


async def handle_sse(request: Request):
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(
            streams[0],
            streams[1],
            server.create_initialization_options(),
        )
    # Newer Starlette treats the endpoint's return value as an ASGI response and
    # calls it; returning None crashes with "'NoneType' object is not callable".
    return Response()


app = Starlette(
    middleware=[Middleware(ApiKeyMiddleware)],
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
        Route("/health", endpoint=lambda r: Response("ok")),
        # OAuth shim (closed pilot only)
        Route("/.well-known/oauth-protected-resource", endpoint=oauth_protected_resource),
        Route("/.well-known/oauth-protected-resource/sse", endpoint=oauth_protected_resource),
        Route("/.well-known/oauth-authorization-server", endpoint=oauth_authorization_server),
        Route("/.well-known/oauth-authorization-server/sse", endpoint=oauth_authorization_server),
        Route("/register", endpoint=oauth_register, methods=["POST"]),
        Route("/authorize", endpoint=oauth_authorize, methods=["GET"]),
        Route("/token", endpoint=oauth_token, methods=["POST"]),
    ],
)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
