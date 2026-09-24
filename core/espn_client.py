"""
ESPN read client: league connection, matchup, roster, pending moves, waiver wire.

Two corrections to the original FFB_Monitor.py, both verified against the
installed espn-api source (0.46.x):

1. Header spoofing. The original set `espn_api.base_league.BaseLeague.headers`,
   but no code in espn-api reads that attribute -- every request goes through
   `requests.get(..., headers=<per-call>)` inside espn_api/requests/espn_requests.py.
   The patch was a silent no-op. We now swap that module's `requests` reference
   for a Session carrying browser headers, so every espn-api call actually sends them.

2. "% Add velocity". espn-api's Player object has no `percent_change` attribute,
   so `getattr(p, "percent_change", 0.0)` was always 0 and the "velocity sort"
   silently fell back to % owned. We read `ownership.percentChange` straight from
   the kona_player_info payload instead.
"""
from __future__ import annotations

import json
from datetime import datetime

import requests as _requests
import espn_api.requests.espn_requests as _espn_requests_mod

from .auth import sanitize_cookies
from .config import Settings

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://fantasy.espn.com/",
    "Origin": "https://fantasy.espn.com",
}


class _HeaderedRequests:
    """Drop-in for the `requests` module inside espn_api that adds browser headers."""

    def __init__(self):
        self._session = _requests.Session()
        self._session.headers.update(BROWSER_HEADERS)

    def get(self, url, params=None, headers=None, cookies=None, **kwargs):
        kwargs.setdefault("timeout", 30)
        return self._session.get(url, params=params, headers=headers, cookies=cookies, **kwargs)

    def __getattr__(self, name):
        return getattr(_requests, name)


_espn_requests_mod.requests = _HeaderedRequests()

from espn_api.football import League  # noqa: E402  (import after patch on purpose)
from espn_api.football.constant import POSITION_MAP, PRO_TEAM_MAP  # noqa: E402

SLOT_NAMES = {k: v for k, v in POSITION_MAP.items() if isinstance(k, int)}
SLOT_NAMES[23] = "FLEX"
BENCH_SLOT, IR_SLOT = 20, 21
NON_STARTING_SLOTS = {BENCH_SLOT, IR_SLOT, 22}
DEFAULT_POSITION = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
FA_POSITION_SLOTS = {"QB": 0, "RB": 2, "WR": 4, "TE": 6, "D/ST": 16, "K": 17}

STATUS_SHORT = {
    "ACTIVE": "", "NORMAL": "", None: "", "": "",
    "QUESTIONABLE": "Q", "DOUBTFUL": "D", "OUT": "O", "INJURY_RESERVE": "IR",
    "SUSPENSION": "SSPD", "DAY_TO_DAY": "DTD", "PROBABLE": "P",
}
UNSTARTABLE = {"O", "IR", "SSPD"}


class EspnError(RuntimeError):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind  # auth | config | error


def connect(settings: Settings):
    if settings.missing:
        raise EspnError(f"Missing in .env: {', '.join(settings.missing)}", "config")
    s2, swid = sanitize_cookies(settings.espn_s2, settings.swid)
    try:
        league = League(league_id=int(settings.league_id), year=int(settings.season),
                        espn_s2=s2, swid=swid)
    except Exception as exc:  # espn-api raises ESPNAccessDenied / ESPNInvalidLeague / generic
        name = type(exc).__name__
        kind = "auth" if "Access" in name or "401" in str(exc) else "error"
        raise EspnError(f"{name}: {exc}", kind) from exc

    team = next((t for t in league.teams if t.team_id == int(settings.team_id)), None)
    if team is None:
        roster = ", ".join(f"{t.team_id}={t.team_name}" for t in league.teams)
        raise EspnError(f"Team {settings.team_id} not in league {settings.league_id}. Teams: {roster}", "config")
    return league, team


# ----------------------------------------------------------------------------- helpers

def _raw(league, views, **params) -> dict:
    p = {"view": views}
    p.update(params)
    return league.espn_request.league_get(params=p)


def _projection_from_raw(player: dict, scoring_period: int) -> float:
    for st in player.get("stats", []) or []:
        if st.get("scoringPeriodId") == scoring_period and st.get("statSourceId") == 1:
            return round(float(st.get("appliedTotal", 0.0) or 0.0), 2)
    return 0.0


