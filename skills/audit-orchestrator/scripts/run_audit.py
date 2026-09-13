#!/usr/bin/env python3
"""Audit Orchestrator — Executable Entrypoint.

Composes specialist audit skills and the bundled deterministic runtime
to perform a read-only audit of a target website and emit a structured JSON report.

Conforms to the Adobe University Hackathon 2026 Round 3 and Agent Skills specification.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Union

# Ensure runtime directory containing 'src' is available in sys.path
_RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

from src.audits.audit_request import AuditContext, AuditRequest
from src.audits.models import Finding
from src.audits.reporting import AuditReport
from src.orchestrator import (
    AuditOptions,
    AuditResult,
    run_audit as _engine_run_audit,
    run_audit_full as _engine_run_audit_full,
)

__all__ = [
    "AuditContext",
    "AuditOptions",
    "AuditReport",
    "AuditRequest",
    "AuditResult",
    "Finding",
    "main",
    "run_audit",
    "run_audit_full",
]


def run_audit(
    url: Union[str, AuditRequest],
    options: Optional[AuditOptions] = None,
) -> AuditResult:
    """Execute the core audit pipeline returning an AuditResult."""
    return _engine_run_audit(url, options)


def run_audit_full(
    url: Union[str, AuditRequest],
    options: Optional[AuditOptions] = None,
) -> tuple[AuditResult, AuditReport]:
    """Execute the complete audit pipeline returning (AuditResult, AuditReport)."""
    return _engine_run_audit_full(url, options)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint for running audits and producing structured reports."""
    parser = argparse.ArgumentParser(
        prog="run_audit.py",
        description="Run deterministic Machine Answerability and Task Readiness audit.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="Target URL to audit (e.g. https://example.com/)",
    )
    parser.add_argument(
        "--url",
        dest="flag_url",
        help="Target URL to audit (alternative to positional argument)",
    )
    parser.add_argument(
        "--intent",
        "-i",
        dest="task_intent",
        default=None,
        help="User or agent task intent (e.g. 'purchase running shoes')",
    )
    parser.add_argument(
        "--description",
        "-d",
        dest="task_description",
        default=None,
        help="Detailed task description context",
    )
    parser.add_argument(
        "--objectives",
        dest="target_objectives",
        default=None,
        help="Comma-separated list of target objectives (e.g. 'transaction_completion,price_discovery')",
    )
    parser.add_argument(
        "--audits",
        dest="audits",
        default=None,
        help="Comma-separated list of specific audits to run",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--user-agent",
        dest="user_agent",
        default=None,
        help="Custom User-Agent string",
    )
    parser.add_argument(
        "--no-robots",
        dest="no_robots",
        action="store_true",
        help="Disable robots.txt checking (enabled by default)",
    )
    parser.add_argument(
        "--details",
        dest="include_details",
        action="store_true",
        help="Include full diagnostic details in finding outputs",
    )
    parser.add_argument(
        "--output",
        "-o",
        dest="output_file",
        type=Path,
        default=None,
        help="Path to save the JSON audit report (defaults to stdout)",
    )

    args = parser.parse_args(argv)
    target_url = args.url or args.flag_url
    if not target_url:
        parser.error("A target URL must be provided.")

    target_objectives = None
    if args.target_objectives:
        target_objectives = [obj.strip() for obj in args.target_objectives.split(",") if obj.strip()]

    selected_audits = None
    if args.audits:
        selected_audits = [a.strip() for a in args.audits.split(",") if a.strip()]

    context = None
    if args.task_intent or args.task_description or target_objectives:
        context = AuditContext(
            task_intent=args.task_intent,
            task_description=args.task_description,
            target_objectives=target_objectives or [],
        )

    request = AuditRequest(
        target_url=target_url,
        context=context,
        audits=tuple(selected_audits) if selected_audits else None,
        timeout_seconds=args.timeout,
        user_agent=args.user_agent,
        include_details=args.include_details,
    )
    options = AuditOptions(
        check_robots=not args.no_robots,
        include_details=args.include_details,
    )

    result, report = run_audit_full(request, options=options)
    report_json = report.to_json(indent=2)

    if args.output_file:
        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        args.output_file.write_text(report_json, encoding="utf-8")
        print(f"Audit report saved to {args.output_file}", file=sys.stderr)
    else:
        try:
            print(report_json)
        except UnicodeEncodeError:
            sys.stdout.buffer.write(report_json.encode("utf-8") + b"\n")

    return 0 if result.is_success else 1


if __name__ == "__main__":
    sys.exit(main())
