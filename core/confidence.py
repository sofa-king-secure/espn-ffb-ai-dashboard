"""
Start/sit and acquisition confidence.

For every starter, compares him against every player you could actually get at that
position (your bench, free agents, waivers, other teams' rosters) and returns the
probability each alternative outscores him this week.

Model
  * Each player's points ~ Normal(mean, sd).
  * mean = ESPN projection, cut for injury / practice / bye status.
  * sd = position-specific coefficient of variation x projection. The CV is measured
    from this league's own history: ESPN's projected vs actual points for every player in
    every box score of every completed week (cached per week; past weeks never change),
    shrunk toward a prior when the sample is small. ESPN's player cards only carry past
    *actuals*, not past weekly projections, so box scores are the projection source.
  * P(B outscores A) = Phi((mean_B - mean_A) / sqrt(sd_A^2 + sd_B^2)).

Calibration
  Every live run logs each pool player's (mean, sd) for the week. Entries freeze at
  kickoff so later runs can't rewrite a prediction after the fact. Once a week's games
  are played, every same-position pair is scored against what actually happened, and the
  calibration table shows how often 60/70/80/90% calls came true.

The status multipliers and priors below are starting assumptions, not fitted values;
the calibration table is how you find out whether they hold.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path

from .config import data_dir
from .espn_client import IR_SLOT, NON_STARTING_SLOTS, UNSTARTABLE

POS_IDS = {"QB": 1, "RB": 2, "WR": 3, "TE": 4, "K": 5, "D/ST": 16}
SLOT_POSITIONS = {0: {"QB"}, 2: {"RB"}, 4: {"WR"}, 6: {"TE"}, 16: {"D/ST"}, 17: {"K"},
                  23: {"RB", "WR", "TE"}, 3: {"RB", "WR"}, 5: {"WR", "TE"}, 7: {"QB", "RB", "WR", "TE"}}
PRIOR_CV = {"QB": 0.40, "RB": 0.55, "WR": 0.60, "TE": 0.65, "K": 0.45, "D/ST": 0.75}
PRIOR_WEIGHT = 40          # pseudo-samples: how strongly the prior resists sparse data
SD_FLOOR = 2.5
STATUS_MULT = {"Q": 0.85, "D": 0.35, "DTD": 0.90, "P": 0.97}
PRACTICE_MULT = {"DNP": 0.85, "LIMITED": 0.95, "LP": 0.95}
FA_PER_POSITION = 30       # free agents per position carried into the model (by projection)
KEEP_PER_TIER = 25         # alternatives kept per tier per starter (cut per tier, not overall, so the
                           # ~150 trade targets can't crowd out bench/free agents/waivers)
TIERS = ("Bench", "Free agent", "Waivers", "Trade")
BUCKETS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


# ---------------------------------------------------------------------------------- math
def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_outscores(b: dict, a: dict) -> float:
    """P(player b scores more than player a)."""
    denom = math.sqrt(a["sd"] ** 2 + b["sd"] ** 2) or 1e-9
    return _phi((b["mean"] - a["mean"]) / denom)


# ---------------------------------------------------------------------------------- pool
def obtainable_pool(snap: dict) -> list[dict]:
    """Every player you could field this week, tagged with how you'd get him."""
    pool = []
    for p in snap.get("roster", []):
        if p["slot_id"] == IR_SLOT:
            continue
        tier = "Starter" if p["slot_id"] not in NON_STARTING_SLOTS else "Bench"
        pool.append(dict(p, tier=tier, owner=""))
    by_pos: dict[str, list] = {}
    for f in snap.get("free_agents", []):
        by_pos.setdefault(f["pos"], []).append(f)
    for pos, rows in by_pos.items():
        for f in sorted(rows, key=lambda r: r["projected"], reverse=True)[:FA_PER_POSITION]:
            pool.append(dict(f, tier="Waivers" if f.get("on_waivers") else "Free agent", owner="",
                             on_bye=f.get("opponent") == "BYE", locked=False))
    for p in snap.get("league_rosters", []):
        if p["slot_id"] == IR_SLOT:
            continue
        pool.append(dict(p, tier="Trade", owner=p.get("fantasy_team", "")))
    return pool