def _pos_for(player: dict) -> str:
    pos = DEFAULT_POSITION.get(player.get("defaultPositionId"))
    if pos:
        return pos
    for slot in player.get("eligibleSlots", []):
        if slot in SLOT_NAMES and slot not in NON_STARTING_SLOTS:
            return SLOT_NAMES[slot]
    return "?"


def _status_code(raw_status) -> str:
    return STATUS_SHORT.get(raw_status, str(raw_status or "")[:4].upper())


def _pro_schedule(league, week) -> dict:
    try:
        return league._get_pro_schedule(week)  # {proTeamId: (oppProTeamId, epoch_ms)}
    except Exception:
        return {}


# ----------------------------------------------------------------------------- snapshot

def build_snapshot(league, team, settings: Settings, fa_pool_per_pos: int = 75) -> dict:
    week = league.current_week
    status = _raw(league, "mStatus").get("status", {})
    scoring_period = status.get("transactionScoringPeriod") or league.scoringPeriodId

    settings_raw = _raw(league, "mSettings").get("settings", {})
    slot_counts = {
        int(k): int(v)
        for k, v in settings_raw.get("rosterSettings", {}).get("lineupSlotCounts", {}).items()
        if int(v) > 0 and int(k) not in NON_STARTING_SLOTS
    }

    matchup, box_players = _matchup(league, team, week)
    roster = _roster(league, team, scoring_period, box_players)
    owners = [m.get("id", "") if isinstance(m, dict) else str(m) for m in (team.owners or [])]
    _, clean_swid = sanitize_cookies("", settings.swid)

    snap = {
        "meta": {
            "league_id": settings.league_id,
            "league_name": getattr(league.settings, "name", ""),
            "team_id": team.team_id,
            "team_name": team.team_name,
            "record": f"{team.wins}-{team.losses}" + (f"-{team.ties}" if getattr(team, "ties", 0) else ""),
            "standing": getattr(team, "standing", None),
            "season": settings.season,
            "week": week,
            "scoring_period": scoring_period,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        },
        "matchup": matchup,
        "roster": roster,
        "slot_counts": slot_counts,
        "pending": pending_transactions(league, team.team_id),
        "free_agents": free_agents(league, week, fa_pool_per_pos),
        "owners": owners,
        "swid_owns_team": clean_swid.lower() in {o.lower() for o in owners} if owners else False,
    }
    snap["alerts"] = compute_alerts(snap)
    return snap


def _matchup(league, team, week):
    out = {"opponent": "BYE / no matchup", "my_projected": 0.0, "opp_projected": 0.0,
           "my_score": 0.0, "opp_score": 0.0, "spread": 0.0, "is_playoff": False}
    box_players = {}
    try:
        for m in league.box_scores(week=week):
            home_id = getattr(m.home_team, "team_id", m.home_team)
            away_id = getattr(m.away_team, "team_id", m.away_team)
            if team.team_id not in (home_id, away_id):
                continue
            mine_home = home_id == team.team_id
            opp_team = m.away_team if mine_home else m.home_team
            lineup = m.home_lineup if mine_home else m.away_lineup
            box_players = {p.playerId: p for p in lineup}
            out.update(
                opponent=getattr(opp_team, "team_name", "Opponent"),
                my_projected=round(float((m.home_projected if mine_home else m.away_projected) or 0), 2),
                opp_projected=round(float((m.away_projected if mine_home else m.home_projected) or 0), 2),
                my_score=round(float((m.home_score if mine_home else m.away_score) or 0), 2),
                opp_score=round(float((m.away_score if mine_home else m.home_score) or 0), 2),
                is_playoff=bool(getattr(m, "is_playoff", False)),
            )
            break
    except Exception as exc:
        out["error"] = f"Box score unavailable: {exc}"
    out["spread"] = round(out["my_projected"] - out["opp_projected"], 2)
    return out, box_players


