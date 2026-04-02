#!/usr/bin/env python3
"""Claude Code Usage Dashboard — local HTML dashboard with incremental caching."""

import html as html_mod
import json
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
STATS_CACHE = CLAUDE_DIR / "stats-cache.json"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"
DASHBOARD_CACHE = CLAUDE_DIR / "dashboard-cache.json"
CACHE_VERSION = 6

MODEL_PRICING = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "claude-opus-4-5-20251101": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}

MODEL_COLORS = {
    "Opus 4.6": "#c084fc", "Opus 4.5": "#a855f7",
    "Sonnet 4.6": "#22d3ee", "Sonnet 4.5": "#06b6d4",
    "Haiku 4.5": "#34d399",
}

LOCAL_TZ = datetime.now().astimezone().tzinfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_pricing(model_id):
    for key, pricing in MODEL_PRICING.items():
        if key in model_id:
            return pricing
    return DEFAULT_PRICING


def model_short_name(model_id):
    for frag, name in [("opus-4-6", "Opus 4.6"), ("opus-4-5", "Opus 4.5"),
                        ("sonnet-4-6", "Sonnet 4.6"), ("sonnet-4-5", "Sonnet 4.5"),
                        ("haiku", "Haiku 4.5")]:
        if frag in model_id:
            return name
    return model_id


def compute_cost(model_id, usage):
    p = get_pricing(model_id)
    return (
        usage.get("input_tokens", 0) * p["input"]
        + usage.get("output_tokens", 0) * p["output"]
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
        + usage.get("cache_creation_input_tokens", 0) * p["cache_write"]
    ) / 1_000_000


def parse_ts(ts):
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)


def prettify_project(name):
    home_prefix = str(Path.home()).replace("/", "-").lstrip("-")
    if name.startswith("-" + home_prefix + "-"):
        path_part = name[len(home_prefix) + 2:]
    elif name.startswith("-" + home_prefix):
        return "~"
    else:
        path_part = name.lstrip("-")
    segments = path_part.split("-")
    result = "~"
    i = 0
    while i < len(segments):
        best = None
        for j in range(len(segments), i, -1):
            candidate = "-".join(segments[i:j])
            if (Path(result.replace("~", str(Path.home()))) / candidate).exists():
                best = (candidate, j)
                break
        if best:
            result += "/" + best[0]
            i = best[1]
        else:
            result += "/" + segments[i]
            i += 1
    return result


def fmt_tokens(n):
    if n >= 1_000_000_000: return f"{n/1e9:.1f}B"
    if n >= 1_000_000: return f"{n/1e6:.1f}M"
    if n >= 1_000: return f"{n/1e3:.1f}K"
    return str(n)


def fmt_cost(c):
    if c >= 1000: return f"${c:,.0f}"
    if c >= 100: return f"${c:.1f}"
    if c >= 1: return f"${c:.2f}"
    return f"${c:.3f}"


def fmt_duration(ms):
    if ms < 1000: return f"{ms:.0f}ms"
    s = ms / 1000
    if s < 60: return f"{s:.1f}s"
    m = s / 60
    if m < 60: return f"{m:.0f}m {s%60:.0f}s"
    h = m / 60
    return f"{h:.0f}h {m%60:.0f}m"


# ---------------------------------------------------------------------------
# Session parser — extracts a cacheable summary from a single JSONL file
# ---------------------------------------------------------------------------

def parse_session_file(fpath, project_name):
    """Parse a conversation JSONL file into a compact SessionSummary dict."""
    summary = {
        "session_id": fpath.stem,
        "project": project_name,
        "first_ts": None,
        "last_ts": None,
        "user_messages": 0,
        "turns": [],
        "turn_durations_ms": [],
        "api_errors": [],
        "compactions": 0,
        "hour_messages": {},
        "tool_counts": {},
        "skill_uses": {},      # skill_name -> count
    }

    with open(fpath) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts_raw = entry.get("timestamp")
            if not ts_raw:
                continue
            try:
                dt = parse_ts(ts_raw)
            except (ValueError, OSError):
                continue

            ts_iso = dt.isoformat()
            if summary["first_ts"] is None:
                summary["first_ts"] = ts_iso
            summary["last_ts"] = ts_iso

            entry_type = entry.get("type")

            # Heatmap: count messages in local time
            if entry_type in ("user", "assistant"):
                local_h = str(dt.astimezone(LOCAL_TZ).hour)
                summary["hour_messages"][local_h] = summary["hour_messages"].get(local_h, 0) + 1

            if entry_type == "user":
                summary["user_messages"] += 1

            elif entry_type == "assistant":
                msg = entry.get("message", {})
                if isinstance(msg, str):
                    try:
                        msg = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage", {})
                model = msg.get("model", "unknown")
                if not usage:
                    continue

                # Extract tool names and skill invocations from content blocks
                tools = []
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown")
                        tools.append(tool_name)
                        # Track Skill invocations separately
                        if tool_name == "Skill":
                            skill_input = block.get("input", {})
                            skill_name = skill_input.get("skill", "unknown")
                            summary["skill_uses"][skill_name] = summary["skill_uses"].get(skill_name, 0) + 1

                for t in tools:
                    summary["tool_counts"][t] = summary["tool_counts"].get(t, 0) + 1

                summary["turns"].append({
                    "ts": ts_iso,
                    "model": model,
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_write": usage.get("cache_creation_input_tokens", 0),
                    "cost": round(compute_cost(model, usage), 6),
                    "tools": tools,
                })

            elif entry_type == "system":
                subtype = entry.get("subtype")
                if subtype == "turn_duration":
                    dur = entry.get("durationMs")
                    if isinstance(dur, (int, float)):
                        summary["turn_durations_ms"].append(int(dur))
                elif subtype == "api_error":
                    err = entry.get("error", {})
                    summary["api_errors"].append({
                        "ts": ts_iso,
                        "status": err.get("status", 0),
                        "retry": entry.get("retryAttempt", 0),
                    })
                elif subtype in ("compact_boundary", "microcompact_boundary"):
                    summary["compactions"] += 1

    return summary


