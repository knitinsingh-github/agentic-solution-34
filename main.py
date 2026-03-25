#!/usr/bin/env python3
"""
IT Helpdesk Triage Agent — Demo Harness

Usage:
  python main.py                    # Run demo batch of 5 requests
  python main.py --interactive      # Process requests from stdin
  python main.py --eval golden      # Run against golden dataset
  python main.py --eval adversarial # Run adversarial eval set
  python main.py --hitl auto-approve # Run demo with auto-approved HITL
"""

import os
import sys
import json
import argparse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Demo requests — a diverse batch representing real inbound volume
# ---------------------------------------------------------------------------

DEMO_REQUESTS = [
    # P4 — auto-resolve (password reset)
    {
        "text": "Hi, I forgot my password and can't log into my computer this morning.",
        "requester": "frank.garcia@company.internal",
        "channel": "email",
        "label": "P4 auto-resolve: password reset",
    },
    # P3 — route to desktop-support
    {
        "text": (
            "My laptop fan is extremely loud and the machine runs hot even when idle. "
            "I've restarted twice today. Not sure if it's a hardware issue."
        ),
        "requester": "alice.johnson@company.internal",
        "channel": "slack",
        "label": "P3 desktop-support: hardware issue",
    },
    # P1 — infrastructure (requires HITL)
    {
        "text": (
            "URGENT: Our production database (SRV-PROD-DB-01) is not responding. "
            "All customer-facing services are throwing 500 errors. "
            "Started approximately 5 minutes ago. Multiple engineers confirming."
        ),
        "requester": "henry.park@company.internal",
        "channel": "web-form",
        "label": "P1 infrastructure: production DB down (HITL required)",
    },
    # P2 — security (requires HITL)
    {
        "text": (
            "I just got an email from 'IT-Support <helpdesk@company-helpdesk.net>' "
            "asking me to verify my credentials by clicking a link. "
            "I didn't click anything but it looked very convincing. Should I be worried?"
        ),
        "requester": "carol.white@company.internal",
        "channel": "email",
        "label": "P2 security: phishing report (HITL required)",
    },
    # P3 — application-support
    {
        "text": (
            "The HR self-service portal keeps timing out when I try to submit my timesheet. "
            "It's been happening since yesterday. I can access other internal sites fine."
        ),
        "requester": "frank.garcia@company.internal",
        "channel": "web-form",
        "label": "P3 application-support: HR portal timeout",
    },
]


# ---------------------------------------------------------------------------
# Eval runner
# ---------------------------------------------------------------------------

