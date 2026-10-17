"""Single-command entry point for the complete MSC-to-local-QA workflow.

This file is intentionally a thin runner: it calls the project's existing
scanner, capture adapter, synthetic-data generators, local mock, template
renderer, and report writer instead of keeping duplicate copies of them.
It must be run from a checkout containing the rest of the CruiseIntel project.

This workflow performs a read-only MSC booking scan. It does not open the
live status-match form, look up a Carnival account, or submit anything.
Synthetic contact values are for local QA only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from MSC_Status_Match_Toolkit.msc_status_match_workflow import (  # noqa: E402
    run_end_to_end_local_qa,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Freshly scan an MSC booking, validate one passenger, then run "
            "synthetic QA only against local status-match and GoCCL-template previews."
        ),
        epilog=(
            "The live MSC status-match form and Carnival account lookup are never "
            "opened. The output directory must be private and outside the project."
        ),
    )
    parser.add_argument("--booking", required=True, help="6-9 digit MSC booking ID")
    parser.add_argument(
        "--passenger-index",
        required=True,
        type=int,
        help="one-based passenger index from the fresh booking scan",
    )
    parser.add_argument(
        "--state",
        default="Oklahoma",
        help="US state for generated local-QA contact data (default: Oklahoma)",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="private parent directory outside the project; each run gets its own folder",
    )
    parser.add_argument(
        "--inbox-wait-seconds",
        default=10,
        type=int,
        help="limit for QA test-inbox polling (0-120; default: 10)",
    )
    parser.add_argument(
        "--login-timeout-minutes",
        default=15,
        type=float,
        help="MSC login wait limit (0-60; default: 15)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(
            run_end_to_end_local_qa(
                booking_id=args.booking,
                passenger_index=args.passenger_index,
                state=args.state,
                output_dir=args.output_dir,
                inbox_wait_seconds=args.inbox_wait_seconds,
                login_timeout_minutes=args.login_timeout_minutes,
            )
        )
    except (
        OSError,
        ValueError,
        LookupError,
        RuntimeError,
        subprocess.CalledProcessError,
    ) as exc:
        parser.error(str(exc))
        return 2

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("status") == "completed_local_qa_not_submitted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
