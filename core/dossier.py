"""Markdown dossier built from a snapshot. Feeds the AI, the .md export, and the Word export."""
from __future__ import annotations

from .espn_client import SLOT_NAMES, NON_STARTING_SLOTS, IR_SLOT


def _row(cells):
    return "| " + " | ".join(str(c) for c in cells) + " |"


def spread_text(spread: float) -> str:
    if spread > 0:
        return f"Favored by {spread:.2f}"
    if spread < 0:
        return f"Underdog by {abs(spread):.2f}"
    return "Pick'em"


def build_markdown(snap: dict, fa_limit: int = 10) -> str:
    m, mu = snap["meta"], snap["matchup"]
    out = [
        f"# ESPN Fantasy Dossier | Week {m['week']}",
        f"**Team:** {m['team_name']} | **Record:** {m['record']} | **Standing:** {m.get('standing')}",
        f"*Snapshot: {m['fetched_at']} | Scoring period {m['scoring_period']} | Opp: '@DET' = away at DET, 'DET' = home vs DET; kickoff in local time*",
        ("**UPCOMING WEEK (analysis only):** the lineup shown is my current lineup carried forward; live scores "
         "are zero. Plan start/sit and waiver moves for this week." if m.get("mode") == "next" else ""), "",
        "## 1. Pending Moves & Trade Pipeline",
    ]
    if snap["pending"]:
        for pm in snap["pending"]:
            out.append(f"- **{pm['type']}** ({pm['status']}) processing {pm['process_date']} with {pm['partner']}")
            if pm.get("incoming"):
                out.append(f"  - Incoming: {', '.join(pm['incoming'])}")
            if pm.get("outgoing"):
                out.append(f"  - Outgoing: {', '.join(pm['outgoing'])}")
    else:
        out.append("- No pending trades or waiver claims.")

    out += ["", f"## 2. Matchup (Week {m['week']})",
            f"- Opponent: {mu['opponent']}",
            f"- Projected: {m['team_name']} {mu['my_projected']} vs {mu['opp_projected']}",
            f"- Live score: {mu['my_score']} - {mu['opp_score']}",
            f"- Spread: {spread_text(mu['spread'])}", ""]

    header = ["Slot", "Player", "Pos", "Team", "Opp", "Kickoff", "Status", "Proj", "Actual", "Locked"]
    starters = [p for p in snap["roster"] if p["slot_id"] not in NON_STARTING_SLOTS]
    reserves = [p for p in snap["roster"] if p["slot_id"] in NON_STARTING_SLOTS]
    for title, group in (("## 3. Starting Lineup", starters), ("## 4. Bench & IR", reserves)):
        out += [title, _row(header), _row([":---"] * len(header))]
        for p in group:
            out.append(_row([p["slot"], p["name"], p["pos"], p["pro_team"], p["opponent"], p.get("kickoff", ""),
                             p["status"] or "OK", p["projected"], p["actual"], "yes" if p["locked"] else ""]))
        out.append("")

    out.append("## 5. Alerts")
    out += [f"- [{a['level'].upper()}] {a['text']}" for a in snap["alerts"]] or ["- None."]

    out += ["", "## 6. Waiver Wire — Top % Add Movers"]
    fa = [f for f in snap["free_agents"] if f.get("pct_change") is not None]
    fa.sort(key=lambda f: f["pct_change"], reverse=True)
    h = ["Player", "Pos", "Team", "Opp", "Kickoff", "Proj", "% Rost", "% Chg", "Status"]
    out += [_row(h), _row([":---"] * len(h))]
    for f in fa[:fa_limit]:
        out.append(_row([f["name"], f["pos"], f["pro_team"], f["opponent"], f.get("kickoff", ""), f["projected"],
                         f"{f['pct_owned']}%", f"{f['pct_change']:+.2f}%", f["status"] or "OK"]))
    for pos in ("QB", "RB", "WR", "TE", "D/ST", "K"):
        grp = sorted([f for f in snap["free_agents"] if f["pos"] == pos],
                     key=lambda f: (f.get("pct_change") or 0, f["projected"]), reverse=True)[:5]
        if grp:
            out.append(f"\n**{pos}:** " + "; ".join(
                f"{f['name']} ({f['pro_team']} {f['opponent']}{(' ' + f['kickoff']) if f.get('kickoff') else ''}, proj {f['projected']}, {f['pct_owned']}% rost"
                + (f", {f['pct_change']:+.1f}%" if f.get("pct_change") is not None else "") + ")" for f in grp))
    out += intel_markdown(snap.get("intel"))
    out += confidence_markdown(snap.get("confidence"))
    return "\n".join(out)


