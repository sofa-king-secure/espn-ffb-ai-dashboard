"""
Lineup optimizer, validator, and diff engine.

The optimizer solves slot assignment exactly (Hungarian algorithm) instead of
greedy fill, so FLEX / OP / superflex interactions are handled correctly.
Locked players (game started) and anyone in the IR slot are never moved.
"""
from __future__ import annotations

from .espn_client import SLOT_NAMES, BENCH_SLOT, IR_SLOT, NON_STARTING_SLOTS, UNSTARTABLE

INELIGIBLE = 1e6

RISK_WEIGHTS = {
    # status -> multiplier on projection
    "floor":   {"Q": 0.80, "D": 0.15, "DTD": 0.85, "P": 0.97},
    "ceiling": {"Q": 0.95, "D": 0.35, "DTD": 0.95, "P": 1.00},
}


def normalize_slot_counts(slot_counts: dict) -> dict:
    return {int(k): int(v) for k, v in (slot_counts or {}).items()
            if int(v) > 0 and int(k) not in NON_STARTING_SLOTS}


def effective_score(p: dict, profile: str = "floor") -> float:
    if p.get("on_bye") or p.get("status") in UNSTARTABLE:
        return 0.0
    return round(p.get("projected", 0.0) * RISK_WEIGHTS.get(profile, RISK_WEIGHTS["floor"]).get(p.get("status"), 1.0), 3)


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Min-cost assignment for an n x m matrix with n <= m. Returns column per row."""
    n, m = len(cost), len(cost[0])
    inf = float("inf")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [inf] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    ans = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            ans[p[j] - 1] = j - 1
    return ans


def optimize(roster: list, slot_counts: dict, profile: str = "floor") -> dict:
    """Return {player_id: target_slot_id} for every rostered player."""
    counts = normalize_slot_counts(slot_counts)
    target = {p["player_id"]: p["slot_id"] for p in roster}

    fixed = [p for p in roster if p["locked"] or p["slot_id"] == IR_SLOT]
    movable = [p for p in roster if p not in fixed]

    remaining = dict(counts)
    for p in fixed:
        if p["slot_id"] in remaining:
            remaining[p["slot_id"]] -= 1
    units = [s for s, c in sorted(remaining.items()) for _ in range(max(c, 0))]
    if not units:
        return target

    cols = movable + [None] * max(0, len(units) - len(movable))  # dummy = leave slot empty
    cost = []
    for slot in units:
        row = []
        for p in cols:
            if p is None:
                row.append(0.0)
            elif slot in p["eligible_slot_ids"]:
                row.append(-(effective_score(p, profile) + 0.001))  # tiny bonus: prefer filling
            else:
                row.append(INELIGIBLE)
        cost.append(row)

    for p in movable:
        target[p["player_id"]] = BENCH_SLOT
    for row_idx, col_idx in enumerate(_hungarian(cost)):
        p = cols[col_idx] if col_idx >= 0 else None
        if p is not None and cost[row_idx][col_idx] < INELIGIBLE:
            target[p["player_id"]] = units[row_idx]
    return target


def validate(roster: list, target: dict, slot_counts: dict) -> list[str]:
    counts = normalize_slot_counts(slot_counts)
    by_id = {p["player_id"]: p for p in roster}
    errors, used = [], {}
    for pid, slot in target.items():
        p = by_id.get(pid)
        if p is None:
            errors.append(f"Unknown player id {pid}.")
            continue
        if slot != p["slot_id"]:
            if p["locked"]:
                errors.append(f"{p['name']} is locked (game started) and cannot move.")
            if p["slot_id"] == IR_SLOT or slot == IR_SLOT:
                errors.append(f"{p['name']}: IR moves are not handled here; do them on ESPN.")
            if slot not in p["eligible_slot_ids"] and slot != BENCH_SLOT:
                errors.append(f"{p['name']} is not eligible for {SLOT_NAMES.get(slot, slot)}.")
        if slot not in NON_STARTING_SLOTS:
            used[slot] = used.get(slot, 0) + 1
    for slot, n in used.items():
        if n > counts.get(slot, 0):
            errors.append(f"{SLOT_NAMES.get(slot, slot)} has {n} players; league allows {counts.get(slot, 0)}.")
    return errors


def diff(roster: list, target: dict) -> list[dict]:
    moves = []
    for p in roster:
        new = target.get(p["player_id"], p["slot_id"])
        if new != p["slot_id"]:
            moves.append({"player_id": p["player_id"], "name": p["name"],
                          "from_slot_id": p["slot_id"], "to_slot_id": new,
                          "from": SLOT_NAMES.get(p["slot_id"], p["slot_id"]),
                          "to": SLOT_NAMES.get(new, new),
                          "projected": p["projected"], "status": p["status"]})
    return moves


def projected_total(roster: list, target: dict | None = None) -> float:
    total = 0.0
    for p in roster:
        slot = (target or {}).get(p["player_id"], p["slot_id"])
        if slot not in NON_STARTING_SLOTS:
            total += 0.0 if (p["on_bye"] or p["status"] in UNSTARTABLE) else p["projected"]
    return round(total, 2)
