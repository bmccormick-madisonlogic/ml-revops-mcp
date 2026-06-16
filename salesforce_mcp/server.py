import json
import os
from datetime import date, timedelta

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
        Tool(
            name="get_pipeline_summary",
            description=(
                "Summarize the open sales pipeline across the org. Returns total amount and deal count, "
                "breakdown by stage (count + $), breakdown by rep/owner (count + $, sorted by amount), "
                "and a list of at-risk deals (closing within 45 days with no activity in 30+ days). "
                "Use for forecast calls, pipeline reviews, and executive summaries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["this_quarter", "next_quarter", "all"],
                        "description": "Filter by close date period. Default: this_quarter.",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Custom range start date (YYYY-MM-DD). Only used if period is omitted.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Custom range end date (YYYY-MM-DD). Only used if period is omitted.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_rep_pipeline",
            description=(
                "Get all open opportunities for a specific sales rep by name. Returns each deal's "
                "account, stage, amount, close date, last activity date, and next step. "
                "Useful for rep-level pipeline reviews and 1:1 prep."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "owner_name": {
                        "type": "string",
                        "description": "Rep's name or partial name, e.g. 'Sarah' or 'Smith'.",
                    },
                    "period": {
                        "type": "string",
                        "enum": ["this_quarter", "next_quarter", "all"],
                        "description": "Filter by close date period. Default: all.",
                    },
                },
                "required": ["owner_name"],
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
            if name == "get_pipeline_summary":
                return _get_pipeline_summary(client, arguments)
            if name == "get_rep_pipeline":
                return _get_rep_pipeline(client, arguments)
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


def _get_pipeline_summary(client: httpx.Client, args: dict) -> list[TextContent]:
    period = args.get("period", "this_quarter")
    if period == "this_quarter":
        date_filter = "AND CloseDate = THIS_QUARTER"
    elif period == "next_quarter":
        date_filter = "AND CloseDate = NEXT_QUARTER"
    else:
        start = args.get("start_date")
        end = args.get("end_date")
        if start and end:
            date_filter = f"AND CloseDate >= {start} AND CloseDate <= {end}"
        else:
            date_filter = ""

    query = (
        f"SELECT Id, Name, StageName, Amount, CloseDate, LastActivityDate, "
        f"Owner.Name, Account.Name "
        f"FROM Opportunity WHERE IsClosed = false {date_filter} "
        f"ORDER BY CloseDate ASC LIMIT 500"
    )
    records = _soql(client, query)

    today = date.today()
    stale_cutoff = today - timedelta(days=30)
    at_risk_cutoff = today + timedelta(days=45)

    by_stage = {}
    by_owner = {}
    at_risk = []
    total_amount = 0

    for r in records:
        stage = r.get("StageName") or "Unknown"
        owner = (r.get("Owner") or {}).get("Name") or "Unknown"
        amount = r.get("Amount") or 0
        close_date_str = r.get("CloseDate")
        last_activity_str = r.get("LastActivityDate")

        by_stage.setdefault(stage, {"count": 0, "amount": 0})
        by_stage[stage]["count"] += 1
        by_stage[stage]["amount"] += amount

        by_owner.setdefault(owner, {"count": 0, "amount": 0})
        by_owner[owner]["count"] += 1
        by_owner[owner]["amount"] += amount

        total_amount += amount

        if close_date_str:
            close_date = date.fromisoformat(close_date_str)
            last_activity = date.fromisoformat(last_activity_str) if last_activity_str else None
            is_stale = last_activity is None or last_activity < stale_cutoff
            if close_date <= at_risk_cutoff and is_stale:
                at_risk.append({
                    "id": r.get("Id"),
                    "name": r.get("Name"),
                    "account": (r.get("Account") or {}).get("Name"),
                    "stage": stage,
                    "amount": amount,
                    "close_date": close_date_str,
                    "last_activity": last_activity_str,
                    "owner": owner,
                })

    by_owner_sorted = dict(
        sorted(by_owner.items(), key=lambda x: x[1]["amount"], reverse=True)
    )

    result = {
        "period": period,
        "total_opportunities": len(records),
        "total_amount": total_amount,
        "by_stage": by_stage,
        "by_owner": by_owner_sorted,
        "at_risk": at_risk[:10],
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _get_rep_pipeline(client: httpx.Client, args: dict) -> list[TextContent]:
    owner_name = args["owner_name"].replace("'", "\\'")
    period = args.get("period", "all")
    if period == "this_quarter":
        date_filter = "AND CloseDate = THIS_QUARTER"
    elif period == "next_quarter":
        date_filter = "AND CloseDate = NEXT_QUARTER"
    else:
        date_filter = ""

    query = (
        f"SELECT Id, Name, Account.Name, StageName, Amount, CloseDate, "
        f"LastActivityDate, NextStep, Type "
        f"FROM Opportunity "
        f"WHERE Owner.Name LIKE '%{owner_name}%' AND IsClosed = false {date_filter} "
        f"ORDER BY CloseDate ASC LIMIT 100"
    )
    records = _soql(client, query)

    total_amount = sum(r.get("Amount") or 0 for r in records)
    cleaned = [
        {
            "id": r.get("Id"),
            "name": r.get("Name"),
            "account": (r.get("Account") or {}).get("Name"),
            "stage": r.get("StageName"),
            "amount": r.get("Amount"),
            "close_date": r.get("CloseDate"),
            "last_activity": r.get("LastActivityDate"),
            "next_step": r.get("NextStep"),
            "type": r.get("Type"),
        }
        for r in records
    ]

    result = {
        "rep": owner_name,
        "period": period,
        "total_opportunities": len(records),
        "total_amount": total_amount,
        "opportunities": cleaned,
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


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