def _roster(league, team, scoring_period, box_players) -> list:
    data = _raw(league, "mRoster", scoringPeriodId=scoring_period)
    entries = next((t.get("roster", {}).get("entries", [])
                    for t in data.get("teams", []) if t.get("id") == team.team_id), [])
    schedule = _pro_schedule(league, league.current_week)
    roster = []
    for e in entries:
        ppe = e.get("playerPoolEntry", {}) or {}
        pl = ppe.get("player", {}) or {}
        pid = e.get("playerId") or pl.get("id")
        bp = box_players.get(pid)
        pro_id = pl.get("proTeamId", 0)
        opp = getattr(bp, "pro_opponent", None) if bp else None
        if not opp or opp == "None":
            sched = schedule.get(pro_id)
            opp = PRO_TEAM_MAP.get(sched[0], "?") if sched else "BYE"
        on_bye = bool(getattr(bp, "on_bye_week", False)) if bp else (pro_id not in schedule and bool(schedule))
        proj = getattr(bp, "projected_points", None) if bp else None
        if not proj:
            proj = _projection_from_raw(pl, scoring_period)
        raw_status = pl.get("injuryStatus") or e.get("injuryStatus")
        slot_id = e.get("lineupSlotId", BENCH_SLOT)
        roster.append({
            "player_id": pid,
            "name": pl.get("fullName", f"Player {pid}"),
            "pos": _pos_for(pl),
            "pro_team": PRO_TEAM_MAP.get(pro_id, "FA"),
            "opponent": "BYE" if on_bye else opp,
            "on_bye": on_bye,
            "slot_id": slot_id,
            "slot": SLOT_NAMES.get(slot_id, str(slot_id)),
            "eligible_slot_ids": [s for s in pl.get("eligibleSlots", []) if isinstance(s, int)],
            "status_raw": raw_status or "ACTIVE",
            "status": _status_code(raw_status),
            "projected": round(float(proj or 0.0), 2),
            "actual": round(float(getattr(bp, "points", 0.0) or 0.0), 2) if bp else 0.0,
            "locked": bool(ppe.get("lineupLocked", False)),
            "pct_owned": round(float((pl.get("ownership") or {}).get("percentOwned", 0) or 0), 1),
        })
    order = {s: i for i, s in enumerate([0, 2, 4, 6, 23, 3, 5, 7, 16, 17])}
    roster.sort(key=lambda r: (r["slot_id"] in NON_STARTING_SLOTS, r["slot_id"] == IR_SLOT,
                               order.get(r["slot_id"], 50), -r["projected"]))
    return roster


def pending_transactions(league, team_id: int) -> list:
    """mPendingTransactions for my team, with names resolved even for free agents."""
    out = []
    try:
        raw_txs = _raw(league, "mPendingTransactions").get("transactions", []) or []
    except Exception as exc:
        return [{"type": "NOTICE", "status": "ERROR", "process_date": "", "partner": "",
                 "incoming": [], "outgoing": [], "note": f"Could not read pending moves: {exc}"}]

    name_map = {p.playerId: f"{p.name} ({p.position} - {p.proTeam})" for t in league.teams for p in t.roster}
    unknown = {it.get("playerId") for tx in raw_txs for it in tx.get("items", []) or []
               if it.get("playerId") and it.get("playerId") not in name_map}
    if unknown:
        try:
            found = league.player_info(playerId=list(unknown))
            for p in (found if isinstance(found, list) else [found]):
                if p:
                    name_map[p.playerId] = f"{p.name} ({p.position} - {p.proTeam})"
        except Exception:
            pass
    team_names = {t.team_id: t.team_name for t in league.teams}

    for tx in raw_txs:
        items = tx.get("items", []) or []
        involved = tx.get("teamId") == team_id or any(
            it.get("fromTeamId") == team_id or it.get("toTeamId") == team_id for it in items)
        if not involved:
            continue
        exec_ms = tx.get("executionDate") or tx.get("proposedDate")
        incoming, outgoing, partner = [], [], None
        for it in items:
            desc = name_map.get(it.get("playerId"), f"Player {it.get('playerId')}")
            if it.get("toTeamId") == team_id:
                incoming.append(desc)
                partner = partner or it.get("fromTeamId")
            elif it.get("fromTeamId") == team_id:
                outgoing.append(desc)
                partner = partner or it.get("toTeamId")
        partner_name = team_names.get(partner) if partner not in (None, 0, -1) else "Waivers / Free Agency"
        out.append({
            "id": tx.get("id"),
            "type": tx.get("type", "TRANSACTION"),
            "status": tx.get("status", "PENDING"),
            "process_date": datetime.fromtimestamp(exec_ms / 1000).strftime("%a %b %d, %I:%M %p") if exec_ms else "TBD",
            "partner": partner_name or f"Team {partner}",
            "incoming": incoming,
            "outgoing": outgoing,
            "bid": tx.get("bidAmount"),
        })
    return out


