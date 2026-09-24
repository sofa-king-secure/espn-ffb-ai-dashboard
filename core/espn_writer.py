"""
ESPN lineup writes (start/sit only -- no adds, drops, waivers, or trades).

ESPN has no public write API, and espn-api is read-only. The write surface
below comes from community reverse-engineering, confirmed end-to-end against a
live 2026 league (jwulff/fantasy-sports PR #92, 2026-09-12) and matching the
shape used by ryanjadhav/espn-fantasy:

  POST https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}
       /segments/0/leagues/{league}/transactions/
  body: {"type": "ROSTER", "executionType": "EXECUTE", "teamId": ..,
         "scoringPeriodId": <current>, "items": [{"type": "LINEUP", ...}]}

Behavior this module is built around:
  * One POST with every changed slot is atomic -- all or nothing.
  * Items must only contain changed slots (from == to -> TRAN_ROSTER_SAME_SLOT).
  * Locked players -> 409 TRAN_LINEUP_LOCKED (we pre-check lineupLocked).
  * Only the current scoring period is writable.
  * There is NO server-side dry run, so "preview" is client-side only.
  * The session cookie may be able to write teams you don't own (commissioner),
    so ownership is enforced client-side before anything is sent.
  * Unofficial API: ESPN can change or block it at any time.
"""
from __future__ import annotations

import time

import requests

from .auth import sanitize_cookies
from .config import Settings
from .espn_client import BROWSER_HEADERS, SLOT_NAMES

WRITE_URL = ("https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
             "/segments/0/leagues/{league}/transactions/")
READ_URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
            "/segments/0/leagues/{league}")


def _cookies(settings: Settings) -> dict:
    s2, swid = sanitize_cookies(settings.espn_s2, settings.swid)
    return {"espn_s2": s2, "SWID": swid}


def build_payload(snapshot: dict, moves: list[dict], settings: Settings) -> dict:
    team_id = int(snapshot["meta"]["team_id"])
    _, swid = sanitize_cookies("", settings.swid)
    return {
        "isLeagueManager": False,
        "teamId": team_id,
        "type": "ROSTER",
        "memberId": swid,
        "scoringPeriodId": int(snapshot["meta"]["scoring_period"]),
        "executionType": "EXECUTE",
        "items": [{
            "playerId": m["player_id"],
            "type": "LINEUP",
            "fromLineupSlotId": m["from_slot_id"],
            "toLineupSlotId": m["to_slot_id"],
            "fromTeamId": team_id,
            "toTeamId": team_id,
        } for m in moves],
    }


def preflight(snapshot: dict, moves: list[dict], settings: Settings, max_age_min: int = 20) -> list[str]:
    """Reasons NOT to send. Empty list = clear to submit."""
    problems = []
    if not settings.enable_lineup_writes:
        problems.append("ENABLE_LINEUP_WRITES is false (Configuration tab).")
    if not moves:
        problems.append("No lineup changes to send.")
    if str(snapshot["meta"]["team_id"]) != str(settings.team_id):
        problems.append("Snapshot team does not match ESPN_TEAM_ID.")
    if not snapshot.get("swid_owns_team"):
        problems.append("Your SWID is not listed as an owner of this team. Refusing cross-team write.")
    locked = {p["player_id"] for p in snapshot["roster"] if p["locked"]}
    for m in moves:
        if m["player_id"] in locked:
            problems.append(f"{m['name']} is locked.")
        if m["from_slot_id"] == m["to_slot_id"]:
            problems.append(f"{m['name']}: no-op move would be rejected.")
    try:
        from datetime import datetime
        age = (datetime.now() - datetime.fromisoformat(snapshot["meta"]["fetched_at"])).total_seconds() / 60
        if age > max_age_min:
            problems.append(f"Snapshot is {age:.0f} min old. Run a live refresh first so slot ids and locks are current.")
    except Exception:
        problems.append("Snapshot timestamp unreadable; refresh first.")
    return problems


