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
        f"*Snapshot: {m['fetched_at']} | Scoring period {m['scoring_period']}*", "",
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

    header = ["Slot", "Player", "Pos", "Team", "Opp", "Status", "Proj", "Actual", "Locked"]
    starters = [p for p in snap["roster"] if p["slot_id"] not in NON_STARTING_SLOTS]
    reserves = [p for p in snap["roster"] if p["slot_id"] in NON_STARTING_SLOTS]
    for title, group in (("## 3. Starting Lineup", starters), ("## 4. Bench & IR", reserves)):
        out += [title, _row(header), _row([":---"] * len(header))]
        for p in group:
            out.append(_row([p["slot"], p["name"], p["pos"], p["pro_team"], p["opponent"],
                             p["status"] or "OK", p["projected"], p["actual"], "yes" if p["locked"] else ""]))
        out.append("")

    out.append("## 5. Alerts")
    out += [f"- [{a['level'].upper()}] {a['text']}" for a in snap["alerts"]] or ["- None."]

    out += ["", "## 6. Waiver Wire — Top % Add Movers"]
    fa = [f for f in snap["free_agents"] if f.get("pct_change") is not None]
    fa.sort(key=lambda f: f["pct_change"], reverse=True)
    h = ["Player", "Pos", "Team", "Opp", "Proj", "% Rost", "% Chg", "Status"]
    out += [_row(h), _row([":---"] * len(h))]
    for f in fa[:fa_limit]:
        out.append(_row([f["name"], f["pos"], f["pro_team"], f["opponent"], f["projected"],
                         f"{f['pct_owned']}%", f"{f['pct_change']:+.2f}%", f["status"] or "OK"]))
    for pos in ("QB", "RB", "WR", "TE", "D/ST", "K"):
        grp = sorted([f for f in snap["free_agents"] if f["pos"] == pos],
                     key=lambda f: (f.get("pct_change") or 0, f["projected"]), reverse=True)[:5]
        if grp:
            out.append(f"\n**{pos}:** " + "; ".join(
                f"{f['name']} ({f['pro_team']}, proj {f['projected']}, {f['pct_owned']}% rost"
                + (f", {f['pct_change']:+.1f}%" if f.get("pct_change") is not None else "") + ")" for f in grp))
    return "\n".join(out)


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
