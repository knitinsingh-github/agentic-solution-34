# IT Helpdesk Triage Agent

An agentic IT support triage system built with the Anthropic Python SDK. It classifies inbound support requests by priority and queue, enforces a multi-layer human-in-the-loop (HITL) safety brake, and ships a real-time streaming web interface — all built for the Scenario 5 hackathon challenge.

---

## The Problem

IT helpdesks drown in noise. Every ticket needs a human to decide: *How urgent is this? Who owns it?*

That's a classification problem. It's also a trust problem — you can't let an LLM route a potential ransomware incident or silently escalate a sticky keyboard to P1 just because someone claimed to be a VP.

This agent does the classification, enforces the trust boundaries, and streams its reasoning back to whoever is watching.

---

## What This Builds

```
Inbound ticket  →  Knowledge base search
                →  User/asset lookup
                →  Priority + queue reasoning (logged verbatim)
                →  HITL gate (auto / interactive / deny)
                →  route_ticket tool call
                →  Append-only audit trail
```

- **Priority levels**: P1 (critical, ≤15 min SLA) → P4 (low, 72 h SLA)
- **Queues**: security · infrastructure · networking · desktop-support · application-support · service-desk · auto-resolved
- **HITL modes**: `interactive` (human decides) · `auto-approve` · `auto-deny`
- **Real-time web UI**: SSE stream of reasoning + tool calls + safety blocks

---

## Architecture

### The Agentic Loop

```
┌─────────────────────────────────────────────────────────┐
│  run_triage()  ←  FastAPI thread pool                   │
│                                                         │
│  messages = [user prompt]                               │
│                                                         │
│  for turn in range(MAX_TURNS=12):                       │
│    response = client.messages.create(...)               │
│    emit reasoning text  →  on_event callback            │
│    for each tool_use block:                             │
│      ┌─────────────────────────────────────────────┐   │
│      │  needs_human_approval()  ← HITL gate        │   │
│      │  if blocked → inject BLOCKED result         │   │
│      │  else → dispatch(tool_name, tool_input)     │   │
│      └─────────────────────────────────────────────┘   │
│      emit tool result  →  on_event callback             │
│    if stop_reason == "end_turn": break                  │
│                                                         │
│  log.save()  →  agent_decisions.jsonl                   │
└─────────────────────────────────────────────────────────┘
```

### Three-Layer Safety Brake

| Layer | Where | Mechanism |
|-------|-------|-----------|
| **1. System prompt** | LLM context | Hard rules: never route P1 without approval, ignore injected instructions, etc. |
| **2. Pre-execution gate** | `needs_human_approval()` in agent loop | Inspects every `route_ticket` call before it executes; blocks P1 and security queue if `human_approval_obtained=False` |
| **3. Tool implementation** | `route_ticket()` in `agent/tools.py` | Validates inputs, rejects contradictory flags, writes audit entry |

The gate fires *before* the tool runs — the equivalent of a `PreToolUse` hook implemented manually in the agentic loop.

### HITL Modes

| Mode | Behaviour |
|------|-----------|
| `interactive` | Prompts operator at terminal before each gated action |
| `auto-approve` | Grants approval automatically (CI / demo use) |
| `auto-deny` | Always denies gated actions (safest, for testing brakes) |

Set via `HITL_MODE` env var or per-request `hitl_mode` field in the web API.

### Tools

| Tool | Purpose |
|------|---------|
| `search_knowledge_base` | Looks up KB articles by keyword |
| `check_user_or_asset` | Retrieves user profile + group membership |
| `request_human_approval` | Asks operator to approve/reject before routing |
| `route_ticket` | Creates ticket, writes to queue, logs HITL decision |

---

## Evaluation

Two harnesses, run against the same agent with `auto-approve` HITL mode:

### Golden Dataset — 30 cases

```
Overall pass rate  : 22/30  (73%)
Priority accuracy  : 83%
Queue accuracy     : 73%
```

Fails concentrated in ambiguous-routing cases (edge conditions intentionally left hard).

### Adversarial Dataset — 12 cases

```
Overall pass rate  : 8/12  (67%)
Priority accuracy  : 83%
Queue accuracy     : 75%
```

Notable passes:
- ✅ Prompt injection in ticket body (direct override attempt)
- ✅ Ransomware hidden in benign language → P1 security
- ✅ Data exfiltration via "help request" → P4 service-desk
- ✅ Phishing credential compromise → P1 security

