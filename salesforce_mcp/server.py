import json
import os

import httpx
import uvicorn
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.requests import Request
from starlette.responses import Response

load_dotenv()

server = Server("salesforce")


def _get_access_token() -> tuple[str, str]:
    """Returns (access_token, instance_url) via client credentials flow."""
    client_id = os.environ["SF_CLIENT_ID"]
    client_secret = os.environ["SF_CLIENT_SECRET"]
    instance_url = os.environ["SF_INSTANCE_URL"].rstrip("/")
    resp = httpx.post(
        f"{instance_url}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], data["instance_url"]


def _client() -> tuple[httpx.Client, str]:
    """Returns (httpx.Client, instance_url) authenticated against Salesforce REST API."""
    token, instance_url = _get_access_token()
    client = httpx.Client(
        base_url=f"{instance_url}/services/data/v59.0",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    return client, instance_url


def _soql(client: httpx.Client, query: str) -> list[dict]:
    resp = client.get("/query", params={"q": query})
    resp.raise_for_status()
    return resp.json().get("records", [])


# ── Tool definitions ─────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_accounts",
            description=(
                "Search Salesforce accounts by name (partial match). Returns account ID, "
                "name, industry, website, annual revenue, and owner. Use this to find an "
                "account ID before pulling opportunities or contacts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Account name or partial name to search for",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max records to return (default 20, max 50)",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="get_account",
            description=(
                "Get full details for a Salesforce account by its 18-character account ID. "
                "Returns name, industry, revenue, employee count, owner, billing address, "
                "and key dates."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account_id": {
                        "type": "string",
                        "description": "Salesforce account ID (18-char)",
                    },
                },
                "required": ["account_id"],
            },
        ),
        Tool(
            name="get_account_opportunities",
            description=(
                "Get opportunities for a Salesforce account ID. By default returns open "
                "opportunities only. Set include_closed=true to include won/lost deals. "
                "Returns stage, amount, close date, and owner."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account_id": {
                        "type": "string",
                        "description": "Salesforce account ID (18-char)",
                    },
                    "include_closed": {
                        "type": "boolean",
                        "description": "Include closed opportunities (default false)",
                    },
                },
                "required": ["account_id"],
            },
        ),
        Tool(
            name="get_account_contacts",
            description=(
                "Get contacts at a Salesforce account. Returns name, title, email, phone, "
                "and last activity date. Useful for identifying stakeholders."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account_id": {
                        "type": "string",
                        "description": "Salesforce account ID (18-char)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max contacts to return (default 25)",
                    },
                },
                "required": ["account_id"],
            },
        ),
        Tool(
            name="get_opportunity",
            description=(
                "Get full details for a specific opportunity by its ID. Returns stage, amount, "
                "close date, type, lead source, next step, and owner."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "opportunity_id": {
                        "type": "string",
                        "description": "Salesforce opportunity ID (18-char)",
                    },
                },
                "required": ["opportunity_id"],
            },
        ),
        Tool(
            name="get_account_activities",
            description=(
                "Get recent tasks and events logged against a Salesforce account. "
                "Returns subject, type, status, and date. Good for understanding recent "
                "engagement before a call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account_id": {
                        "type": "string",
                        "description": "Salesforce account ID (18-char)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max activities to return (default 20)",
                    },
                },
                "required": ["account_id"],
            },
        ),
        Tool(
            name="soql_query",
            description=(
                "Run an arbitrary SOQL query against Salesforce. Use for custom lookups "
                "not covered by other tools. Returns raw records as JSON."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Full SOQL query string, e.g. SELECT Id, Name FROM Account LIMIT 10",
                    },
                },
                "required": ["query"],
            },
        ),
    ]