# ---------------------------------------------------------------------------
# Cache layer — incremental scanning with mtime-based invalidation
# ---------------------------------------------------------------------------

def load_cache():
    if DASHBOARD_CACHE.exists():
        try:
            data = json.loads(DASHBOARD_CACHE.read_text())
            tz_offset = datetime.now(timezone.utc).astimezone(LOCAL_TZ).utcoffset().total_seconds() / 60
            if data.get("version") == CACHE_VERSION and data.get("tz_offset_minutes") == tz_offset:
                return data.get("entries", {})
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_cache(entries):
    tz_offset = datetime.now(timezone.utc).astimezone(LOCAL_TZ).utcoffset().total_seconds() / 60
    DASHBOARD_CACHE.write_text(json.dumps({
        "version": CACHE_VERSION,
        "tz_offset_minutes": tz_offset,
        "entries": entries,
    }, separators=(",", ":")))


def cache_key(fpath):
    st = fpath.stat()
    return f"{fpath}:{st.st_mtime_ns}:{st.st_size}"


def scan_sessions(use_cache=True):
    """Scan all conversation files, using cache for unchanged files. Returns all summaries."""
    if not PROJECTS_DIR.exists():
        return []

    cached = load_cache() if use_cache else {}
    new_entries = {}
    summaries = []
    hits = misses = 0

    jsonl_files = [
        (f, pd.name)
        for pd in PROJECTS_DIR.iterdir() if pd.is_dir()
        for f in pd.glob("*.jsonl")
    ]

    for i, (fpath, proj) in enumerate(jsonl_files, 1):
        if i % 200 == 0:
            print(f"  [{i}/{len(jsonl_files)}] scanning...", flush=True)
        try:
            key = cache_key(fpath)
        except OSError:
            continue

        if key in cached:
            summary = cached[key]
            hits += 1
        else:
            try:
                summary = parse_session_file(fpath, proj)
            except (PermissionError, OSError):
                continue
            misses += 1
        new_entries[key] = summary
        summaries.append(summary)

    if use_cache:
        save_cache(new_entries)
    print(f"  {len(summaries)} sessions ({hits} cached, {misses} parsed)")
    return summaries


# ---------------------------------------------------------------------------
# Analyzers — pure functions over List[SessionSummary]
# ---------------------------------------------------------------------------

def filter_by_days(summaries, days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [s for s in summaries if (s.get("last_ts") or "") >= cutoff]


def analyze_costs(sessions, date_range):
    daily = defaultdict(float)
    daily_by_model = defaultdict(lambda: defaultdict(float))
    model_costs = defaultdict(float)
    model_tokens = defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})

    for s in sessions:
        for t in s["turns"]:
            d = t["ts"][:10]
            mname = model_short_name(t["model"])
            daily[d] += t["cost"]
            daily_by_model[d][mname] += t["cost"]
            model_costs[mname] += t["cost"]
            model_tokens[mname]["input"] += t["input_tokens"]
            model_tokens[mname]["output"] += t["output_tokens"]
            model_tokens[mname]["cache_read"] += t["cache_read"]
            model_tokens[mname]["cache_write"] += t["cache_write"]

    total_cost = sum(daily.values())
    total_input = sum(v["input"] for v in model_tokens.values())
    total_output = sum(v["output"] for v in model_tokens.values())
    total_cache_read = sum(v["cache_read"] for v in model_tokens.values())
    total_cache_write = sum(v["cache_write"] for v in model_tokens.values())

    # Cost per message
    total_msgs = sum(s["user_messages"] for s in sessions)
    cost_per_msg = total_cost / total_msgs if total_msgs else 0

    # 7-day rolling average
    rolling = []
    for i, d in enumerate(date_range):
        window = [daily.get(date_range[j], 0) for j in range(max(0, i - 6), i + 1)]
        rolling.append(round(sum(window) / len(window), 4))

    return {
        "daily": daily, "daily_by_model": daily_by_model,
        "model_costs": model_costs, "model_tokens": model_tokens,
        "total_cost": total_cost, "total_input": total_input,
        "total_output": total_output, "total_cache_read": total_cache_read,
        "total_cache_write": total_cache_write,
        "cost_per_msg": cost_per_msg, "rolling_avg": rolling,
    }


