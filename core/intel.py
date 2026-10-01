"""
External intel from Sleeper's public API (https://docs.sleeper.com), joined to ESPN by espn_id.

Sleeper's terms, followed here:
  * Free for non-commercial use, no key; stay well under 1000 calls/minute.
  * /players/nfl is ~5MB and meant to be pulled at most once a day and stored locally.
    We fetch active players only, keep a slim index on disk, and refresh after 20 hours.
  * Trending data requires attribution to Sleeper (shown in the dossier and UI).

Everything here is best-effort: any network or parsing failure is recorded in
intel["errors"] and the rest of the dashboard keeps working without it.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import requests

from .config import data_dir

API = "https://api.sleeper.app/v1"
CACHE_HOURS = 20
TIMEOUT = 20
KEEP = ("full_name", "first_name", "last_name", "team", "position", "status", "injury_status",
        "injury_body_part", "injury_notes", "practice_participation", "practice_description",
        "depth_chart_position", "depth_chart_order", "espn_id", "news_updated")


def _cache_path() -> Path:
    return data_dir() / "sleeper_players.json"


def _slim(pid: str, p: dict) -> dict:
    out = {k: p.get(k) for k in KEEP if p.get(k) not in (None, "")}
    if not out.get("full_name"):
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        out["full_name"] = name or pid
    return out


def load_players(force: bool = False) -> tuple[dict, str]:
    """Return ({sleeper_id: slim player}, note). Uses the on-disk copy if it's fresh."""
    path = _cache_path()
    if path.exists() and not force:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - cached.get("fetched", 0) < CACHE_HOURS * 3600:
            return cached["players"], f"Sleeper player index from {cached.get('fetched_at', '?')} (cached)"
    resp = requests.get(f"{API}/players/nfl", params={"active": "true"}, timeout=TIMEOUT)
    resp.raise_for_status()
    players = {pid: _slim(pid, p) for pid, p in resp.json().items() if isinstance(p, dict)}
    stamp = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps({"fetched": time.time(), "fetched_at": stamp, "players": players}), encoding="utf-8")
    return players, f"Sleeper player index refreshed {stamp}"


def trending(kind: str, hours: int = 24, limit: int = 25) -> list[dict]:
    resp = requests.get(f"{API}/players/nfl/trending/{kind}",
                        params={"lookback_hours": hours, "limit": limit}, timeout=TIMEOUT)
    resp.raise_for_status()
    return [r for r in resp.json() if isinstance(r, dict)]


def _depth(p: dict) -> str:
    pos, order = p.get("depth_chart_position"), p.get("depth_chart_order")
    return f"{pos or p.get('position', '')}{order}" if order else ""


def build_intel(snapshot: dict) -> dict:
    """Sleeper view of my roster, top free agents, and league-wide trending adds/drops."""
    intel = {"fetched_at": datetime.now().isoformat(timespec="seconds"), "source": "Sleeper",
             "roster": [], "free_agents": {}, "trending_add": [], "trending_drop": [], "errors": [], "note": ""}
    try:
        players, intel["note"] = load_players()
    except Exception as exc:
        intel["errors"].append(f"Player index unavailable: {exc}")
        return intel

    by_espn = {str(p["espn_id"]): p for p in players.values() if p.get("espn_id")}
    fa_ids = {str(f["player_id"]) for f in snapshot.get("free_agents", [])}
    mine = {str(p["player_id"]) for p in snapshot.get("roster", [])}

    for p in snapshot.get("roster", []):
        sp = by_espn.get(str(p["player_id"]))
        intel["roster"].append({
            "player_id": p["player_id"], "name": p["name"], "espn_status": p.get("status_raw") or "ACTIVE",
            "matched": sp is not None,
            "injury": (sp or {}).get("injury_status") or "",
            "body_part": (sp or {}).get("injury_body_part") or "",
            "practice": (sp or {}).get("practice_participation") or (sp or {}).get("practice_description") or "",
            "depth": _depth(sp) if sp else "",
            "roster_status": (sp or {}).get("status") or "",
        })
    for f in snapshot.get("free_agents", []):
        sp = by_espn.get(str(f["player_id"]))
        if sp and (sp.get("injury_status") or sp.get("depth_chart_order")):
            intel["free_agents"][str(f["player_id"])] = {"injury": sp.get("injury_status") or "",
                                                         "depth": _depth(sp)}

    for kind in ("add", "drop"):
        try:
            rows = trending(kind)
        except Exception as exc:
            intel["errors"].append(f"Trending {kind}s unavailable: {exc}")
            continue
        for r in rows:
            pid = str(r.get("player_id", ""))
            sp = players.get(pid, {})
            espn_id = str(sp.get("espn_id", "")) if sp.get("espn_id") else ""
            if pid.isalpha():  # team defenses are keyed by team code, e.g. "CHI"
                name, pos, team = f"{pid} D/ST", "D/ST", pid
                avail = "on waivers/FA" if any(f["pos"] == "D/ST" and f["pro_team"] == pid
                                               for f in snapshot.get("free_agents", [])) else ""
            else:
                name, pos, team = sp.get("full_name", f"Sleeper {pid}"), sp.get("position", ""), sp.get("team", "")
                avail = ("on my roster" if espn_id in mine else
                         "on waivers/FA" if espn_id in fa_ids else "")
            intel[f"trending_{kind}"].append({
                "name": name, "pos": pos, "team": team or "FA", "count": int(r.get("count", 0) or 0),
                "injury": sp.get("injury_status") or "", "in_my_league": avail})
    return intel


def flags(intel: dict) -> list[str]:
    """Plain-language disagreements worth the AI's attention."""
    out = []
    for r in intel.get("roster", []):
        if not r["matched"]:
            continue
        espn = (r["espn_status"] or "ACTIVE").upper()
        slp = (r["injury"] or "").upper()
        if slp and slp not in espn and not (slp == "IR" and "INJURY_RESERVE" in espn):
            out.append(f"{r['name']}: ESPN says {espn}, Sleeper says {r['injury']}"
                       + (f" ({r['body_part']})" if r['body_part'] else "") + ".")
        if r["practice"] and r["practice"].upper() in {"DNP", "LIMITED", "LP"}:
            out.append(f"{r['name']}: practice participation {r['practice']}.")
    return out
