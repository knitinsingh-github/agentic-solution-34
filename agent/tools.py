"""
Custom tools for the IT Helpdesk Triage Agent.

Each tool is a plain Python function + a JSON schema definition.
The agentic loop in intake_agent.py dispatches to these functions
when Claude calls them.
"""

import json
import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
LOG_DIR = Path(__file__).parent.parent / "logs"

# ---------------------------------------------------------------------------
# Tool schemas — what Claude sees
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the IT knowledge base for known issues, standard procedures, "
            "and auto-resolution scripts. Always call this first to check if a solution "
            "already exists. Returns matching articles including whether auto-resolution is possible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms describing the issue (e.g. 'password reset', 'VPN not connecting')",
                },
                "category": {
                    "type": "string",
                    "description": "Optional category filter (e.g. 'security', 'networking', 'account')",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_user_or_asset",
        "description": (
            "Look up a user or asset in the IT directory. Use this to verify the requester exists, "
            "find their department and group (executive/legal/standard), and check assigned hardware. "
            "Requester group determines escalation rules."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": "Email address, name, or asset ID to look up",
                },
                "lookup_type": {
                    "type": "string",
                    "enum": ["user", "asset"],
                    "description": "'user' for people lookups, 'asset' for hardware/server lookups",
                },
            },
            "required": ["identifier", "lookup_type"],
        },
    },
    {
        "name": "request_human_approval",
        "description": (
            "Pause and request human operator approval before taking a sensitive action. "
            "REQUIRED before route_ticket when: priority is P1, queue is 'security', "
            "confidence < 0.70, or requester group is 'executive' or 'legal'. "
            "Returns APPROVED, DENIED, or OVERRIDE:<new_priority>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposed_priority": {
                    "type": "string",
                    "enum": ["P1", "P2", "P3", "P4"],
                    "description": "The priority you intend to assign",
                },
                "proposed_queue": {
                    "type": "string",
                    "description": "The queue you intend to route to",
                },
                "summary": {
                    "type": "string",
                    "description": "1-2 sentence plain-language summary for the approver",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Your full classification reasoning",
                },
                "confidence": {
                    "type": "number",
                    "description": "Your confidence score 0.0–1.0",
                },
                "trigger_reason": {
                    "type": "string",
                    "description": "Why approval is required: P1 | security-queue | low-confidence | executive | legal | security-signals",
                },
            },
            "required": ["proposed_priority", "proposed_queue", "summary", "confidence", "trigger_reason"],
        },
    },
    {
        "name": "route_ticket",
        "description": (
            "Create and route a classified ticket to the appropriate queue. "
            "For P1 tickets or security queue routing, you MUST call request_human_approval "
            "first and receive APPROVED. For P3/P4 with confidence >= 0.80 and no escalation "
            "triggers, you may route directly. Set auto_resolve=true only when a KB article "
            "provides a complete resolution script."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "priority": {
                    "type": "string",
                    "enum": ["P1", "P2", "P3", "P4"],
                    "description": "Ticket priority",
                },
                "queue": {
                    "type": "string",
                    "enum": [
                        "service-desk",
                        "infrastructure",
                        "security",
                        "desktop-support",
                        "application-support",
                        "networking",
                        "auto-resolved",
                    ],
                    "description": "Target queue",
                },
                "title": {
                    "type": "string",
                    "description": "Short ticket title (max 80 chars)",
                },
                "description": {
                    "type": "string",
                    "description": "Full ticket description for the receiving team",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Classification reasoning (stored in audit log)",
                },
                "confidence": {
                    "type": "number",
                    "description": "Classification confidence 0.0–1.0",
                },
                "requester_email": {
                    "type": "string",
                    "description": "Requester email address",
                },
                "auto_resolve": {
                    "type": "boolean",
                    "description": "True if agent is sending a self-service resolution now",
                },
                "resolution_applied": {
                    "type": "string",
                    "description": "Describe what resolution was sent (if auto_resolve=true)",
                },
                "kb_articles_used": {
                    "type": "string",
                    "description": "Comma-separated KB article IDs referenced",
                },
                "human_approval_obtained": {
                    "type": "boolean",
                    "description": "Set true only after request_human_approval returned APPROVED",
                },
            },
            "required": ["priority", "queue", "title", "description", "reasoning", "confidence"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def search_knowledge_base(query: str, category: str = "") -> str:
    query_lower = query.lower().strip()
    category_lower = category.lower().strip()

    with open(DATA_DIR / "knowledge_base.json") as f:
        kb = json.load(f)

    query_words = set(query_lower.split())
    scored = []
    for article in kb["articles"]:
        title_lower = article["title"].lower()
        content_lower = article["content"].lower()
        keywords_lower = [k.lower() for k in article["keywords"]]

        score = 0
        if query_lower in title_lower:
            score += 4
        if query_lower in content_lower:
            score += 2
        if any(query_lower in kw for kw in keywords_lower):
            score += 3
        for word in query_words:
            if len(word) > 3:
                if word in title_lower:
                    score += 2
                if any(word in kw for kw in keywords_lower):
                    score += 1

        if score > 0:
            if not category_lower or category_lower in article["categories"]:
                scored.append((score, article))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [a for _, a in scored[:3]]

    if not top:
        return f"No KB articles found for: '{query}'. May require manual assessment."

    lines = []
    for a in top:
        lines.append(f"[{a['id']}] {a['title']}")
        lines.append(f"  Categories: {', '.join(a['categories'])}")
        lines.append(f"  Auto-resolve: {a.get('auto_resolve', False)}")
        if a.get("security_incident"):
            lines.append("  ⚠️  SECURITY INCIDENT — escalate to security queue immediately")
        if a.get("escalate_to"):
            lines.append(f"  Recommended queue: {a['escalate_to']}")
        lines.append(f"  Content: {a['content'][:400]}")
        if a.get("resolution_template"):
            lines.append(f"  Resolution template: {a['resolution_template'][:250]}")
        lines.append("")

    return "\n".join(lines)


def check_user_or_asset(identifier: str, lookup_type: str = "user") -> str:
    identifier_lower = identifier.lower().strip()

    with open(DATA_DIR / "user_directory.json") as f:
        directory = json.load(f)

    if lookup_type == "asset":
        for asset in directory["assets"]:
            if identifier_lower in asset["asset_id"].lower() or \
               identifier_lower in asset.get("assigned_to", "").lower():
                return json.dumps(asset, indent=2)
        return f"Asset not found: {identifier}"

    for user in directory["users"]:
        if identifier_lower in user["email"].lower() or \
           identifier_lower in user["name"].lower():
            summary = {
                "name": user["name"],
                "email": user["email"],
                "department": user["department"],
                "title": user["title"],
                "group": user["group"],
                "active": user["active"],
                "location": user["location"],
                "routing_note": directory["groups"].get(user["group"], ""),
            }
            return json.dumps(summary, indent=2)

    return (
        f"User not found in directory: {identifier}\n"
        "Treat as 'unknown' group. Flag if this is unusual for the request type."
    )


def request_human_approval(
    proposed_priority: str,
    proposed_queue: str,
    summary: str,
    confidence: float,
    trigger_reason: str,
    reasoning: str = "",
) -> str:
    import os
    import sys

    hitl_mode = os.environ.get("HITL_MODE", "interactive").lower()

    separator = "─" * 62
    prompt_lines = [
        "",
        separator,
        "🚨  HUMAN APPROVAL REQUIRED",
        separator,
        f"  Trigger  : {trigger_reason}",
        f"  Priority : {proposed_priority}   Queue: {proposed_queue}",
        f"  Confidence: {confidence:.0%}",
        f"  Summary  : {summary}",
    ]
    if reasoning:
        prompt_lines.append("\n  Agent reasoning:")
        for line in reasoning.strip().split("\n")[:6]:
            prompt_lines.append(f"    {line}")
    prompt_lines += [
        "",
        separator,
        "  [A] Approve as-is",
        "  [D] Deny — do not route",
        "  [O:<priority>] Override priority (e.g. O:P2)",
        separator,
    ]

    _log_hitl_request(
        proposed_priority=proposed_priority,
        proposed_queue=proposed_queue,
        confidence=confidence,
        trigger_reason=trigger_reason,
        hitl_mode=hitl_mode,
    )

    if hitl_mode == "auto-approve":
        _log_hitl_decision("AUTO-APPROVED", proposed_priority, proposed_queue)
        return f"APPROVED: (auto-approve mode) {proposed_priority} → {proposed_queue}"

    if hitl_mode == "auto-deny":
        _log_hitl_decision("AUTO-DENIED", proposed_priority, proposed_queue)
        return "DENIED: (auto-deny mode) Action blocked. Do not route this ticket."

    # Interactive — print to stderr so it's visible even in piped scenarios
    print("\n".join(prompt_lines), file=sys.stderr)
    print("  Your decision: ", end="", flush=True, file=sys.stderr)

    try:
        raw = input().strip().upper()
    except (EOFError, KeyboardInterrupt):
        raw = "D"

    if raw.startswith("O:"):
        new_priority = raw[2:].strip()
        _log_hitl_decision(f"OVERRIDE:{new_priority}", proposed_priority, proposed_queue)
        return (
            f"APPROVED: Human operator overrode priority to {new_priority}. "
            f"Use priority={new_priority} when calling route_ticket."
        )
    elif raw in ("A", "APPROVE", "Y", "YES", ""):
        _log_hitl_decision("APPROVED", proposed_priority, proposed_queue)
        return f"APPROVED: {proposed_priority} → {proposed_queue}. Proceed with route_ticket."
    else:
        _log_hitl_decision("DENIED", proposed_priority, proposed_queue)
        return (
            "DENIED: Human operator rejected this routing. "
            "Do not create the ticket. Tell the requester their request is under manual review."
        )


def route_ticket(
    priority: str,
    queue: str,
    title: str,
    description: str,
    reasoning: str,
    confidence: float,
    requester_email: str = "unknown",
    auto_resolve: bool = False,
    resolution_applied: str = "",
    kb_articles_used: str = "",
    human_approval_obtained: bool = False,
) -> str:
    priority = priority.upper()

    # Hard guardrails — defence-in-depth even if system prompt is bypassed
    if priority == "P1" and not human_approval_obtained:
        return (
            "BLOCKED: P1 tickets require human approval first. "
            "Call request_human_approval, then set human_approval_obtained=true."
        )
    if queue == "security" and not human_approval_obtained:
        return (
            "BLOCKED: Security queue routing requires human approval first. "
            "Call request_human_approval, then set human_approval_obtained=true."
        )

    ticket_id = f"INC-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    ticket = {
        "ticket_id": ticket_id,
        "priority": priority,
        "queue": queue,
        "title": title[:120],
        "description": description,
        "reasoning": reasoning,
        "confidence": round(float(confidence), 3),
        "requester_email": requester_email,
        "auto_resolve": auto_resolve,
        "resolution_applied": resolution_applied,
        "kb_articles_used": kb_articles_used,
        "human_approval_obtained": human_approval_obtained,
        "routed_at": datetime.datetime.now().isoformat(),
        "status": "auto_resolved" if auto_resolve else "open",
        "routed_by": "intake-agent-v1",
    }

    # Persist ticket
    queue_file = DATA_DIR / "ticket_queue.json"
    with open(queue_file) as f:
        queue_data = json.load(f)
    queue_data["tickets"].append(ticket)
    with open(queue_file, "w") as f:
        json.dump(queue_data, f, indent=2)

    # Structured decision log
    _write_decision_log(ticket)

    status = "✅ AUTO-RESOLVED" if auto_resolve else f"📋 ROUTED → {queue}"
    return (
        f"{status}\n"
        f"Ticket: {ticket_id} | Priority: {priority} | Confidence: {confidence:.0%}\n"
        + (f"Resolution: {resolution_applied[:120]}" if auto_resolve else f"Queue: {queue} (SLA starts now)")
    )


# ---------------------------------------------------------------------------
# Tool dispatcher — called by the agentic loop
# ---------------------------------------------------------------------------

def dispatch(tool_name: str, tool_input: dict) -> str:
    """Execute a tool by name and return a string result."""
    if tool_name == "search_knowledge_base":
        return search_knowledge_base(
            query=tool_input.get("query", ""),
            category=tool_input.get("category", ""),
        )
    elif tool_name == "check_user_or_asset":
        return check_user_or_asset(
            identifier=tool_input.get("identifier", ""),
            lookup_type=tool_input.get("lookup_type", "user"),
        )
    elif tool_name == "request_human_approval":
        return request_human_approval(
            proposed_priority=tool_input.get("proposed_priority", "P3"),
            proposed_queue=tool_input.get("proposed_queue", ""),
            summary=tool_input.get("summary", ""),
            confidence=float(tool_input.get("confidence", 0.5)),
            trigger_reason=tool_input.get("trigger_reason", ""),
            reasoning=tool_input.get("reasoning", ""),
        )
    elif tool_name == "route_ticket":
        return route_ticket(
            priority=tool_input.get("priority", "P3"),
            queue=tool_input.get("queue", "service-desk"),
            title=tool_input.get("title", "Untitled"),
            description=tool_input.get("description", ""),
            reasoning=tool_input.get("reasoning", ""),
            confidence=float(tool_input.get("confidence", 0.5)),
            requester_email=tool_input.get("requester_email", "unknown"),
            auto_resolve=bool(tool_input.get("auto_resolve", False)),
            resolution_applied=tool_input.get("resolution_applied", ""),
            kb_articles_used=tool_input.get("kb_articles_used", ""),
            human_approval_obtained=bool(tool_input.get("human_approval_obtained", False)),
        )
    else:
        return f"Unknown tool: {tool_name}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_decision_log(ticket: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / "decisions.jsonl", "a") as f:
        f.write(json.dumps(ticket) + "\n")


def _log_hitl_request(**kwargs) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    entry = {"timestamp": datetime.datetime.now().isoformat(), "event": "hitl_request", **kwargs}
    with open(LOG_DIR / "hitl_decisions.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def _log_hitl_decision(outcome: str, priority: str, queue: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "event": "hitl_decision",
        "outcome": outcome,
        "priority": priority,
        "queue": queue,
    }
    with open(LOG_DIR / "hitl_decisions.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")