def analyze_sessions(sessions, date_range):
    daily_sessions = defaultdict(set)
    daily_messages = defaultdict(int)

    for s in sessions:
        for t in s["turns"]:
            daily_sessions[t["ts"][:10]].add(s["session_id"])
        if s["first_ts"]:
            daily_messages[s["first_ts"][:10]] += s["user_messages"]

    total_sessions = len(sessions)
    total_messages = sum(s["user_messages"] for s in sessions)
    avg_msgs_per_session = total_messages / total_sessions if total_sessions else 0

    # Session duration from turn_durations
    session_durations = []
    for s in sessions:
        if s["turn_durations_ms"]:
            session_durations.append((s["session_id"], s["project"], sum(s["turn_durations_ms"]),
                                      s["user_messages"], s.get("first_ts", "")[:10]))

    avg_duration = (sum(d[2] for d in session_durations) / len(session_durations)) if session_durations else 0

    # Session length distribution
    buckets = {"1-5": 0, "6-20": 0, "21-50": 0, "51-100": 0, "100+": 0}
    for s in sessions:
        m = s["user_messages"]
        if m <= 5: buckets["1-5"] += 1
        elif m <= 20: buckets["6-20"] += 1
        elif m <= 50: buckets["21-50"] += 1
        elif m <= 100: buckets["51-100"] += 1
        else: buckets["100+"] += 1

    # Top sessions by duration
    top_by_duration = sorted(session_durations, key=lambda x: x[2], reverse=True)[:5]

    return {
        "daily_sessions": daily_sessions, "daily_messages": daily_messages,
        "total_sessions": total_sessions, "total_messages": total_messages,
        "avg_msgs_per_session": avg_msgs_per_session, "avg_duration_ms": avg_duration,
        "length_buckets": buckets, "top_by_duration": top_by_duration,
    }


def analyze_time_patterns(sessions, date_range):
    hour_totals = defaultdict(int)
    weekday_msgs = defaultdict(int)
    weekday_days = defaultdict(set)

    for s in sessions:
        for h, count in s["hour_messages"].items():
            hour_totals[int(h)] += count
        if s["first_ts"]:
            try:
                dt = datetime.fromisoformat(s["first_ts"])
                dow = dt.weekday()
                d = s["first_ts"][:10]
                weekday_msgs[dow] += s["user_messages"]
                weekday_days[dow].add(d)
            except ValueError:
                pass

    # Peak hours (top 4 consecutive)
    hours_sorted = sorted(range(24), key=lambda h: hour_totals.get(h, 0), reverse=True)
    peak_hours = sorted(hours_sorted[:4])

    # Weekday vs weekend avg
    wd_msgs = sum(weekday_msgs.get(d, 0) for d in range(5))
    wd_days = sum(len(weekday_days.get(d, set())) for d in range(5))
    we_msgs = sum(weekday_msgs.get(d, 0) for d in range(5, 7))
    we_days = sum(len(weekday_days.get(d, set())) for d in range(5, 7))
    weekday_avg = wd_msgs / wd_days if wd_days else 0
    weekend_avg = we_msgs / we_days if we_days else 0

    # Day-hour activity for heatmap
    day_hour = defaultdict(int)
    for s in sessions:
        d = (s["first_ts"] or "")[:10]
        for h, count in s["hour_messages"].items():
            day_hour[f"{d}|{h}"] += count

    return {
        "hour_totals": hour_totals, "peak_hours": peak_hours,
        "weekday_avg": weekday_avg, "weekend_avg": weekend_avg,
        "day_hour": day_hour,
    }


def analyze_efficiency(sessions, date_range):
    daily_cache_rate = {}
    daily_errors = defaultdict(int)
    daily_compactions = defaultdict(int)
    daily_turn_dur = defaultdict(list)
    tool_totals = defaultdict(int)

    for s in sessions:
        d = (s["first_ts"] or "")[:10]
        s_compactions = s.get("compactions", 0)
        if s_compactions:
            daily_compactions[d] += s_compactions

        for err in s.get("api_errors", []):
            daily_errors[err["ts"][:10]] += 1

        for dur in s.get("turn_durations_ms", []):
            daily_turn_dur[d].append(dur)

        for tool, count in s.get("tool_counts", {}).items():
            tool_totals[tool] += count

    # Daily cache hit rate
    for d in date_range:
        total_in = total_cache_r = 0
        for s in sessions:
            for t in s["turns"]:
                if t["ts"][:10] == d:
                    total_in += t["input_tokens"] + t["cache_write"]
                    total_cache_r += t["cache_read"]
        total = total_in + total_cache_r
        daily_cache_rate[d] = round(total_cache_r / total * 100, 1) if total else 0

    # Aggregate cache rate
    total_ctx = sum(t["input_tokens"] + t["cache_write"] + t["cache_read"]
                    for s in sessions for t in s["turns"])
    total_cache = sum(t["cache_read"] for s in sessions for t in s["turns"])
    cache_rate = round(total_cache / total_ctx * 100, 1) if total_ctx else 0

    # Average turn duration per day
    avg_turn_dur = {}
    for d in date_range:
        durs = daily_turn_dur.get(d, [])
        avg_turn_dur[d] = round(sum(durs) / len(durs)) if durs else 0

    total_errors = sum(daily_errors.values())
    total_compactions = sum(daily_compactions.values())

    # Top tools
    top_tools = sorted(tool_totals.items(), key=lambda x: x[1], reverse=True)[:12]

    return {
        "cache_rate": cache_rate, "daily_cache_rate": daily_cache_rate,
        "total_errors": total_errors, "daily_errors": daily_errors,
        "total_compactions": total_compactions, "daily_compactions": daily_compactions,
        "avg_turn_dur": avg_turn_dur, "top_tools": top_tools,
    }


def analyze_projects(sessions):
    proj = defaultdict(lambda: {"cost": 0.0, "sessions": 0, "messages": 0})
    for s in sessions:
        p = prettify_project(s["project"])
        proj[p]["sessions"] += 1
        proj[p]["messages"] += s["user_messages"]
        proj[p]["cost"] += sum(t["cost"] for t in s["turns"])
    rows = sorted(proj.items(), key=lambda x: x[1]["cost"], reverse=True)[:15]
    return {"rows": rows}