def run_eval(eval_type: str, hitl_mode: str) -> None:
    from agent.intake_agent import run_triage

    eval_file = Path(__file__).parent / "evals" / f"{eval_type}_dataset.json"
    if not eval_file.exists():
        eval_file = Path(__file__).parent / "evals" / f"{eval_type}_set.json"
    if not eval_file.exists():
        print(f"ERROR: eval file not found: {eval_file}")
        sys.exit(1)

    with open(eval_file) as f:
        data = json.load(f)

    cases = data["cases"]
    print(f"\n{'='*60}")
    print(f"Running {eval_type} eval: {len(cases)} cases")
    print(f"HITL mode: {hitl_mode}")
    print(f"{'='*60}\n")

    results = []
    for case in cases:
        print(f"\n[{case['id']}] {case.get('category', '')} — {case.get('label', case.get('threat', ''))}")
        print(f"  Request: {case['request'][:120]}...")

        result = run_triage(
            request_text=case["request"],
            requester_email=case.get("requester", "unknown"),
            channel="eval",
            hitl_mode=hitl_mode,
            verbose=False,
        )

        # Score against expected
        expected_priority = case.get("expected_priority") or case.get("priority")
        expected_queue = case.get("expected_queue") or case.get("queue")
        expected_hitl = case.get("hitl_required", case.get("expected_hitl", False))

        priority_match = result.get("priority") == expected_priority
        queue_match = (
            result.get("queue") == expected_queue
            or result.get("queue") in case.get("expected_queue_options", [])
        )

        status = "✅ PASS" if (priority_match and queue_match) else "❌ FAIL"
        print(f"  Expected: {expected_priority} → {expected_queue}")
        print(f"  Got:      {result.get('priority')} → {result.get('queue')}")
        print(f"  {status}")

        results.append({
            "id": case["id"],
            "pass": priority_match and queue_match,
            "priority_match": priority_match,
            "queue_match": queue_match,
            "expected_priority": expected_priority,
            "got_priority": result.get("priority"),
            "expected_queue": expected_queue,
            "got_queue": result.get("queue"),
        })

    # Summary
    passed = sum(1 for r in results if r["pass"])
    priority_accuracy = sum(1 for r in results if r["priority_match"]) / len(results)
    queue_accuracy = sum(1 for r in results if r["queue_match"]) / len(results)

    print(f"\n{'='*60}")
    print(f"EVAL RESULTS: {eval_type}")
    print(f"{'─'*60}")
    print(f"  Overall pass rate  : {passed}/{len(results)} ({passed/len(results):.0%})")
    print(f"  Priority accuracy  : {priority_accuracy:.0%}")
    print(f"  Queue accuracy     : {queue_accuracy:.0%}")
    print(f"{'='*60}\n")

    # Save results
    output_file = Path(__file__).parent / "logs" / f"eval_{eval_type}.json"
    output_file.parent.mkdir(exist_ok=True)
    with open(output_file, "w") as f:
        json.dump({
            "eval_type": eval_type,
            "total": len(results),
            "passed": passed,
            "priority_accuracy": priority_accuracy,
            "queue_accuracy": queue_accuracy,
            "results": results,
        }, f, indent=2)
    print(f"Results saved to {output_file}")


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def run_interactive(hitl_mode: str) -> None:
    from agent.intake_agent import run_triage

    print("\nIT Helpdesk Triage Agent — Interactive Mode")
    print("Enter requests (empty line to finish, Ctrl+C to exit)")
    print("Format: [email] <request text>  OR just the request text")
    print("─" * 50)

    while True:
        try:
            raw = input("\nRequest> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if not raw:
            continue

        # Parse optional email prefix: [alice@co.com] ticket text
        requester = "user@company.internal"
        if raw.startswith("[") and "]" in raw:
            end = raw.index("]")
            requester = raw[1:end]
            raw = raw[end + 1:].strip()

        run_triage(
            request_text=raw,
            requester_email=requester,
            channel="interactive",
            hitl_mode=hitl_mode,
            verbose=True,
        )


# ---------------------------------------------------------------------------
# Demo batch
# ---------------------------------------------------------------------------

def run_demo(hitl_mode: str) -> None:
    from agent.intake_agent import run_triage

    print("\n" + "=" * 60)
    print("IT Helpdesk Triage Agent — Demo")
    print(f"HITL mode: {hitl_mode}")
    print("=" * 60)
    print(f"Running {len(DEMO_REQUESTS)} demo requests...\n")

    for i, req in enumerate(DEMO_REQUESTS, 1):
        print(f"\n[{i}/{len(DEMO_REQUESTS)}] {req['label']}")
        run_triage(
            request_text=req["text"],
            requester_email=req["requester"],
            channel=req["channel"],
            hitl_mode=hitl_mode,
            verbose=True,
        )

    print("\nDemo complete. See logs/ for decision audit trail.")
    print("  logs/decisions.jsonl      — all routed tickets")
    print("  logs/hitl_decisions.jsonl — human approval decisions")
    print("  logs/agent_decisions.jsonl— full reasoning chains")
    print("  data/ticket_queue.json    — created tickets")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="IT Helpdesk Triage Agent")
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Interactive mode — enter requests from stdin",
    )
    parser.add_argument(
        "--eval",
        choices=["golden", "adversarial"],
        help="Run against an eval dataset",
    )
    parser.add_argument(
        "--hitl",
        choices=["interactive", "auto-approve", "auto-deny"],
        default="auto-approve",
        help="HITL approval mode (default: auto-approve for non-interactive demo)",
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        print("Copy .env.example to .env and add your key.")
        sys.exit(1)

    if args.eval:
        run_eval(args.eval, args.hitl)
    elif args.interactive:
        run_interactive("interactive")
    else:
        run_demo(args.hitl)


if __name__ == "__main__":
    main()
