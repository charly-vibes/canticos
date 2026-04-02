#!/usr/bin/env python3
"""ci-usage — GitHub Actions CI usage report.

Queries workflow runs for a repository and reports which workflows and
runs are consuming the most CI minutes.
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# GitHub Actions billing multipliers (minutes)
OS_MULTIPLIERS = {"UBUNTU": 1, "MACOS": 10, "WINDOWS": 2}
OS_LABELS = {"UBUNTU": "Linux", "MACOS": "macOS", "WINDOWS": "Windows"}


def gh(*args, check=True):
    """Run gh CLI and return stdout."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        msg = r.stderr.strip() or f"gh exited with code {r.returncode}"
        print(f"error: {msg}", file=sys.stderr)
        sys.exit(1)
    return r


def fetch_runs(repo, limit=1000):
    """Fetch workflow runs via gh run list."""
    fields = ",".join([
        "databaseId", "workflowName", "name", "status", "conclusion",
        "startedAt", "updatedAt", "event", "headBranch", "displayTitle",
        "url", "number",
    ])
    raw = gh("run", "list", "--repo", repo, "-L", str(limit),
             "--json", fields).stdout
    return json.loads(raw)


def fetch_timing(repo, run_id):
    """Fetch billable timing for a single run."""
    raw = gh("api", f"/repos/{repo}/actions/runs/{run_id}/timing").stdout
    return json.loads(raw)


def fetch_quota(owner):
    """Fetch Actions billing quota for a user or org.

    Tries the org endpoint first, then the user endpoint.
    Returns dict with total_minutes_used, included_minutes, etc.
    or None if the token lacks the required scope.
    """
    # try org first
    r = gh("api", f"/orgs/{owner}/settings/billing/actions", check=False)
    if r.returncode == 0:
        return json.loads(r.stdout)
    # try user
    r = gh("api", f"/users/{owner}/settings/billing/actions", check=False)
    if r.returncode == 0:
        return json.loads(r.stdout)
    return None


def parse_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_dur(seconds):
    if seconds < 0:
        seconds = 0
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_ms(ms):
    return fmt_dur(ms / 1000)