def load_slash_commands(days):
    """Parse slash commands from history.jsonl (where the CLI records them)."""
    cmds = []
    if not HISTORY_FILE.exists():
        return cmds
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
    with open(HISTORY_FILE) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            display = entry.get("display", "")
            ts = entry.get("timestamp")
            if not ts or not isinstance(display, str):
                continue
            stripped = display.strip()
            if not stripped.startswith("/"):
                continue
            if isinstance(ts, (int, float)) and ts < cutoff:
                continue
            cmd = stripped.split()[0]
            if 2 <= len(cmd) <= 30 and "/" not in cmd[1:]:
                try:
                    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if isinstance(ts, (int, float)) else parse_ts(ts)
                    cmds.append({"ts": dt.isoformat(), "command": cmd})
                except (ValueError, OSError):
                    continue
    return cmds


def analyze_skills(sessions, date_range, history_commands=None):
    skill_totals = defaultdict(int)
    skill_daily = defaultdict(lambda: defaultdict(int))
    skill_projects = defaultdict(lambda: defaultdict(int))  # skill -> project -> count
    cmd_totals = defaultdict(int)
    cmd_daily = defaultdict(lambda: defaultdict(int))

    for s in sessions:
        proj = prettify_project(s["project"])
        for skill, count in s.get("skill_uses", {}).items():
            skill_totals[skill] += count
            skill_projects[skill][proj] += count
        # Use session first_ts for daily distribution
        d = (s.get("first_ts") or "")[:10]
        for skill, count in s.get("skill_uses", {}).items():
            skill_daily[d][skill] += count

    # Slash commands from history.jsonl
    for cmd_entry in (history_commands or []):
        cmd = cmd_entry["command"]
        d = cmd_entry["ts"][:10]
        cmd_totals[cmd] += 1
        cmd_daily[d][cmd] += 1

    top_skills = sorted(skill_totals.items(), key=lambda x: x[1], reverse=True)
    top_commands = sorted(cmd_totals.items(), key=lambda x: x[1], reverse=True)

    # Skills over time: daily total skill invocations
    daily_skill_count = [sum(skill_daily.get(d, {}).values()) for d in date_range]

    # Top skill-project associations
    skill_project_rows = []
    for skill, projs in sorted(skill_projects.items(), key=lambda x: skill_totals[x[0]], reverse=True):
        top_proj = max(projs.items(), key=lambda x: x[1])[0] if projs else ""
        skill_project_rows.append((skill, skill_totals[skill], top_proj, len(projs)))

    return {
        "top_skills": top_skills,
        "top_commands": top_commands,
        "daily_skill_count": daily_skill_count,
        "skill_project_rows": skill_project_rows,
        "total_skill_uses": sum(skill_totals.values()),
        "total_commands": sum(cmd_totals.values()),
    }


# ---------------------------------------------------------------------------
# HTML Renderer — component functions
# ---------------------------------------------------------------------------

def _stat_card(label, value, css_class="", sub=""):
    return f"""<div class="stat-card">
    <div class="label">{label}</div>
    <div class="value {css_class}">{value}</div>
    {f'<div class="sub">{sub}</div>' if sub else ''}
  </div>"""


def render_summary(costs, sess, eff, stats_cache):
    at_sess = stats_cache.get("totalSessions", sess["total_sessions"]) if stats_cache else sess["total_sessions"]
    at_msgs = stats_cache.get("totalMessages", sess["total_messages"]) if stats_cache else sess["total_messages"]
    cards = [
        _stat_card("Estimated Cost", fmt_cost(costs["total_cost"]), "cost", f'{fmt_cost(costs["cost_per_msg"])}/msg'),
        _stat_card("Sessions", f'{sess["total_sessions"]:,}', "accent", f"{at_sess:,} all time"),
        _stat_card("Messages", f'{sess["total_messages"]:,}', "cyan", f"{at_msgs:,} all time"),
        _stat_card("Output Tokens", fmt_tokens(costs["total_output"]), "", f'{fmt_tokens(costs["total_input"])} input'),
        _stat_card("Cache Hit Rate", f'{eff["cache_rate"]}%', "green", f'{fmt_tokens(costs["total_cache_read"])} tokens cached'),
        _stat_card("Avg Session", f'{sess["avg_msgs_per_session"]:.0f} msgs', "",
                   fmt_duration(sess["avg_duration_ms"]) if sess["avg_duration_ms"] else ""),
    ]
    return f'<div class="stats-grid">{"".join(cards)}</div>'


