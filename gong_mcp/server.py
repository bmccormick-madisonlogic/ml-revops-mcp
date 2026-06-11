import asyncio
import base64
import json
import os

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
from starlette.responses import Response
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
                        "description": "Max calls to return (default 50, max 100)",
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
                "reverse chronological order. Useful for account research and deal reviews."
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
    raise ValueError(f"Unknown tool: {name}")


def _get_calls(client: httpx.Client, args: dict) -> list[TextContent]:
    limit = min(int(args.get("limit", 50)), 100)
    params = {"fromDateTime": args["from_date"], "toDateTime": args["to_date"]}
    resp = client.get("/calls", params=params)
    resp.raise_for_status()
    data = resp.json()
    calls = data.get("calls", [])[:limit]
    # Return only key fields to keep response concise
    summary = [
        {
            "id": c.get("id"),
            "title": c.get("title"),
            "started": c.get("started"),
            "duration_seconds": c.get("duration"),
            "direction": c.get("direction"),
        }
        for c in calls
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


def _get_calls_by_account(client: httpx.Client, args: dict) -> list[TextContent]:
    body: dict = {"filter": {"crmAccountIds": [args["crm_account_id"]]}}
    if "from_date" in args:
        body["filter"]["fromDateTime"] = args["from_date"]
    if "to_date" in args:
        body["filter"]["toDateTime"] = args["to_date"]
    resp = client.post("/calls/extensive", json=body)
    resp.raise_for_status()
    calls = resp.json().get("calls", [])
    summary = [
        {
            "id": c.get("metaData", {}).get("id"),
            "title": c.get("metaData", {}).get("title"),
            "started": c.get("metaData", {}).get("started"),
            "duration_seconds": c.get("metaData", {}).get("duration"),
        }
        for c in calls
    ]
    return [TextContent(type="text", text=json.dumps(summary, indent=2))]


# ── Auth middleware ──────────────────────────────────────────────────────────

class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        api_key = os.getenv("MCP_API_KEY")
        if api_key:
            provided = request.headers.get("X-API-Key") or request.query_params.get("api_key")
            if provided != api_key:
                return Response("Unauthorized", status_code=401)
        return await call_next(request)


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


app = Starlette(
    middleware=[Middleware(ApiKeyMiddleware)],
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
        Route("/health", endpoint=lambda r: Response("ok")),
    ],
)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
