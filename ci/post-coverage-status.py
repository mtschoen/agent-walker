#!/usr/bin/env python3
"""Post the pr-crew/coverage commit status from CI (stdlib only).

Reads the cumulative coverage percent from coverage/summary.json (emitted by
shared/coverage.py) and POSTs it as a Gitea commit status (context=
pr-crew/coverage) on $GITHUB_SHA using the auto $GITHUB_TOKEN.

Modeled on projdash/ci/post-coverage-status.py — same status-post shape so
the percent shows up as its own check line on the PR (the workflow job's
own status line cannot be re-described per-run).

The llamabox Gitea instance serves a publicly-trusted Let's Encrypt cert,
so urllib's default verification validates it with no custom CA handling.

On any read/parse failure it posts state=error and exits 0 so an
`if: always()` step does not double-fail the job. A POST/network failure
DOES raise. Outside CI (no GITHUB_* env), it prints and skips cleanly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SUMMARY = ROOT / "coverage" / "summary.json"


def _post(state: str, description: str) -> None:
    server = os.environ["GITHUB_SERVER_URL"]
    repository = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["GITHUB_SHA"]
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    body = json.dumps({
        "context": "pr-crew/coverage",
        "state": state,
        "description": description,
        "target_url": f"{server}/{repository}/actions/runs/{run_id}",
    }).encode()
    request = urllib.request.Request(
        f"{server}/api/v1/repos/{repository}/statuses/{sha}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(request).read()


def _description(summary: dict) -> str:
    pct = summary["cumulative_percent"]
    cov = summary["cumulative_covered"]
    tot = summary["cumulative_total"]
    n = summary["measured_impls"]
    return f"{pct:.2f}% cumulative ({cov}/{tot} pooled across {n} impls)"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", default=str(DEFAULT_SUMMARY),
                        help="path to coverage/summary.json")
    arguments = parser.parse_args(argv[1:])

    # Off-CI guard — running locally without GITHUB_* env should not crash.
    if "GITHUB_SHA" not in os.environ or "GITHUB_TOKEN" not in os.environ:
        print("post-coverage-status: GITHUB_* env not set — skipping (not in CI)")
        return 0

    try:
        summary = json.loads(Path(arguments.summary).read_text())
        description = _description(summary)
    except Exception as error:  # measurement failed -> post error, exit 0
        print(f"coverage summary unreadable: {error}", file=sys.stderr)
        _post("error", "coverage summary unreadable")
        return 0

    _post("success", description)
    print(f"posted pr-crew/coverage success: {description}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
