#!/usr/bin/env python3
"""Post the pr-crew/coverage commit status from CI (stdlib only).

Reads the cumulative coverage percent from coverage/summary.json (emitted by
shared/coverage.py) and POSTs it as a Gitea commit status (context=
pr-crew/coverage) on $GITHUB_SHA using the auto $GITHUB_TOKEN.

Modeled on projdash/ci/post-coverage-status.py — same status-post shape so
the percent shows up as its own check line on the PR (the workflow job's
own status line cannot be re-described per-run).

On any read/parse failure it posts state=error and exits 0 so an
`if: always()` step does not double-fail the job. A POST/network failure
DOES raise. Outside CI (no GITHUB_* env), it prints and skips cleanly.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SUMMARY = ROOT / "coverage" / "summary.json"


def _ssl_context() -> ssl.SSLContext:
    """Build the SSL context for the status POST.

    Default: full verification against the system trust store (the secure
    path). The bearer token MUST NOT cross an unverified TLS channel —
    that's exactly the surface a MITM would exploit.

    Two explicit opt-outs for self-signed deployments (e.g. the llamabox
    Gitea instance with its mkcert cert):
      - GITEA_CA_BUNDLE=/path/to/root.pem : pin the self-signed root
      - GITEA_TLS_INSECURE=1              : disable verification entirely
        (loud warning; only set this if you've already accepted the
        token-leak risk on a fully-trusted network)

    On the local-ci Linux runner the mounted mkcert CA is exposed via
    CURL_CA_BUNDLE / NODE_EXTRA_CA_CERTS (#33); those are honored as
    fallbacks after GITEA_CA_BUNDLE, so the poster verifies in CI with no
    extra config and never needs GITEA_TLS_INSECURE there.
    """
    if os.environ.get("GITEA_TLS_INSECURE") == "1":
        print("post-coverage-status: WARNING — TLS verification disabled "
              "(GITEA_TLS_INSECURE=1). Bearer token is being sent over an "
              "unverified channel.", file=sys.stderr)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    cafile = (
        os.environ.get("GITEA_CA_BUNDLE")
        or os.environ.get("CURL_CA_BUNDLE")
        or os.environ.get("NODE_EXTRA_CA_CERTS")
        or None
    )
    return ssl.create_default_context(cafile=cafile)


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
    urllib.request.urlopen(request, context=_ssl_context()).read()


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