def confidence_markdown(conf: dict | None, per_slot: int = 5) -> list[str]:
    if not conf:
        return []
    out = ["", "## 8. Start/sit confidence (statistical model)",
           f"P = probability the alternative outscores my starter this week. Pool: {conf['pool_size']} obtainable "
           "players (bench, free agents, waivers, other teams via trade). Points ~ Normal(mean, sd); sd = position CV "
           "x projection, CV measured from this league's projected-vs-actual history: " +
           ", ".join(f"{k} {v['cv']} (n={v['samples']})" for k, v in conf["model"].items()) + "."]
    cal = conf.get("calibration") or {}
    if cal.get("pairs"):
        out.append(f"Calibration so far: {cal['pairs']:,} graded pairs, Brier {cal['brier']}; " + "; ".join(
            f"{r['bucket']} predicted -> {r['actual_hit_rate']:.0%} happened (n={r['pairs']})"
            for r in cal["table"] if r["pairs"]) + ".")
    h = ["Alternative", "Tier", "P", "Opp", "Kickoff", "Status", "Practice", "Depth", "Mean±SD", "80th",
         "Season avg", "Last 3 act (proj)", "OPRK"]
    for s in conf["slots"]:
        out.append(f"\n**{s['slot']}: {s['name']}** ({s['opponent']} {s['kickoff']}; mean {s['mean']} ± {s['sd']}, "
                   f"80th {s['p80']}{', LOCKED' if s['locked'] else ''}{', ' + s['note'] if s['note'] else ''})")
        rows = s["alternatives"][:per_slot]
        best = [a for a in s["best_by_tier"].values() if a and a not in rows]
        if not rows:
            out.append("- No alternatives.")
            continue
        out += [_row(h), _row([":---"] * len(h))]
        for a in rows + best:
            out.append(_row([a["name"] + (f" ({a['owner']})" if a["owner"] else ""), a["tier"], f"{a['p_outscores']:.0%}",
                             a["opponent"], a["kickoff"], a["status"] or "OK", a["practice"] or "-", a["depth"] or "-",
                             f"{a['mean']}±{a['sd']}", a["p80"], a["season_avg"] if a["season_avg"] is not None else "-",
                             a["last3"] or "-", a["oprk"] or "-"]))
    return out


def intel_markdown(intel: dict | None) -> list[str]:
    if not intel:
        return []
    from .intel import flags
    out = ["", f"## 7. External intel (Sleeper, pulled {intel.get('fetched_at', '?')})"]
    for e in intel.get("errors", []):
        out.append(f"- Unavailable: {e}")
    fl = flags(intel)
    if fl:
        out.append("**Status disagreements / practice flags:**")
        out += [f"- {x}" for x in fl]
    rows = [r for r in intel.get("roster", []) if r["matched"]]
    if rows:
        h = ["Player", "ESPN status", "Sleeper injury", "Practice", "Depth chart"]
        out += ["", _row(h), _row([":---"] * len(h))]
        for r in rows:
            out.append(_row([r["name"], r["espn_status"], (r["injury"] + (f" ({r['body_part']})" if r["body_part"] else "")) or "-",
                             r["practice"] or "-", r["depth"] or "-"]))
    for kind, title in (("trending_add", "Most added in the last 24h (all Sleeper leagues)"),
                        ("trending_drop", "Most dropped in the last 24h (all Sleeper leagues)")):
        rows = intel.get(kind, [])[:15]
        if rows:
            out.append(f"\n**{title}:** " + "; ".join(
                f"{r['name']} ({r['pos']}, {r['team']}, {r['count']:,}"
                + (f", {r['injury']}" if r["injury"] else "")
                + (f", {r['in_my_league']}" if r["in_my_league"] else "") + ")" for r in rows))
    out.append("\n*Trending data courtesy of Sleeper.*")
    return out


def lineup_table_for_ai(snap: dict) -> str:
    """Compact machine-oriented roster table with ESPN slot ids for JSON lineup requests."""
    lines = ["player_id | name | pos | status | proj | locked | current_slot_id | eligible_slot_ids"]
    for p in snap["roster"]:
        if p["slot_id"] == IR_SLOT:
            continue
        elig = [s for s in p["eligible_slot_ids"] if s not in NON_STARTING_SLOTS]
        lines.append(f"{p['player_id']} | {p['name']} | {p['pos']} | {p['status'] or 'OK'}"
                     f"{' BYE' if p['on_bye'] else ''} | {p['projected']} | {p['locked']} | "
                     f"{p['slot_id']} | {elig}")
    caps = ", ".join(f"{sid} ({SLOT_NAMES.get(int(sid), sid)}) x{c}" for sid, c in snap["slot_counts"].items())
    return "\n".join(lines) + f"\n\nStarting slot capacities: {caps}\nBench slot id: 20"