def render_cost_chart(costs, date_range):
    labels = json.dumps(date_range)
    models = sorted(costs["model_costs"].keys(), key=lambda m: costs["model_costs"][m], reverse=True)
    datasets = []
    for mname in models:
        color = MODEL_COLORS.get(mname, "#94a3b8")
        values = [round(costs["daily_by_model"].get(d, {}).get(mname, 0), 4) for d in date_range]
        datasets.append({"label": mname, "data": values, "backgroundColor": color,
                         "borderColor": color, "borderWidth": 1})
    # Rolling average overlay
    datasets.append({"label": "7d avg", "data": costs["rolling_avg"], "type": "line",
                     "borderColor": "#f87171", "borderWidth": 2, "pointRadius": 0,
                     "fill": False, "order": 0})

    short = json.dumps([d[5:] if i % max(1, len(date_range) // 10) == 0 else ""
                        for i, d in enumerate(date_range)])
    return f"""<div class="card">
  <h2>Daily Cost by Model</h2>
  <div class="chart-container"><canvas id="costChart"></canvas></div>
</div>
<script>
new Chart(document.getElementById('costChart'), {{
  type: 'bar',
  data: {{labels: {short}.map((l,i) => l || {labels}[i].slice(5)), datasets: {json.dumps(datasets)}}},
  options: {{responsive:true, maintainAspectRatio:false,
    plugins: {{tooltip: {{callbacks: {{
      title: items => {labels}[items[0].dataIndex],
      label: item => item.dataset.label + ': $' + (typeof item.raw === 'number' ? item.raw.toFixed(2) : '0')
    }}}}, legend: {{position:'top', labels: {{boxWidth:12, padding:16}}}}}},
    scales: {{x: {{stacked:true, grid:{{display:false}}, ticks:{{maxRotation:45}}}},
              y: {{stacked:true, ticks:{{callback: v => '$'+v.toFixed(0)}}, grid:{{color:'#1f1f2f'}}}}}}
  }}
}});
</script>"""


def render_heatmap(time_pat, date_range):
    date_to_idx = {d: i for i, d in enumerate(date_range)}
    points = []
    max_act = max(time_pat["day_hour"].values()) if time_pat["day_hour"] else 1
    for key, count in time_pat["day_hour"].items():
        d, h = key.split("|")
        idx = date_to_idx.get(d)
        if idx is not None:
            r = max(3, min(20, (count / max_act) * 20))
            points.append({"x": idx, "y": int(h), "r": round(r, 1), "count": count})
    labels = json.dumps(date_range)
    peak = ", ".join(f"{h}:00" for h in time_pat["peak_hours"])
    wd = f'{time_pat["weekday_avg"]:.0f}'
    we = f'{time_pat["weekend_avg"]:.0f}'
    return f"""<div class="card">
  <h2>Activity Heatmap <span class="dim">&mdash; peak: {peak} &bull; weekday avg: {wd} msgs/day &bull; weekend avg: {we} msgs/day</span></h2>
  <div class="chart-container" style="height:320px"><canvas id="heatmap"></canvas></div>
</div>
<script>
new Chart(document.getElementById('heatmap'), {{
  type: 'bubble',
  data: {{datasets:[{{label:'Messages', data:{json.dumps(points)},
    backgroundColor:'rgba(192,132,252,0.4)', borderColor:'rgba(192,132,252,0.8)', borderWidth:1}}]}},
  options: {{responsive:true, maintainAspectRatio:false,
    plugins: {{tooltip: {{callbacks: {{title:()=>'',
      label: item => {labels}[item.raw.x]+' at '+item.raw.y+':00 — '+item.raw.count+' msgs'
    }}}}, legend:{{display:false}}}},
    scales: {{
      x: {{type:'linear', min:-0.5, max:{len(date_range)-1}+0.5,
        ticks:{{callback:function(v){{const i=Math.round(v); return i>=0&&i<{len(date_range)} ? {labels}[i].slice(5):''}}, maxTicksLimit:12}},
        grid:{{display:false}}}},
      y: {{min:-0.5, max:23.5, ticks:{{callback:v=>v+':00', stepSize:3}}, grid:{{color:'#1f1f2f'}}}}
    }}
  }}
}});
</script>"""


def render_model_table(costs):
    rows = ""
    for mname in sorted(costs["model_tokens"].keys(), key=lambda m: costs["model_costs"].get(m, 0), reverse=True):
        t = costs["model_tokens"][mname]
        c = costs["model_costs"][mname]
        rows += f"""<tr>
          <td><span class="model-badge">{html_mod.escape(mname)}</span></td>
          <td class="num">{fmt_tokens(t['input'])}</td><td class="num">{fmt_tokens(t['output'])}</td>
          <td class="num">{fmt_tokens(t['cache_read'])}</td><td class="num">{fmt_tokens(t['cache_write'])}</td>
          <td class="num cost">{fmt_cost(c)}</td></tr>"""
    return f"""<div class="card"><h2>Model Breakdown</h2><table>
      <tr><th>Model</th><th class="num">Input</th><th class="num">Output</th>
          <th class="num">Cache Read</th><th class="num">Cache Write</th><th class="num">Cost</th></tr>
      {rows}
      <tr class="total-row"><td>Total</td>
        <td class="num">{fmt_tokens(costs['total_input'])}</td><td class="num">{fmt_tokens(costs['total_output'])}</td>
        <td class="num">{fmt_tokens(costs['total_cache_read'])}</td><td class="num">{fmt_tokens(costs['total_cache_write'])}</td>
        <td class="num cost">{fmt_cost(costs['total_cost'])}</td></tr>
    </table></div>"""


def render_project_table(projects):
    rows = ""
    for proj, data in projects["rows"]:
        rows += f"""<tr><td class="project-name">{html_mod.escape(proj)}</td>
          <td class="num">{data['sessions']}</td><td class="num">{data['messages']:,}</td>
          <td class="num cost">{fmt_cost(data['cost'])}</td></tr>"""
    return f"""<div class="card"><h2>Top Projects</h2><table>
      <tr><th>Project</th><th class="num">Sessions</th><th class="num">Messages</th><th class="num">Cost</th></tr>
      {rows}</table></div>"""


def render_bar_chart(chart_id, title, labels_json, date_range_json, values, color, prefix=""):
    return f"""<div class="card"><h2>{title}</h2>
  <div class="chart-container"><canvas id="{chart_id}"></canvas></div></div>
<script>
new Chart(document.getElementById('{chart_id}'), {{
  type: 'bar',
  data: {{labels: {labels_json}.map((l,i) => l || {date_range_json}[i].slice(5)),
    datasets: [{{data: {json.dumps(values)}, backgroundColor:'{color}44', borderColor:'{color}', borderWidth:1}}]}},
  options: {{responsive:true, maintainAspectRatio:false,
    plugins: {{legend:{{display:false}}, tooltip:{{callbacks:{{title: items => {date_range_json}[items[0].dataIndex]}}}}}},
    scales: {{x:{{grid:{{display:false}}, ticks:{{maxRotation:45}}}},
              y:{{grid:{{color:'#1f1f2f'}}{f", ticks:{{callback: v => '{prefix}'+v}}" if prefix else ""}}}}}
  }}
}});
</script>"""


def render_line_chart(chart_id, title, labels_json, date_range_json, datasets_config):
    return f"""<div class="card"><h2>{title}</h2>
  <div class="chart-container"><canvas id="{chart_id}"></canvas></div></div>
<script>
new Chart(document.getElementById('{chart_id}'), {{
  type: 'line',
  data: {{labels: {labels_json}.map((l,i) => l || {date_range_json}[i].slice(5)),
    datasets: {json.dumps(datasets_config)}}},
  options: {{responsive:true, maintainAspectRatio:false,
    plugins: {{legend:{{display: {json.dumps(len(datasets_config)>1)}, position:'top', labels:{{boxWidth:12}}}},
      tooltip:{{callbacks:{{title: items => {date_range_json}[items[0].dataIndex]}}}}}},
    scales: {{x:{{grid:{{display:false}}, ticks:{{maxRotation:45}}}},
              y:{{grid:{{color:'#1f1f2f'}}, beginAtZero:true}}}}
  }}
}});
</script>"""


def render_session_insights(sess):
    # Length distribution as horizontal bar
    buckets = sess["length_buckets"]
    bucket_labels = json.dumps(list(buckets.keys()))
    bucket_values = json.dumps(list(buckets.values()))

    # Longest sessions table
    dur_rows = ""
    for sid, proj, dur_ms, msgs, date in sess["top_by_duration"]:
        dur_rows += f"""<tr><td class="dim">{date}</td><td class="project-name">{html_mod.escape(prettify_project(proj))}</td>
          <td class="num">{msgs}</td><td class="num">{fmt_duration(dur_ms)}</td></tr>"""

    return f"""<div class="two-col">
  <div class="card"><h2>Session Length Distribution</h2>
    <div class="chart-container">
      <canvas id="lenDist"></canvas>
    </div>
  </div>
  <div class="card"><h2>Longest Sessions (by thinking time)</h2>
    <table><tr><th>Date</th><th>Project</th><th class="num">Msgs</th><th class="num">Duration</th></tr>
    {dur_rows}</table>
  </div>
</div>
<script>
new Chart(document.getElementById('lenDist'), {{
  type: 'bar',
  data: {{labels: {bucket_labels}, datasets: [{{
    data: {bucket_values}, backgroundColor: '#c084fc44', borderColor: '#c084fc', borderWidth: 1
  }}]}},
  options: {{indexAxis:'y', responsive:true, maintainAspectRatio:false,
    plugins: {{legend:{{display:false}}}},
    scales: {{x:{{grid:{{color:'#1f1f2f'}}}}, y:{{grid:{{display:false}}}}}}
  }}
}});
</script>"""


def render_efficiency(eff, date_range):
    labels = json.dumps(date_range)
    short = json.dumps([d[5:] if i % max(1, len(date_range) // 10) == 0 else ""
                        for i, d in enumerate(date_range)])

    # Tool usage horizontal bar
    tool_labels = json.dumps([t[0] for t in eff["top_tools"]])
    tool_values = json.dumps([t[1] for t in eff["top_tools"]])
    tool_colors = json.dumps(["#c084fc" if t[0] in ("Agent",) else
                               "#22d3ee" if t[0] in ("Bash",) else
                               "#34d399" if t[0] in ("Read", "Grep", "Glob") else
                               "#fb923c" if t[0] in ("Edit", "Write") else
                               "#94a3b8" for t in eff["top_tools"]])

    # Cache rate and turn duration lines
    cache_vals = [eff["daily_cache_rate"].get(d, 0) for d in date_range]
    dur_vals = [eff["avg_turn_dur"].get(d, 0) for d in date_range]
    error_vals = [eff["daily_errors"].get(d, 0) for d in date_range]
    compact_vals = [eff["daily_compactions"].get(d, 0) for d in date_range]

    err_compact_note = ""
    if eff["total_errors"] or eff["total_compactions"]:
        err_compact_note = f' &bull; {eff["total_errors"]} API errors &bull; {eff["total_compactions"]} compactions'

    return f"""<div class="two-col">
  <div class="card"><h2>Tool Usage</h2>
    <div class="chart-container"><canvas id="toolChart"></canvas></div>
  </div>
  <div class="card"><h2>Cache Hit Rate <span class="dim">{err_compact_note}</span></h2>
    <div class="chart-container"><canvas id="cacheChart"></canvas></div>
  </div>
</div>
<script>
new Chart(document.getElementById('toolChart'), {{
  type: 'bar',
  data: {{labels: {tool_labels}, datasets: [{{
    data: {tool_values}, backgroundColor: {tool_colors}, borderWidth: 0
  }}]}},
  options: {{indexAxis:'y', responsive:true, maintainAspectRatio:false,
    plugins: {{legend:{{display:false}}}},
    scales: {{x:{{grid:{{color:'#1f1f2f'}}}}, y:{{grid:{{display:false}}}}}}
  }}
}});
new Chart(document.getElementById('cacheChart'), {{
  type: 'line',
  data: {{labels: {short}.map((l,i) => l || {labels}[i].slice(5)),
    datasets: [
      {{label:'Cache %', data:{json.dumps(cache_vals)}, borderColor:'#34d399', borderWidth:2, pointRadius:0, fill:false}},
      {{label:'Errors', data:{json.dumps(error_vals)}, borderColor:'#f87171', borderWidth:1.5, pointRadius:0, fill:false, yAxisID:'y1'}},
      {{label:'Compactions', data:{json.dumps(compact_vals)}, borderColor:'#fb923c', borderWidth:1.5, pointRadius:0, fill:false, yAxisID:'y1'}}
    ]}},
  options: {{responsive:true, maintainAspectRatio:false,
    plugins: {{legend:{{position:'top', labels:{{boxWidth:12, padding:12}}}},
      tooltip:{{callbacks:{{title: items => {labels}[items[0].dataIndex]}}}}}},
    scales: {{
      x:{{grid:{{display:false}}, ticks:{{maxRotation:45}}}},
      y:{{grid:{{color:'#1f1f2f'}}, ticks:{{callback:v=>v+'%'}}, min:0, max:100}},
      y1:{{position:'right', grid:{{display:false}}, min:0, ticks:{{stepSize:1}}}}
    }}
  }}
}});
</script>
""" + render_line_chart("turnDur", "Avg Turn Duration (ms)", short, labels,
    [{"label": "Duration", "data": dur_vals, "borderColor": "#22d3ee",
      "borderWidth": 2, "pointRadius": 0, "fill": False}])


def render_skills(skills, date_range):
    if not skills["top_skills"] and not skills["top_commands"]:
        return ""

    labels = json.dumps(date_range)
    short = json.dumps([d[5:] if i % max(1, len(date_range) // 10) == 0 else ""
                        for i, d in enumerate(date_range)])

    # Skills table
    skill_rows = ""
    for skill, count, top_proj, n_projs in skills["skill_project_rows"][:15]:
        skill_rows += f"""<tr>
          <td><span class="skill-badge">{html_mod.escape(skill)}</span></td>
          <td class="num">{count}</td>
          <td class="project-name">{html_mod.escape(top_proj)}</td>
          <td class="num dim">{n_projs}</td></tr>"""

    # Commands table
    cmd_rows = ""
    for cmd, count in skills["top_commands"][:10]:
        cmd_rows += f"""<tr>
          <td><span class="cmd-badge">{html_mod.escape(cmd)}</span></td>
          <td class="num">{count}</td></tr>"""

    # Skills frequency chart
    skill_chart = ""
    if skills["top_skills"]:
        s_labels = json.dumps([s[0] for s in skills["top_skills"][:12]])
        s_values = json.dumps([s[1] for s in skills["top_skills"][:12]])
        skill_chart = f"""<div class="card"><h2>Skill Usage</h2>
    <div class="chart-container"><canvas id="skillBarChart"></canvas></div></div>
<script>
new Chart(document.getElementById('skillBarChart'), {{
  type: 'bar',
  data: {{labels: {s_labels}, datasets: [{{
    data: {s_values}, backgroundColor: '#a855f744', borderColor: '#a855f7', borderWidth: 1
  }}]}},
  options: {{indexAxis:'y', responsive:true, maintainAspectRatio:false,
    plugins: {{legend:{{display:false}}}},
    scales: {{x:{{grid:{{color:'#1f1f2f'}}}}, y:{{grid:{{display:false}}}}}}
  }}
}});
</script>"""

    # Daily skill invocations line
    daily_chart = render_line_chart("skillDaily", "Skill Invocations Over Time", short, labels,
        [{"label": "Skills", "data": skills["daily_skill_count"],
          "borderColor": "#a855f7", "borderWidth": 2, "pointRadius": 0, "fill": False}])

    left = f"""<div class="card"><h2>Skills <span class="dim">&mdash; {skills['total_skill_uses']} invocations</span></h2>
    <table><tr><th>Skill</th><th class="num">Uses</th><th>Top Project</th><th class="num">Projects</th></tr>
    {skill_rows}</table></div>"""

    right_content = ""
    if cmd_rows:
        right_content = f"""<div class="card"><h2>Slash Commands <span class="dim">&mdash; {skills['total_commands']} total</span></h2>
    <table><tr><th>Command</th><th class="num">Uses</th></tr>
    {cmd_rows}</table></div>"""
    else:
        right_content = skill_chart

    parts = [
        '<div class="two-col">',
        left,
        right_content,
        '</div>',
    ]
    if cmd_rows and skill_chart:
        parts.append('<div class="two-col">')
        parts.append(skill_chart)
        parts.append(daily_chart)
        parts.append('</div>')
    elif skill_chart or skills["daily_skill_count"]:
        parts.append(daily_chart)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main HTML assembly
# ---------------------------------------------------------------------------

CSS = """<style>
:root{--bg:#0f0f13;--card:#1a1a24;--border:#2a2a3a;--text:#e4e4ef;--text-dim:#8888a0;
--accent:#c084fc;--green:#34d399;--cyan:#22d3ee;--red:#f87171;--orange:#fb923c}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
background:var(--bg);color:var(--text);padding:24px;max-width:1200px;margin:0 auto}
h1{font-size:28px;margin-bottom:4px}
.subtitle{color:var(--text-dim);font-size:14px;margin-bottom:24px}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:16px;margin-bottom:24px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px}
.stat-card .label{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);margin-bottom:8px}
.stat-card .value{font-size:26px;font-weight:700}
.stat-card .value.cost,.green{color:var(--green)}.stat-card .value.accent{color:var(--accent)}
.stat-card .value.cyan{color:var(--cyan)}
.stat-card .sub{font-size:12px;color:var(--text-dim);margin-top:4px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:20px}
.card h2{font-size:16px;margin-bottom:16px;color:var(--text-dim);font-weight:500}
.card h2 .dim{font-size:13px;color:var(--text-dim);font-weight:400}
.chart-container{position:relative;height:260px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:800px){.two-col{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--text-dim);font-weight:500;font-size:12px;text-transform:uppercase;
letter-spacing:.5px;padding:8px 12px;border-bottom:1px solid var(--border)}
td{padding:10px 12px;border-bottom:1px solid var(--border)}
tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.cost{color:var(--green);font-weight:600}
.model-badge{background:#2a2a3a;padding:3px 10px;border-radius:6px;font-size:13px;font-weight:500}
.skill-badge{background:#2d1f4e;color:#c084fc;padding:3px 10px;border-radius:6px;font-size:13px;font-family:'SF Mono','Fira Code',monospace}
.cmd-badge{background:#1f3a2d;color:#34d399;padding:3px 10px;border-radius:6px;font-size:13px;font-family:'SF Mono','Fira Code',monospace}
.project-name{font-family:'SF Mono','Fira Code',monospace;font-size:13px;color:var(--cyan)}
.dim{color:var(--text-dim)}
.total-row td{font-weight:700;border-top:2px solid var(--border);padding-top:12px}
.footer{text-align:center;color:var(--text-dim);font-size:12px;margin-top:32px;padding-top:16px;
border-top:1px solid var(--border)}
.section-title{font-size:18px;font-weight:600;margin:32px 0 16px;padding-top:16px;
border-top:1px solid var(--border);color:var(--text-dim)}
</style>"""


def generate_html(costs, sess, time_pat, eff, projects, skills, stats_cache, days):
    today = datetime.now(timezone.utc).date()
    date_range = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    labels = json.dumps(date_range)
    short = json.dumps([d[5:] if i % max(1, len(date_range) // 10) == 0 else ""
                        for i, d in enumerate(date_range)])

    msg_vals = [sess["daily_messages"].get(d, 0) for d in date_range]
    sess_vals = [len(sess["daily_sessions"].get(d, set())) for d in date_range]

    parts = [
        f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Claude Code Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
{CSS}
</head><body>
<h1>Claude Code Dashboard</h1>
<p class="subtitle">Usage analytics &bull; Last {days} days &bull;
Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
<script>
Chart.defaults.color='#8888a0';Chart.defaults.borderColor='#2a2a3a';
Chart.defaults.font.family="-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif";
</script>""",
        render_summary(costs, sess, eff, stats_cache),
        render_cost_chart(costs, date_range),
        render_heatmap(time_pat, date_range),
        '<div class="two-col">',
        render_model_table(costs),
        render_project_table(projects),
        '</div>',
        '<div class="two-col">',
        render_bar_chart("msgChart", "Daily Messages", short, labels, msg_vals, "#22d3ee"),
        render_bar_chart("sessChart", "Daily Sessions", short, labels, sess_vals, "#c084fc"),
        '</div>',
        '<div class="section-title">Session Insights</div>',
        render_session_insights(sess),
        '<div class="section-title">Skills &amp; Commands</div>',
        render_skills(skills, date_range),
        '<div class="section-title">Efficiency &amp; Tooling</div>',
        render_efficiency(eff, date_range),
        f"""<div class="footer">Claude Code Local Dashboard &bull;
Cost estimates based on public API pricing &mdash; included in your Max/Pro subscription</div>
</body></html>""",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    days = 30
    use_cache = True
    open_browser = True
    output_path = Path.home() / "claude-dashboard.html"

    args = sys.argv[1:]
    for arg in args:
        if arg == "--no-cache":
            use_cache = False
        elif arg == "--no-open":
            open_browser = False
        elif arg.startswith("--output="):
            output_path = Path(arg.split("=", 1)[1])
        elif arg.startswith("-"):
            print(f"Usage: {sys.argv[0]} [days] [--no-cache] [--no-open] [--output=PATH]")
            sys.exit(1)
        else:
            try:
                days = int(arg)
            except ValueError:
                print(f"Usage: {sys.argv[0]} [days] [--no-cache] [--no-open] [--output=PATH]")
                sys.exit(1)
    if days < 1:
        print("Error: days must be >= 1")
        sys.exit(1)

    print(f"Scanning Claude Code sessions (last {days} days)...")
    all_summaries = scan_sessions(use_cache)
    sessions = filter_by_days(all_summaries, days)
    print(f"  {len(sessions)} sessions in range")

    today = datetime.now(timezone.utc).date()
    date_range = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]

    print("Analyzing...")
    costs = analyze_costs(sessions, date_range)
    sess = analyze_sessions(sessions, date_range)
    time_pat = analyze_time_patterns(sessions, date_range)
    eff = analyze_efficiency(sessions, date_range)
    projects = analyze_projects(sessions)
    history_cmds = load_slash_commands(days)
    skills = analyze_skills(sessions, date_range, history_cmds)
    stats_cache = None
    if STATS_CACHE.exists():
        try:
            with open(STATS_CACHE) as f:
                stats_cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    print("Generating dashboard...")
    html = generate_html(costs, sess, time_pat, eff, projects, skills, stats_cache, days)
    output_path.write_text(html)
    print(f"Dashboard written to {output_path}")

    if open_browser:
        try:
            webbrowser.open(f"file://{output_path}")
        except Exception:
            print(f"Open manually: file://{output_path}")


if __name__ == "__main__":
    main()