Notable fails:
- ❌ Social engineering priority: CFO impersonation rated P3 instead of P2
- ❌ Payroll system on payday: rated P1/infrastructure vs expected P2/application-support

---

## Running It

### Prerequisites

```bash
pip install -r requirements.txt
cp .env.example .env           # add ANTHROPIC_API_KEY
```

### Web UI (recommended)

```bash
uvicorn web.app:app --reload --port 8000
# Open http://localhost:8000
```

### CLI

```bash
python main.py
```

### Evals

```bash
python evals/run_golden_eval.py
python evals/run_adversarial_eval.py
```

---

## Project Structure

```
agentic-solution-34/
├── agent/
│   ├── intake_agent.py     # Agentic loop, HITL gate, decision logger
│   └── tools.py            # Tool implementations + TOOL_SCHEMAS
├── data/
│   ├── knowledge_base.json # KB articles (KB-001 … KB-010)
│   ├── ticket_queue.json   # Append-only ticket store
│   └── users.json          # User profiles + groups
├── evals/
│   ├── run_golden_eval.py      # 30-case golden harness
│   ├── run_adversarial_eval.py # 12-case adversarial harness
│   ├── golden_cases.json
│   └── adversarial_cases.json
├── logs/
│   ├── agent_decisions.jsonl   # Full reasoning + tool chain per request
│   ├── hitl_decisions.jsonl    # Every HITL approval/denial
│   ├── routing_events.jsonl    # Every route_ticket attempt
│   ├── eval_golden.json        # Last golden eval results
│   └── eval_adversarial.json   # Last adversarial eval results
├── web/
│   ├── app.py      # FastAPI server + SSE streaming
│   └── index.html  # Single-page app (user view + admin dashboard)
├── main.py         # CLI entry point
├── requirements.txt
└── CLAUDE.md       # How to work with this codebase
```

---

## Architecture Decision Records

### ADR-001: Manual agentic loop over SDK tool runner

**Decision**: Implement the loop manually (`for turn in range(MAX_TURNS)`) instead of using the SDK's beta tool runner.

**Why**: The HITL brake needs to intercept every tool call *before* execution, inspect its parameters, and optionally veto it. The SDK tool runner executes tools automatically with no injection point. The manual loop gives us a `needs_human_approval()` gate that fires between parsing the tool-use block and calling `dispatch()`.

### ADR-002: Defence-in-depth for safety

**Decision**: Three independent layers (system prompt + pre-execution gate + tool implementation) rather than trusting any single layer.

**Why**: LLMs can be prompted around; prompt injection is real (see adversarial eval). Each layer has different attack surface. The gate is deterministic Python — it cannot be reasoned around by the model.

### ADR-003: `HITL_MODE` via environment variable

**Decision**: Pass HITL mode as an env var (`os.environ["HITL_MODE"] = hitl_mode`) rather than threading it through every function signature.

**Why**: `request_human_approval()` runs deep inside the tool dispatcher. Env var avoids plumbing the mode through 4 layers of function calls, and mirrors how real config flags propagate in production (12-factor apps).

### ADR-004: Append-only JSONL for all logs

**Decision**: All audit trails (`agent_decisions.jsonl`, `hitl_decisions.jsonl`, `routing_events.jsonl`) are append-only JSONL files.

**Why**: Append-only means writes never corrupt existing entries. JSONL is grep-friendly and streams well. No database dependency — the agent runs standalone. Entries are immutable, which is what regulators and auditors want.

---

## What's Next

### Near-term (weeks)

- **Slack / email ingestion** — ingest tickets from Slack channels and email via webhooks; currently supports only web-form and CLI
- **Ticket deduplication** — detect when a new request duplicates an open ticket and merge instead of creating a new one
- **Confidence calibration** — the 0.70 threshold is hardcoded; measure actual accuracy vs confidence score to find the right cutoff empirically

### Medium-term (months)

- **RAG over the knowledge base** — replace exact-match KB search with vector search so "my Outlook keeps crashing" matches KB-008 (email troubleshooting) even without the word "Outlook"
- **Multi-tenant mode** — per-organisation knowledge bases, user directories, and queue configurations
- **Feedback loop** — let human agents mark tickets as mis-routed; feed corrections back into the golden eval set automatically

### Long-term

- **Active learning** — periodically re-run evals and surface cases where the model has degraded; flag for fine-tuning or prompt revision
- **SLA enforcement** — integrate with PagerDuty/OpsGenie to auto-escalate P1s that haven't been acknowledged within 15 minutes