# ---------------------------------------------------------------------------------- history
def fetch_history(league, player_ids: list[int], max_period: int) -> dict:
    """{player_id: {"weeks": {sp: {"proj", "act"}}, "avg": season avg}} from ESPN player cards."""
    out = {}
    ids = [i for i in dict.fromkeys(player_ids) if isinstance(i, int)]
    for start in range(0, len(ids), 100):
        try:
            data = league.espn_request.get_player_card(ids[start:start + 100], max_period)
        except Exception:
            continue
        for pl in data.get("players", []) or []:
            player = pl.get("player", pl)
            pid = player.get("id") or pl.get("id")
            weeks, avg = {}, None
            for st in player.get("stats", []) or []:
                if st.get("seasonId") != league.year or st.get("statSplitTypeId") == 2:
                    continue
                sp, src = st.get("scoringPeriodId"), st.get("statSourceId")
                if sp == 0:
                    if src == 0:
                        avg = st.get("appliedAverage")
                    continue
                key = "act" if src == 0 else "proj"
                weeks.setdefault(sp, {})[key] = float(st.get("appliedTotal", 0) or 0)
            out[pid] = {"weeks": weeks, "avg": round(float(avg), 2) if avg is not None else None}
    return out


def box_history(league, meta: dict, current_period: int, weeks: int = 8) -> tuple[dict, list]:
    """{scoring_period: {player_id: {"proj", "act", "pos"}}} for completed weeks, from league box scores.

    Every rostered player in every matchup, so ~150 samples a week in a 10-team league.
    Completed weeks are cached on disk and never refetched.
    """
    hist_dir = data_dir() / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    out, errors = {}, []
    for sp in range(max(1, current_period - weeks), current_period):
        path = hist_dir / f"box-{meta['league_id']}-sp{sp}.json"
        if path.exists():
            out[sp] = {int(k): v for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
            continue
        try:
            boxes = league.box_scores(week=sp)
        except Exception as exc:
            errors.append(f"Week {sp} box scores: {exc}")
            continue
        rows = {}
        for m in boxes:
            for lineup in (getattr(m, "home_lineup", []) or [], getattr(m, "away_lineup", []) or []):
                for bp in lineup:
                    rows[bp.playerId] = {"proj": float(getattr(bp, "projected_points", 0) or 0),
                                         "act": float(getattr(bp, "points", 0) or 0),
                                         "pos": getattr(bp, "position", "")}
        if rows:
            path.write_text(json.dumps(rows), encoding="utf-8")
            out[sp] = rows
        else:
            errors.append(f"Week {sp}: box scores returned no players")
    return out, errors


def fit_spreads(box: dict) -> dict:
    """Position CVs measured from projected-vs-actual box-score history, shrunk toward the prior."""
    sq, proj_sum, n = {}, {}, {}
    for rows in box.values():
        for r in rows.values():
            pos = r.get("pos")
            if pos not in PRIOR_CV or r.get("proj", 0) < 2:
                continue
            sq[pos] = sq.get(pos, 0.0) + (r["act"] - r["proj"]) ** 2
            proj_sum[pos] = proj_sum.get(pos, 0.0) + r["proj"]
            n[pos] = n.get(pos, 0) + 1
    model = {}
    for pos, prior in PRIOR_CV.items():
        k = n.get(pos, 0)
        emp = (math.sqrt(sq[pos] / k) / (proj_sum[pos] / k)) if k else None
        cv = ((k * emp + PRIOR_WEIGHT * prior) / (k + PRIOR_WEIGHT)) if emp is not None else prior
        model[pos] = {"cv": round(cv, 3), "empirical_cv": round(emp, 3) if emp is not None else None,
                      "samples": k, "prior_cv": prior}
    return model


def distribution(p: dict, model: dict, practice: str = "", sleeper_injury: str = "") -> dict:
    proj = float(p.get("projected", 0) or 0)
    cv = model.get(p["pos"], {}).get("cv", PRIOR_CV.get(p["pos"], 0.6))
    status = p.get("status", "")
    out_now = p.get("on_bye") or status in UNSTARTABLE or (sleeper_injury or "").lower() in {"out", "ir", "sus"}
    if out_now:
        return {"mean": 0.0, "sd": 0.5, "note": "bye" if p.get("on_bye") else "out"}
    mult = STATUS_MULT.get(status, 1.0) * PRACTICE_MULT.get((practice or "").upper(), 1.0)
    mean = proj * mult
    return {"mean": round(mean, 2), "sd": round(max(cv * proj, SD_FLOOR), 2),
            "note": "" if mult == 1.0 else f"x{mult:.2f} for {status or ''}{' ' + practice if practice else ''}".strip()}


# ---------------------------------------------------------------------------------- engine
def build_confidence(league, snap: dict) -> dict:
    meta = snap["meta"]
    period = int(meta["scoring_period"])
    pool = obtainable_pool(snap)
    raw_intel = snap.get("intel") or {}
    intel = {r["player_id"]: r for r in raw_intel.get("roster", [])}
    for pid, v in (raw_intel.get("free_agents") or {}).items():
        intel.setdefault(int(pid), v)

    logged_ids = _recent_logged_ids(meta)
    history = fetch_history(league, [p["player_id"] for p in pool] + logged_ids,
                            getattr(league, "finalScoringPeriod", 18))
    card_players = sum(1 for h in history.values() if h["weeks"] or h["avg"] is not None)
    box, box_errors = box_history(league, meta, period)
    model = fit_spreads(box)
    for sp, rows in box.items():  # merge past-week projections into card history (cards have actuals only)
        for pid, r in rows.items():
            w = history.setdefault(pid, {"weeks": {}, "avg": None})["weeks"].setdefault(sp, {})
            w["proj"] = r["proj"]
            w.setdefault("act", r["act"])

    try:
        oprk = league._get_positional_ratings(period)
    except Exception:
        oprk = {}

    for p in pool:
        i = intel.get(p["player_id"], {})
        p.update(distribution(p, model, i.get("practice", ""), i.get("injury", "")))
        p["p80"] = round(p["mean"] + 0.8416 * p["sd"], 1)
        h = history.get(p["player_id"], {"weeks": {}, "avg": None})
        done = sorted((sp, w) for sp, w in h["weeks"].items() if sp < period and "act" in w)[-3:]
        p["season_avg"] = h["avg"]
        p["last3"] = " / ".join(f"{w['act']:.1f}" + (f" ({w['proj']:.1f})" if "proj" in w else "") for _, w in done)
        rank = (oprk.get(str(POS_IDS.get(p["pos"], 0))) or {}).get(str(p.get("opp_id")))
        p["oprk"] = rank
        p["practice"] = i.get("practice", "")
        p["depth"] = i.get("depth", "")

    slots = []
    starters = [p for p in pool if p["tier"] == "Starter"]
    for s in starters:
        allowed = SLOT_POSITIONS.get(s["slot_id"], {s["pos"]})
        alts = []
        for b in pool:
            if b is s or b["pos"] not in allowed or b["tier"] == "Starter":
                continue
            alts.append({"name": b["name"], "player_id": b["player_id"], "tier": b["tier"], "owner": b["owner"],
                         "pos": b["pos"], "pro_team": b["pro_team"], "opponent": b.get("opponent", ""),
                         "kickoff": b.get("kickoff", ""), "status": b.get("status", ""), "practice": b["practice"],
                         "depth": b["depth"], "projected": b["projected"], "mean": b["mean"], "sd": b["sd"],
                         "p80": b["p80"], "season_avg": b["season_avg"], "last3": b["last3"], "oprk": b["oprk"],
                         "pct_owned": b.get("pct_owned"), "pct_change": b.get("pct_change"),
                         "p_outscores": round(p_outscores(b, s), 3)})
        alts.sort(key=lambda r: r["p_outscores"], reverse=True)
        kept = [a for t in TIERS for a in [x for x in alts if x["tier"] == t][:KEEP_PER_TIER]]
        kept.sort(key=lambda r: r["p_outscores"], reverse=True)
        slots.append({"slot": s["slot"], "slot_id": s["slot_id"], "name": s["name"], "player_id": s["player_id"],
                      "pos": s["pos"], "locked": s.get("locked", False), "mean": s["mean"], "sd": s["sd"],
                      "p80": s["p80"], "note": s.get("note", ""), "projected": s["projected"],
                      "season_avg": s["season_avg"], "last3": s["last3"], "oprk": s["oprk"],
                      "opponent": s.get("opponent", ""), "kickoff": s.get("kickoff", ""),
                      "alternatives": kept,
                      "available_by_tier": {t: sum(1 for a in alts if a["tier"] == t) for t in TIERS},
                      "best_by_tier": {t: next((a for a in alts if a["tier"] == t), None) for t in TIERS}})

    log_predictions(meta, pool)
    diagnostics = {"card_players": card_players, "box_weeks": sorted(box),
                   "box_samples": sum(len(r) for r in box.values()), "errors": box_errors}
    return {"built_at": datetime.now().isoformat(timespec="seconds"), "model": model, "slots": slots,
            "pool_size": len(pool), "calibration": calibrate(meta, history), "diagnostics": diagnostics}


# ---------------------------------------------------------------------------------- log + calibration
def _log_dir() -> Path:
    d = data_dir() / "predictions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_path(meta: dict, period: int) -> Path:
    return _log_dir() / f"pred-{meta['league_id']}-{meta['team_id']}-sp{period}.json"


def log_predictions(meta: dict, pool: list[dict]) -> None:
    """Record (mean, sd) for every pool player; entries freeze once that player's game kicks off."""
    path = _log_path(meta, int(meta["scoring_period"]))
    log = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    now_ms = time.time() * 1000
    for p in pool:
        key = str(p["player_id"])
        old = log.get(key)
        if old and old.get("kickoff_ms") and old["kickoff_ms"] <= now_ms:
            continue  # frozen at kickoff
        log[key] = {"name": p["name"], "pos": p["pos"], "mean": p["mean"], "sd": p["sd"],
                    "kickoff_ms": p.get("kickoff_ms"), "logged_at": datetime.now().isoformat(timespec="seconds")}
    path.write_text(json.dumps(log, indent=1), encoding="utf-8")


def _recent_logged_ids(meta: dict, weeks: int = 6) -> list[int]:
    ids = []
    period = int(meta["scoring_period"])
    for sp in range(max(1, period - weeks), period):
        path = _log_path(meta, sp)
        if path.exists():
            ids += [int(k) for k in json.loads(path.read_text(encoding="utf-8")) if k.lstrip("-").isdigit()]
    return ids


def calibrate(meta: dict, history: dict, weeks: int = 6) -> dict:
    """Score every logged same-position pair from completed weeks against what actually happened."""
    period = int(meta["scoring_period"])
    buckets = {b: {"n": 0, "hits": 0, "p_sum": 0.0} for b in BUCKETS}
    brier, pairs, weeks_used = 0.0, 0, []
    for sp in range(max(1, period - weeks), period):
        path = _log_path(meta, sp)
        if not path.exists():
            continue
        log = json.loads(path.read_text(encoding="utf-8"))
        rows = []
        for k, v in log.items():
            act = (history.get(int(k), {}).get("weeks", {}).get(sp) or {}).get("act")
            if act is not None and v["mean"] > 0:
                rows.append((v, act))
        if not rows:
            continue
        weeks_used.append(sp)
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                (a, act_a), (b, act_b) = rows[i], rows[j]
                if a["pos"] != b["pos"] or act_a == act_b:
                    continue
                p = p_outscores(b, a)
                hit = act_b > act_a
                if p < 0.5:
                    p, hit = 1 - p, not hit
                brier += (p - (1.0 if hit else 0.0)) ** 2
                pairs += 1
                for lo, hi in BUCKETS:
                    if lo <= p < hi:
                        bk = buckets[(lo, hi)]
                        bk["n"] += 1
                        bk["hits"] += int(hit)
                        bk["p_sum"] += p
    table = [{"bucket": f"{int(lo * 100)}–{min(int(hi * 100), 100)}%", "pairs": v["n"],
              "avg_predicted": round(v["p_sum"] / v["n"], 3) if v["n"] else None,
              "actual_hit_rate": round(v["hits"] / v["n"], 3) if v["n"] else None}
             for (lo, hi), v in buckets.items()]
    return {"weeks": weeks_used, "pairs": pairs, "brier": round(brier / pairs, 4) if pairs else None, "table": table}