def free_agents(league, week: int, per_position: int = 75) -> list:
    """FA + waiver pool per position with real % owned and % change from ESPN."""
    schedule = _pro_schedule(league, week)
    seen, pool = set(), []
    for pos, slot in FA_POSITION_SLOTS.items():
        filters = {"players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "filterSlotIds": {"value": [slot]},
            "limit": per_position,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
        }}
        try:
            data = league.espn_request.league_get(
                params={"view": "kona_player_info", "scoringPeriodId": week},
                headers={"x-fantasy-filter": json.dumps(filters)},
            )
        except Exception:
            continue
        for entry in data.get("players", []) or []:
            pl = entry.get("player", {}) or {}
            pid = pl.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            own = pl.get("ownership", {}) or {}
            pro_id = pl.get("proTeamId", 0)
            sched = schedule.get(pro_id)
            pool.append({
                "player_id": pid,
                "name": pl.get("fullName", ""),
                "pos": pos,
                "pro_team": PRO_TEAM_MAP.get(pro_id, "FA"),
                "opponent": PRO_TEAM_MAP.get(sched[0], "?") if sched else "BYE",
                "projected": _projection_from_raw(pl, week),
                "pct_owned": round(float(own.get("percentOwned", 0) or 0), 1),
                # ESPN field; None when the payload omits it so the UI can say "n/a"
                "pct_change": (round(float(own["percentChange"]), 2)
                               if own.get("percentChange") is not None else None),
                "status": _status_code(pl.get("injuryStatus")),
                "on_waivers": entry.get("status") == "WAIVERS",
            })
    return pool


def compute_alerts(snap: dict) -> list:
    alerts = []
    starters = [p for p in snap["roster"] if p["slot_id"] not in NON_STARTING_SLOTS]
    for p in starters:
        if p["on_bye"]:
            alerts.append({"level": "critical", "text": f"{p['name']} ({p['slot']}) is on BYE but starting."})
        elif p["status"] in UNSTARTABLE:
            alerts.append({"level": "critical", "text": f"{p['name']} ({p['slot']}) is {p['status_raw']} but starting."})
        elif p["status"] in {"Q", "D"}:
            alerts.append({"level": "warning", "text": f"{p['name']} ({p['slot']}) is {p['status_raw']} — have a pivot ready."})
    for p in snap["roster"]:
        if p["slot_id"] == IR_SLOT and p["status"] not in {"IR", "O", "SSPD"}:
            alerts.append({"level": "critical",
                           "text": f"{p['name']} sits in IR but is {p['status_raw']}. ESPN locks adds/drops until moved."})
    filled = {}
    for p in starters:
        filled[p["slot_id"]] = filled.get(p["slot_id"], 0) + 1
    for slot_id, cap in snap.get("slot_counts", {}).items():
        open_n = cap - filled.get(int(slot_id), 0)
        if open_n > 0:
            alerts.append({"level": "critical", "text": f"{open_n} open {SLOT_NAMES.get(int(slot_id), slot_id)} slot(s)."})
    return alerts


def discover_my_teams(settings: Settings, candidates: list[dict]) -> list[dict]:
    """Confirm history candidates against ESPN and return the team this SWID owns in each league."""
    s2, swid = sanitize_cookies(settings.espn_s2, settings.swid)
    if not (s2 and swid):
        return [{"league_id": c["league_id"], "error": "ESPN_S2 / ESPN_SWID not set"} for c in candidates]
    results = []
    this_year = datetime.now().year
    for c in candidates:
        seasons = list(dict.fromkeys([this_year, int(c["season"]) if c.get("season") else this_year]))
        for season in seasons:
            try:
                league = League(league_id=int(c["league_id"]), year=season, espn_s2=s2, swid=swid)
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                continue
            mine = [t for t in league.teams
                    if swid.lower() in {(o.get("id", "") if isinstance(o, dict) else str(o)).lower()
                                        for o in (t.owners or [])}]
            if mine:
                t = mine[0]
                results.append({"league_id": c["league_id"], "season": season,
                                "league_name": getattr(league.settings, "name", ""),
                                "team_id": t.team_id, "team_name": t.team_name,
                                "history_team_id": c.get("team_id")})
            else:
                results.append({"league_id": c["league_id"], "season": season,
                                "error": "Connected, but no team in this league lists your SWID as owner."})
            break
        else:
            results.append({"league_id": c["league_id"], "error": last_err})
    return results