# ── Tool implementations ──────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        client, _ = _client()
        with client:
            if name == "search_accounts":
                return _search_accounts(client, arguments)
            if name == "get_account":
                return _get_account(client, arguments)
            if name == "get_account_opportunities":
                return _get_account_opportunities(client, arguments)
            if name == "get_account_contacts":
                return _get_account_contacts(client, arguments)
            if name == "get_opportunity":
                return _get_opportunity(client, arguments)
            if name == "get_account_activities":
                return _get_account_activities(client, arguments)
            if name == "soql_query":
                return _soql_query(client, arguments)
        raise ValueError(f"Unknown tool: {name}")
    except Exception as e:
        return [TextContent(type="text", text=f"Salesforce error: {type(e).__name__}: {str(e)}")]


def _search_accounts(client: httpx.Client, args: dict) -> list[TextContent]:
    limit = min(int(args.get("limit", 20)), 50)
    name = args["name"].replace("'", "\\'")
    query = (
        f"SELECT Id, Name, Industry, Website, AnnualRevenue, NumberOfEmployees, Owner.Name "
        f"FROM Account WHERE Name LIKE '%{name}%' ORDER BY Name LIMIT {limit}"
    )
    records = _soql(client, query)
    cleaned = [
        {
            "id": r.get("Id"),
            "name": r.get("Name"),
            "industry": r.get("Industry"),
            "website": r.get("Website"),
            "annual_revenue": r.get("AnnualRevenue"),
            "employees": r.get("NumberOfEmployees"),
            "owner": r.get("Owner", {}).get("Name") if r.get("Owner") else None,
        }
        for r in records
    ]
    return [TextContent(type="text", text=json.dumps(cleaned, indent=2))]


