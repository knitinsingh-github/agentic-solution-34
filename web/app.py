"""
IT Helpdesk Triage Agent — FastAPI web server

Run with:
    uvicorn web.app:app --reload --port 8000
"""
import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(override=True)

app = FastAPI(title="IT Helpdesk Triage Agent")

_executor = ThreadPoolExecutor(max_workers=4)

DATA_DIR = Path(__file__).parent.parent / "data"
LOG_DIR  = Path(__file__).parent.parent / "logs"


class TriageRequest(BaseModel):
    text: str
    requester: str = "user@company.internal"
    channel: str = "web-form"
    hitl_mode: str = "auto-approve"


# ── User view ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (Path(__file__).parent / "index.html").read_text()


@app.post("/triage")
async def triage_stream(req: TriageRequest):
    from agent.intake_agent import run_triage

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_event(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def run_sync() -> None:
        try:
            run_triage(
                request_text=req.text,
                requester_email=req.requester,
                channel=req.channel,
                hitl_mode=req.hitl_mode,
                verbose=False,
                on_event=on_event,
            )
        except Exception as exc:
            loop.call_soon_threadsafe(
                queue.put_nowait, {"type": "error", "message": str(exc)}
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    loop.run_in_executor(_executor, run_sync)

    async def event_stream():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Admin view ───────────────────────────────────────────────────────────────

@app.get("/admin/stats")
async def admin_stats():
    try:
        with open(DATA_DIR / "ticket_queue.json") as f:
            tickets = json.load(f).get("tickets", [])

        priority_counts = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
        queue_counts: dict = {}
        hitl_count = 0
        auto_count = 0

        for t in tickets:
            p = t.get("priority", "")
            if p in priority_counts:
                priority_counts[p] += 1
            q = t.get("queue", "unknown")
            queue_counts[q] = queue_counts.get(q, 0) + 1
            if t.get("human_approval_obtained"):
                hitl_count += 1
            if t.get("auto_resolve"):
                auto_count += 1

        return {
            "total": len(tickets),
            "priority_counts": priority_counts,
            "queue_counts": dict(sorted(queue_counts.items(), key=lambda x: -x[1])),
            "hitl_count": hitl_count,
            "auto_count": auto_count,
        }
    except Exception as exc:
        return {"error": str(exc), "total": 0, "priority_counts": {}, "queue_counts": {}, "hitl_count": 0, "auto_count": 0}


@app.get("/admin/tickets")
async def admin_tickets():
    try:
        with open(DATA_DIR / "ticket_queue.json") as f:
            tickets = json.load(f).get("tickets", [])
        return {"tickets": list(reversed(tickets[-30:]))}
    except Exception as exc:
        return {"error": str(exc), "tickets": []}


@app.get("/admin/hitl")
async def admin_hitl():
    try:
        log_file = LOG_DIR / "hitl_decisions.jsonl"
        entries = []
        if log_file.exists():
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        return {"entries": list(reversed(entries[-30:]))}
    except Exception as exc:
        return {"error": str(exc), "entries": []}


@app.get("/admin/evals")
async def admin_evals():
    result = {}
    for eval_type in ["golden", "adversarial"]:
        eval_file = LOG_DIR / f"eval_{eval_type}.json"
        if eval_file.exists():
            with open(eval_file) as f:
                result[eval_type] = json.load(f)
    return result
