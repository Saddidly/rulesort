from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .models import RuleSortError
from .planning import build_plan, load_plan, save_plan, summary
from .transaction import apply_plan, undo_journal


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if "counts" in payload:
        counts = payload["counts"]
        print(f"Planned: {counts['planned']}  Unchanged: {counts['unchanged']}  Conflicts: {counts['conflict']}  Skipped: {counts['skipped']}")
    for action in payload.get("actions", []):
        if action["status"] == "planned":
            print(f"  {action['source']} -> {action['destination']} [{action['rule']}]")
        elif action["status"] in {"conflict", "skipped"}:
            print(f"  {action['status'].upper()}: {action['source']} -> {action['destination']} ({action['reason']})")
    if "journal" in payload:
        print(f"Journal: {payload['journal']}")
    if "message" in payload:
        print(payload["message"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rulesort", description="Plan safe, rule-based file moves and keep a journal for undo.")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", aliases=["dry-run"], help="build and save a dry-run plan")
    plan.add_argument("config", type=Path, help="JSON rule configuration")
    plan.add_argument("--out", type=Path, default=Path("rulesort-plan.json"), help="where to save the plan")
    plan.add_argument("--json", action="store_true", help="print a JSON report")
    apply = sub.add_parser("apply", help="apply a saved plan")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--allow-partial", action="store_true", help="apply eligible entries even when the plan has conflicts")
    apply.add_argument("--json", action="store_true", help="print a JSON report")
    undo = sub.add_parser("undo", help="undo a completed transaction journal")
    undo.add_argument("journal", type=Path)
    undo.add_argument("--json", action="store_true", help="print a JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in {"plan", "dry-run"}:
            if args.out.resolve() == args.config.resolve():
                raise RuleSortError('plan output cannot replace the configuration')
            plan = build_plan(load_config(args.config))
            save_plan(plan, args.out)
            counts = summary(plan)
            _emit({"counts": counts, "actions": plan["actions"], "plan": str(args.out.resolve())}, args.json)
            return 2 if counts["conflict"] else 0
        if args.command == "apply":
            plan = load_plan(args.plan)
            journal = apply_plan(plan, allow_partial=args.allow_partial)
            _emit({"journal": str(journal), "message": "Apply completed; the journal contains recovery and undo evidence."}, args.json)
            return 0
        if args.command == "undo":
            undo_journal(args.journal)
            _emit({"message": f"Undo completed from {args.journal.resolve()}."}, args.json)
            return 0
    except (RuleSortError, OSError) as exc:
        print(f"rulesort: error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