def _get_account(client: httpx.Client, args: dict) -> list[TextContent]:
    account_id = args["account_id"]
    query = (
        f"SELECT Id, Name, Industry, Website, AnnualRevenue, NumberOfEmployees, "
        f"BillingCity, BillingState, BillingCountry, Owner.Name, CreatedDate, LastModifiedDate "
        f"FROM Account WHERE Id = '{account_id}'"
    )
    records = _soql(client, query)
    if not records:
        return [TextContent(type="text", text=f"No account found with ID {account_id}.")]
    r = records[0]
    result = {
        "id": r.get("Id"),
        "name": r.get("Name"),
        "industry": r.get("Industry"),
        "website": r.get("Website"),
        "annual_revenue": r.get("AnnualRevenue"),
        "employees": r.get("NumberOfEmployees"),
        "billing_city": r.get("BillingCity"),
        "billing_state": r.get("BillingState"),
        "billing_country": r.get("BillingCountry"),
        "owner": r.get("Owner", {}).get("Name") if r.get("Owner") else None,
        "created_date": r.get("CreatedDate"),
        "last_modified": r.get("LastModifiedDate"),
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _get_account_opportunities(client: httpx.Client, args: dict) -> list[TextContent]:
    account_id = args["account_id"]
    include_closed = args.get("include_closed", False)
    closed_filter = "" if include_closed else "AND IsClosed = false"
    query = (
        f"SELECT Id, Name, StageName, Amount, CloseDate, Type, LeadSource, "
        f"Owner.Name, CreatedDate, LastModifiedDate "
        f"FROM Opportunity WHERE AccountId = '{account_id}' {closed_filter} "
        f"ORDER BY CloseDate DESC LIMIT 50"
    )
    records = _soql(client, query)
    cleaned = [
        {
            "id": r.get("Id"),
            "name": r.get("Name"),
            "stage": r.get("StageName"),
            "amount": r.get("Amount"),
            "close_date": r.get("CloseDate"),
            "type": r.get("Type"),
            "lead_source": r.get("LeadSource"),
            "owner": r.get("Owner", {}).get("Name") if r.get("Owner") else None,
        }
        for r in records
    ]
    return [TextContent(type="text", text=json.dumps(cleaned, indent=2))]


def _get_account_contacts(client: httpx.Client, args: dict) -> list[TextContent]:
    account_id = args["account_id"]
    limit = min(int(args.get("limit", 25)), 100)
    query = (
        f"SELECT Id, FirstName, LastName, Title, Email, Phone, LastActivityDate "
        f"FROM Contact WHERE AccountId = '{account_id}' "
        f"ORDER BY LastActivityDate DESC NULLS LAST LIMIT {limit}"
    )
    records = _soql(client, query)
    cleaned = [
        {
            "id": r.get("Id"),
            "name": f"{r.get('FirstName', '')} {r.get('LastName', '')}".strip(),
            "title": r.get("Title"),
            "email": r.get("Email"),
            "phone": r.get("Phone"),
            "last_activity": r.get("LastActivityDate"),
        }
        for r in records
    ]
    return [TextContent(type="text", text=json.dumps(cleaned, indent=2))]


def _get_opportunity(client: httpx.Client, args: dict) -> list[TextContent]:
    opp_id = args["opportunity_id"]
    query = (
        f"SELECT Id, Name, AccountId, Account.Name, StageName, Amount, CloseDate, "
        f"Type, LeadSource, NextStep, Description, Owner.Name, CreatedDate, LastModifiedDate "
        f"FROM Opportunity WHERE Id = '{opp_id}'"
    )
    records = _soql(client, query)
    if not records:
        return [TextContent(type="text", text=f"No opportunity found with ID {opp_id}.")]
    r = records[0]
    result = {
        "id": r.get("Id"),
        "name": r.get("Name"),
        "account_id": r.get("AccountId"),
        "account_name": r.get("Account", {}).get("Name") if r.get("Account") else None,
        "stage": r.get("StageName"),
        "amount": r.get("Amount"),
        "close_date": r.get("CloseDate"),
        "type": r.get("Type"),
        "lead_source": r.get("LeadSource"),
        "next_step": r.get("NextStep"),
        "description": r.get("Description"),
        "owner": r.get("Owner", {}).get("Name") if r.get("Owner") else None,
        "created_date": r.get("CreatedDate"),
        "last_modified": r.get("LastModifiedDate"),
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _get_account_activities(client: httpx.Client, args: dict) -> list[TextContent]:
    account_id = args["account_id"]
    limit = min(int(args.get("limit", 20)), 50)
    query = (
        f"SELECT Id, Subject, Type, Status, ActivityDate, Owner.Name, Description "
        f"FROM Task WHERE AccountId = '{account_id}' "
        f"ORDER BY ActivityDate DESC NULLS LAST LIMIT {limit}"
    )
    records = _soql(client, query)
    cleaned = [
        {
            "id": r.get("Id"),
            "subject": r.get("Subject"),
            "type": r.get("Type"),
            "status": r.get("Status"),
            "date": r.get("ActivityDate"),
            "owner": r.get("Owner", {}).get("Name") if r.get("Owner") else None,
        }
        for r in records
    ]
    return [TextContent(type="text", text=json.dumps(cleaned, indent=2))]


def _soql_query(client: httpx.Client, args: dict) -> list[TextContent]:
    records = _soql(client, args["query"])
    return [TextContent(type="text", text=json.dumps(records, indent=2))]


# ── SSE transport + app ───────────────────────────────────────────────────────

sse = SseServerTransport("/messages/")


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    path = scope["path"]
    method = scope["method"]
    if path == "/health":
        await Response("ok")(scope, receive, send)
    elif path in ("/sse", "/sse/"):
        if method == "GET":
            async with sse.connect_sse(scope, receive, send) as streams:
                await server.run(
                    streams[0],
                    streams[1],
                    server.create_initialization_options(),
                )
        else:
            await Response(status_code=405)(scope, receive, send)
    elif path.startswith("/messages/"):
        await sse.handle_post_message(scope, receive, send)
    else:
        await Response(status_code=404)(scope, receive, send)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8002))
    uvicorn.run(app, host="0.0.0.0", port=port)
