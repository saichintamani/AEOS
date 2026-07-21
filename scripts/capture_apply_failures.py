#!/usr/bin/env python3
"""Milestone 14.2 apply-failure capture harness.

Parses a `terraform apply` log and extracts the failure classes that a first
real apply typically surfaces, then maps them back to concrete deploy-policy
fixes. The point of 14.2 is *these findings*, not a green apply -- so this
turns raw log noise into an actionable list.

Extracted classes:
  - IAM denials     -> the exact `Action` the deploy role is missing
  - EC2 unauthorized (UnauthorizedOperation, often unnamed without DryRun)
  - Service quotas / limits (LimitExceeded, quota, maximum number of, capacity)
  - Backend / state errors (S3 backend, DynamoDB lock)

Usage:
    python scripts/capture_apply_failures.py apply-<runid>.log
    python scripts/capture_apply_failures.py apply-<runid>.log --json report.json

Exit code 0 always (this is a reporter, not a gate). Emits a markdown summary
to stdout and, with --json, a machine-readable report.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict

# Windows consoles default to cp1252; force UTF-8 so status glyphs don't crash.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# "... is not authorized to perform: eks:CreateCluster on resource: ..."
RE_IAM_DENY = re.compile(
    r"is not authorized to perform:?\s*([a-zA-Z0-9]+:[A-Za-z0-9*]+)"
)
# Fallback: AccessDenied blocks that name the action differently.
RE_ACCESS_DENIED = re.compile(r"AccessDenied[^\n]*?action[:\s]+([a-zA-Z0-9]+:[A-Za-z0-9*]+)")
RE_EC2_UNAUTH = re.compile(r"UnauthorizedOperation")
RE_QUOTA = re.compile(
    r"(LimitExceeded|VpcLimitExceeded|maximum number of|exceeded the .* quota|"
    r"InsufficientInstanceCapacity|would exceed|ServiceQuotaExceeded)",
    re.IGNORECASE,
)
RE_BACKEND = re.compile(
    r"(Error (loading|inspecting|refreshing) state|S3 bucket does not exist|"
    r"ConditionalCheckFailedException|error acquiring the state lock|"
    r"NoSuchBucket|Backend initialization required)",
    re.IGNORECASE,
)
# terraform error headers, to give each finding a resource context
RE_TF_ERROR = re.compile(r"^\s*Error:\s*(.+)$")


def parse(text: str) -> dict:
    lines = text.splitlines()
    missing_actions: "OrderedDict[str, int]" = OrderedDict()
    ec2_unauth = 0
    quotas: list[str] = []
    backend: list[str] = []
    error_headers: list[str] = []

    for i, line in enumerate(lines):
        for m in RE_IAM_DENY.finditer(line):
            act = m.group(1)
            missing_actions[act] = missing_actions.get(act, 0) + 1
        for m in RE_ACCESS_DENIED.finditer(line):
            act = m.group(1)
            missing_actions.setdefault(act, 0)
            missing_actions[act] += 1
        if RE_EC2_UNAUTH.search(line):
            ec2_unauth += 1
        if RE_QUOTA.search(line):
            quotas.append(line.strip()[:240])
        if RE_BACKEND.search(line):
            backend.append(line.strip()[:240])
        hm = RE_TF_ERROR.match(line)
        if hm:
            error_headers.append(hm.group(1).strip()[:200])

    return {
        "missing_iam_actions": list(missing_actions.keys()),
        "missing_iam_action_hits": missing_actions,
        "ec2_unauthorized_operation_count": ec2_unauth,
        "quota_or_limit_lines": _dedup(quotas),
        "backend_or_state_lines": _dedup(backend),
        "terraform_error_headers": _dedup(error_headers),
    }


def _dedup(items: list[str]) -> list[str]:
    seen: dict[str, None] = OrderedDict()
    for it in items:
        seen.setdefault(it, None)
    return list(seen.keys())


def markdown(report: dict, log_path: str) -> str:
    out: list[str] = []
    out.append(f"# Milestone 14.2 apply-failure report\n")
    out.append(f"Source log: `{log_path}`\n")

    acts = report["missing_iam_actions"]
    out.append("## Missing IAM actions (add these to aeos-deploy-permissions.json)\n")
    if acts:
        for a in acts:
            out.append(f"- `{a}`  (denied {report['missing_iam_action_hits'][a]}x)")
        out.append("")
        out.append("Suggested JSON snippet to merge into the matching service statement:")
        out.append("```json")
        out.append('"Action": [')
        out.append(",\n".join(f'  "{a}"' for a in acts))
        out.append("]")
        out.append("```")
    else:
        out.append("- none — no IAM denials in this log ✅")
    out.append("")

    out.append("## EC2 UnauthorizedOperation\n")
    n = report["ec2_unauthorized_operation_count"]
    if n:
        out.append(
            f"- {n} occurrence(s). EC2 often omits the action name unless DryRun is set. "
            "Cross-reference the `terraform_error_headers` below to see which EC2 resource "
            "failed, then add the corresponding `ec2:*` action."
        )
    else:
        out.append("- none ✅")
    out.append("")

    for title, key in [
        ("Quota / service limits", "quota_or_limit_lines"),
        ("Backend / state errors", "backend_or_state_lines"),
        ("Terraform error headers (context)", "terraform_error_headers"),
    ]:
        out.append(f"## {title}\n")
        vals = report[key]
        if vals:
            for v in vals[:40]:
                out.append(f"- {v}")
        else:
            out.append("- none ✅")
        out.append("")

    total = (
        len(acts)
        + report["ec2_unauthorized_operation_count"]
        + len(report["quota_or_limit_lines"])
        + len(report["backend_or_state_lines"])
    )
    out.append("---")
    out.append(
        f"**{total} actionable finding(s).** "
        + ("Clean apply — no policy/quota fixes needed." if total == 0
           else "Fix these, re-run `terraform apply`, repeat until zero.")
    )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("logfile", help="Path to the terraform apply log")
    ap.add_argument("--json", dest="json_out", help="Also write machine-readable JSON here")
    args = ap.parse_args()

    try:
        with open(args.logfile, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"cannot read log: {e}", file=sys.stderr)
        return 0  # reporter, never a gate

    report = parse(text)
    print(markdown(report, args.logfile))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"\n(JSON written to {args.json_out})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