def main():
    ap = argparse.ArgumentParser(
        description="GitHub Actions CI usage report.",
        epilog="Examples:\n"
               "  ci-usage owner/repo\n"
               "  ci-usage owner/repo --days 7 --top 5\n"
               "  ci-usage owner/repo --quota\n"
               "  ci-usage owner/repo --billable\n"
               "  ci-usage owner/repo --workflow 'CI' --json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("repo", help="Repository in owner/repo format")
    ap.add_argument("--days", type=int, default=30,
                    help="Look back N days (default: 30)")
    ap.add_argument("--top", type=int, default=10,
                    help="Number of top runs to show (default: 10)")
    ap.add_argument("--billable", action="store_true",
                    help="Fetch billable timing per run (slower, extra API calls)")
    ap.add_argument("--workflow", help="Filter to a specific workflow name")
    ap.add_argument("--branch", help="Filter to a specific branch")
    ap.add_argument("--event",
                    help="Filter to a trigger event (push, pull_request, schedule, ...)")
    ap.add_argument("--quota", action="store_true",
                    help="Show account-level Actions minutes quota "
                         "(requires 'user' or 'read:org' scope)")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="Output JSON instead of formatted text")
    args = ap.parse_args()

    if "/" not in args.repo:
        print("error: repo must be in owner/repo format", file=sys.stderr)
        sys.exit(1)

    owner = args.repo.split("/")[0]
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)

    # --- quota ---------------------------------------------------------------
    quota = None
    if args.quota:
        print(f"Fetching Actions quota for {owner}...", file=sys.stderr)
        quota = fetch_quota(owner)
        if quota is None:
            print("warning: could not fetch quota — run "
                  "'gh auth refresh -h github.com -s user' "
                  "(or -s read:org for orgs)", file=sys.stderr)

    # --- fetch ---------------------------------------------------------------
    print(f"Fetching runs for {args.repo} (last {args.days} days)...",
          file=sys.stderr)
    all_runs = fetch_runs(args.repo)

    # --- filter & enrich -----------------------------------------------------
    runs = []
    for r in all_runs:
        if r.get("status") != "completed":
            continue
        started = parse_ts(r.get("startedAt"))
        updated = parse_ts(r.get("updatedAt"))
        if not started or not updated:
            continue
        if started < since:
            continue
        if args.workflow and r.get("workflowName") != args.workflow:
            continue
        if args.branch and r.get("headBranch") != args.branch:
            continue
        if args.event and r.get("event") != args.event:
            continue
        r["_dur"] = (updated - started).total_seconds()
        r["_started"] = started
        runs.append(r)

    if not runs:
        print("No completed runs match the criteria.", file=sys.stderr)
        sys.exit(0)

    runs.sort(key=lambda r: r["_dur"], reverse=True)

    # --- billable timing (optional) ------------------------------------------
    if args.billable:
        n = min(len(runs), args.top)
        print(f"Fetching billable timing for top {n} runs...", file=sys.stderr)
        for r in runs[:n]:
            try:
                t = fetch_timing(args.repo, r["databaseId"])
                billable = {}
                for os_key in OS_MULTIPLIERS:
                    ms = (t.get("billable", {})
                           .get(os_key, {})
                           .get("total_ms", 0))
                    billable[os_key] = ms
                r["_billable"] = billable
                r["_billable_total_ms"] = sum(billable.values())
                r["_billed_ms"] = sum(
                    ms * OS_MULTIPLIERS[k] for k, ms in billable.items()
                )
            except Exception:
                pass

    # --- aggregation ---------------------------------------------------------
    by_wf = defaultdict(lambda: {"runs": 0, "failed": 0, "dur": 0.0})
    by_event = defaultdict(lambda: {"runs": 0, "dur": 0.0})
    by_branch = defaultdict(lambda: {"runs": 0, "dur": 0.0})

    for r in runs:
        wf = r.get("workflowName") or r.get("name") or "unknown"
        by_wf[wf]["runs"] += 1
        by_wf[wf]["dur"] += r["_dur"]
        if r.get("conclusion") == "failure":
            by_wf[wf]["failed"] += 1

        ev = r.get("event", "unknown")
        by_event[ev]["runs"] += 1
        by_event[ev]["dur"] += r["_dur"]

        br = r.get("headBranch", "unknown")
        by_branch[br]["runs"] += 1
        by_branch[br]["dur"] += r["_dur"]

    total_runs = len(runs)
    total_dur = sum(r["_dur"] for r in runs)
    total_failed = sum(1 for r in runs if r.get("conclusion") == "failure")

    # --- JSON output ---------------------------------------------------------
    if args.as_json:
        out = {
            "repo": args.repo,
            "period_days": args.days,
            "since": since.isoformat(),
            "total_runs": total_runs,
            "total_failed": total_failed,
            "total_duration_s": total_dur,
            "by_workflow": dict(sorted(
                by_wf.items(), key=lambda x: x[1]["dur"], reverse=True,
            )),
            "by_event": dict(by_event),
            "top_runs": [
                {
                    "number": r.get("number"),
                    "workflow": r.get("workflowName", ""),
                    "branch": r.get("headBranch", ""),
                    "conclusion": r.get("conclusion", ""),
                    "duration_s": r["_dur"],
                    "started_at": r["_started"].isoformat(),
                    "title": r.get("displayTitle", ""),
                    "url": r.get("url", ""),
                    **({"billable": r["_billable"],
                        "billed_ms": r["_billed_ms"]}
                       if "_billable" in r else {}),
                }
                for r in runs[:args.top]
            ],
        }
        if quota:
            out["quota"] = quota
        json.dump(out, sys.stdout, indent=2, default=str)
        print()
        return

    # --- text report ---------------------------------------------------------
    W = 64

    print()
    print(f"  CI Usage Report: {args.repo}")
    print(f"  {since.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}"
          f" ({args.days} days)")
    print(f"  {'─' * W}")
    print()
    fail_pct = total_failed / total_runs * 100 if total_runs else 0
    print(f"  Runs: {total_runs}   Duration: {fmt_dur(total_dur)}   "
          f"Failed: {total_failed} ({fail_pct:.0f}%)")

    # quota
    if quota:
        used = quota.get("total_minutes_used", 0)
        included = quota.get("included_minutes", 0)
        paid = quota.get("total_paid_minutes_used", 0)
        breakdown = quota.get("minutes_used_breakdown", {})
        print()
        print(f"  Account Quota ({owner})")
        print(f"  {'─' * W}")
        if included > 0:
            pct = used / included * 100
            bar_len = 30
            filled = int(bar_len * min(used, included) / included)
            bar = "█" * filled + "░" * (bar_len - filled)
            print(f"  [{bar}] {pct:.1f}%")
            print(f"  {used:.0f} / {included:.0f} minutes used this cycle")
            if paid > 0:
                print(f"  {paid:.0f} paid minutes (beyond free tier)")
            if pct >= 80:
                remaining = included - used
                print(f"  ⚠ {remaining:.0f} minutes remaining!")
        else:
            print(f"  {used:.0f} minutes used (no included quota)")
        if breakdown:
            parts = []
            for k in ("UBUNTU", "MACOS", "WINDOWS"):
                v = breakdown.get(k, 0)
                if v:
                    parts.append(f"{OS_LABELS.get(k, k)}: {v:.0f}m")
            if parts:
                print(f"  Breakdown: {', '.join(parts)}")

    print()

    # workflows
    print(f"  By Workflow")
    print(f"  {'─' * W}")
    wf_list = sorted(by_wf.items(), key=lambda x: x[1]["dur"], reverse=True)
    col = min(max(len(n) for n, _ in wf_list), 35)
    print(f"  {'Name':<{col}}  {'Runs':>5}  {'Fail':>4}"
          f"  {'Total':>10}  {'Avg':>10}")
    for name, d in wf_list:
        avg = d["dur"] / d["runs"] if d["runs"] else 0
        print(f"  {name[:col]:<{col}}  {d['runs']:>5}  {d['failed']:>4}"
              f"  {fmt_dur(d['dur']):>10}  {fmt_dur(avg):>10}")
    print()

    # trigger events
    print(f"  By Trigger")
    print(f"  {'─' * W}")
    for ev, d in sorted(by_event.items(),
                        key=lambda x: x[1]["dur"], reverse=True):
        pct = d["dur"] / total_dur * 100 if total_dur else 0
        print(f"  {ev:<20}  {d['runs']:>5} runs"
              f"  {fmt_dur(d['dur']):>10}  ({pct:.0f}%)")
    print()

    # top branches
    print(f"  Top Branches")
    print(f"  {'─' * W}")
    for br, d in sorted(by_branch.items(),
                        key=lambda x: x[1]["dur"], reverse=True)[:10]:
        print(f"  {br[:40]:<40}  {d['runs']:>5} runs"
              f"  {fmt_dur(d['dur']):>10}")
    print()

    # top runs
    n = min(args.top, len(runs))
    print(f"  Top {n} Runs")
    print(f"  {'─' * W}")
    for r in runs[:n]:
        num = f"#{r.get('number', '?')}"
        wf = (r.get("workflowName") or "")[:22]
        br = (r.get("headBranch") or "")[:14]
        con = r.get("conclusion", "")
        dur = fmt_dur(r["_dur"])
        dt = r["_started"].strftime("%m-%d")
        mark = ("x" if con == "failure"
                else "~" if con == "cancelled"
                else " ")
        line = f"  [{mark}] {num:<7} {wf:<22} {br:<14} {dur:>10}  {dt}"
        if r.get("_billed_ms"):
            line += f"  (billed: {fmt_ms(r['_billed_ms'])})"
        print(line)

    # billable summary
    if args.billable:
        billed_runs = [r for r in runs[:n] if "_billable" in r]
        if billed_runs:
            totals = defaultdict(int)
            for r in billed_runs:
                for k, ms in r["_billable"].items():
                    totals[k] += ms
            grand = sum(ms * OS_MULTIPLIERS[k] for k, ms in totals.items())
            print()
            if grand:
                print(f"  Billable Breakdown (top {len(billed_runs)} runs)")
                print(f"  {'─' * W}")
                for k in ("UBUNTU", "MACOS", "WINDOWS"):
                    ms = totals[k]
                    if ms:
                        billed = ms * OS_MULTIPLIERS[k]
                        print(f"  {OS_LABELS[k]:<10}"
                              f"  {fmt_ms(ms):>10} raw"
                              f"  x{OS_MULTIPLIERS[k]:<2}"
                              f"  = {fmt_ms(billed):>10} billed")
                print(f"  {'Total':<10}  {'':>14}     "
                      f"  {fmt_ms(grand):>10} billed")
            else:
                print("  Billable: n/a (public repos have free Actions minutes)")

    print()


if __name__ == "__main__":
    main()
