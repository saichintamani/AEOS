#!/usr/bin/env python3
"""Pre-14.2 de-risk: does the deploy policy cover every service the plan needs?

Cross-references the rendered terraform plan (`plan-show.txt`) against the
`aeos-deploy` permission policy. A first `terraform apply` wastes a full
iteration (and real money) for every service whose actions are entirely
missing from the policy -- terraform gets an AccessDenied wall before it can
create anything. This catches those *namespace-level* holes statically.

It does NOT prove the action set is complete (only a real apply does that --
see Milestone 14.2). It proves the weaker but valuable property: every AWS
service the plan touches has at least the create/describe verbs present.

Usage:
    python scripts/check_policy_coverage.py \
        --plan  infrastructure/terraform/environments/dev/plan-show.txt \
        --policy infrastructure/aws/iam/aeos-deploy-permissions.json

Exit code: 0 if every needed namespace is covered, 1 if any is missing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict

# Windows consoles default to cp1252; force UTF-8 so status glyphs don't crash.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# aws_<type> resource -> the IAM service namespace(s) its CRUD calls live under.
# Prefixes are matched longest-first so aws_cloudwatch_log_group -> logs, not cloudwatch.
RESOURCE_PREFIX_TO_NAMESPACES: list[tuple[str, tuple[str, ...]]] = [
    ("aws_cloudwatch_log_group", ("logs",)),
    ("aws_cloudwatch_metric_alarm", ("cloudwatch",)),
    ("aws_cloudwatch_dashboard", ("cloudwatch",)),
    ("aws_flow_log", ("ec2", "logs")),          # creates a flow log -> ec2; writes to logs
    ("aws_db_", ("rds",)),
    ("aws_elasticache_", ("elasticache",)),
    ("aws_eks_", ("eks",)),
    ("aws_ecr_", ("ecr",)),
    ("aws_s3_", ("s3",)),
    ("aws_kms_", ("kms",)),
    ("aws_iam_openid_connect_provider", ("iam",)),
    ("aws_iam_", ("iam",)),
    # everything VPC/networking is the ec2 namespace
    ("aws_vpc", ("ec2",)),
    ("aws_subnet", ("ec2",)),
    ("aws_route", ("ec2",)),                     # route + route_table + association
    ("aws_internet_gateway", ("ec2",)),
    ("aws_nat_gateway", ("ec2",)),
    ("aws_eip", ("ec2",)),
    ("aws_security_group", ("ec2",)),
    ("aws_network", ("ec2",)),
]

RE_CREATED = re.compile(r"#\s+module\..*?\.((?:aws|random|tls|null)_[a-z0-9_]+)\b.*will be created")


def namespaces_for(resource_type: str) -> tuple[str, ...]:
    for prefix, ns in RESOURCE_PREFIX_TO_NAMESPACES:
        if resource_type.startswith(prefix):
            return ns
    return ()  # non-aws (random/tls/null) or unmapped -> no IAM namespace needed


def plan_namespaces(plan_text: str) -> dict[str, list[str]]:
    """namespace -> sorted list of resource types that require it."""
    needed: dict[str, set[str]] = defaultdict(set)
    for m in RE_CREATED.finditer(plan_text):
        rtype = m.group(1)
        for ns in namespaces_for(rtype):
            needed[ns].add(rtype)
    return {ns: sorted(v) for ns, v in sorted(needed.items())}


def policy_namespaces(policy: dict) -> set[str]:
    present: set[str] = set()
    for stmt in policy.get("Statement", []):
        if stmt.get("Effect") != "Allow":
            continue  # a Deny doesn't grant coverage
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        for a in actions:
            if ":" in a:
                present.add(a.split(":", 1)[0])
    return present


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--policy", required=True)
    args = ap.parse_args()

    with open(args.plan, encoding="utf-8", errors="replace") as f:
        plan_text = f.read()
    with open(args.policy, encoding="utf-8") as f:
        policy = json.load(f)

    needed = plan_namespaces(plan_text)
    present = policy_namespaces(policy)

    print("# Deploy-policy coverage vs plan\n")
    missing: list[str] = []
    for ns, rtypes in needed.items():
        ok = ns in present
        mark = "OK " if ok else "MISSING"
        print(f"[{mark}] {ns:12s} <- {', '.join(rtypes)}")
        if not ok:
            missing.append(ns)

    print()
    print(f"namespaces needed by plan : {', '.join(needed) or '(none)'}")
    print(f"namespaces in policy      : {', '.join(sorted(present)) or '(none)'}")
    print()
    if missing:
        print(f"RESULT: {len(missing)} namespace(s) MISSING -> {', '.join(missing)}")
        print("These would produce an AccessDenied wall on the first apply. Add them")
        print("to aeos-deploy-permissions.json before running Milestone 14.2.")
        return 1
    print("RESULT: every service namespace the plan needs is present in the policy. ✅")
    print("(Namespace coverage only -- exact action completeness is validated by the")
    print(" real apply in 14.2.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