def _parse_error(resp) -> tuple[str, str]:
    try:
        body = resp.json()
    except ValueError:
        return "HTTP_%d" % resp.status_code, resp.text[:300]
    kinds, msgs = [], []

    def walk(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("type"), str) and obj["type"].isupper():
                kinds.append(obj["type"])
            if isinstance(obj.get("message"), str):
                msgs.append(obj["message"])
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(body)
    return (kinds[0] if kinds else f"HTTP_{resp.status_code}"), ("; ".join(dict.fromkeys(msgs)) or str(body)[:300])


def submit(snapshot: dict, moves: list[dict], settings: Settings) -> dict:
    problems = preflight(snapshot, moves, settings)
    if problems:
        return {"ok": False, "stage": "preflight", "problems": problems}

    payload = build_payload(snapshot, moves, settings)
    url = WRITE_URL.format(season=settings.season, league=settings.league_id)
    headers = dict(BROWSER_HEADERS)
    headers.update({"Content-Type": "application/json",
                    "X-Fantasy-Source": "kona", "X-Fantasy-Platform": "espn-fantasy-web"})
    started = time.time()
    try:
        resp = requests.post(url, json=payload, headers=headers, cookies=_cookies(settings), timeout=30)
    except requests.Timeout:
        # Never blind-retry a write: read back first.
        verdict = verify(snapshot, moves, settings)
        return {"ok": verdict["match"], "stage": "timeout", "payload": payload, "verify": verdict,
                "message": "Request timed out; state was read back instead of retrying."}
    except requests.RequestException as exc:
        return {"ok": False, "stage": "network", "payload": payload, "message": str(exc)}

    result = {"stage": "response", "http": resp.status_code, "payload": payload,
              "elapsed_ms": int((time.time() - started) * 1000)}
    if resp.status_code == 200:
        try:
            result["espn_status"] = resp.json().get("status")
        except ValueError:
            result["espn_status"] = None
        result["verify"] = verify(snapshot, moves, settings)
        result["ok"] = result["verify"]["match"]
        result["message"] = ("Lineup updated and confirmed by read-back." if result["ok"]
                             else "ESPN accepted the request but the read-back differs. Check ESPN directly.")
        return result

    kind, msg = _parse_error(resp)
    result.update(ok=False, error_type=kind, message=_explain(resp.status_code, kind, msg))
    return result


def _explain(code: int, kind: str, msg: str) -> str:
    hints = {
        "TRAN_LINEUP_LOCKED": "A player in this change has already kicked off. Nothing was applied.",
        "TRAN_ROSTER_SAME_SLOT": "A move targeted the slot the player is already in. Nothing was applied.",
        "TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT": "Only the current scoring period can be edited. Refresh and retry.",
        "AUTH_MISSING_CREDENTIALS": "ESPN rejected the cookies on the write host.",
    }
    if code == 401 and kind not in hints:
        return (f"401 from the write host ({msg}). Reads worked with the same cookie, so this is not "
                "proof of expiry; re-sync cookies from Firefox and retry once.")
    return f"{code} {kind}: {hints.get(kind, msg)}"


def verify(snapshot: dict, moves: list[dict], settings: Settings) -> dict:
    """Read the roster back and confirm every requested slot landed."""
    url = READ_URL.format(season=settings.season, league=settings.league_id)
    try:
        r = requests.get(url, params={"view": "mRoster", "scoringPeriodId": snapshot["meta"]["scoring_period"]},
                         headers=BROWSER_HEADERS, cookies=_cookies(settings), timeout=30)
        r.raise_for_status()
        team_id = int(snapshot["meta"]["team_id"])
        entries = next((t.get("roster", {}).get("entries", []) for t in r.json().get("teams", [])
                        if t.get("id") == team_id), [])
        now = {e.get("playerId"): e.get("lineupSlotId") for e in entries}
    except Exception as exc:
        return {"match": False, "error": f"Read-back failed: {exc}", "details": []}
    details = [{"name": m["name"], "wanted": SLOT_NAMES.get(m["to_slot_id"], m["to_slot_id"]),
                "actual": SLOT_NAMES.get(now.get(m["player_id"]), now.get(m["player_id"])),
                "ok": now.get(m["player_id"]) == m["to_slot_id"]} for m in moves]
    return {"match": all(d["ok"] for d in details), "details": details}
