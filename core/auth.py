"""
ESPN session handling: Firefox cookie harvesting and cookie sanitization.

Fixes vs. the original FFB_Monitor.py:
  * Copies cookies.sqlite *and* its -wal/-shm sidecars. Firefox runs SQLite in
    WAL mode, so a freshly rotated espn_s2 often lives only in cookies.sqlite-wal
    until a checkpoint. Copying the main file alone reads stale tokens.
  * Picks the newest profile by max(mtime of db, wal), not the db alone.
  * ORDER BY lastAccessed so the most recently used cookie wins when ESPN has
    set the same name on several hosts (.espn.com vs fantasy.espn.com).
  * Uses a private temp directory (tempfile.mkdtemp) that is always removed.
"""
from __future__ import annotations

import glob
import os
import platform
import shutil
import sqlite3
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, update_env_file, mask


@dataclass
class AuthStatus:
    state: str          # firefox_synced | cookies_active | missing | expired | error
    message: str

    @property
    def label(self) -> str:
        return {
            "firefox_synced": "Firefox synced",
            "cookies_active": "Cookies active",
            "missing": "Setup incomplete",
            "expired": "Session expired",
            "error": "Auth error",
        }.get(self.state, self.state)

    @property
    def ok(self) -> bool:
        return self.state in {"firefox_synced", "cookies_active"}


def firefox_profile_globs() -> list[str]:
    system = platform.system()
    if system == "Windows":
        appdata = os.getenv("APPDATA")
        return [os.path.join(appdata, "Mozilla", "Firefox", "Profiles", "*")] if appdata else []
    if system == "Darwin":
        return [os.path.join(str(Path.home()), "Library", "Application Support", "Firefox", "Profiles", "*")]
    return [os.path.join(str(Path.home()), ".mozilla", "firefox", "*"),
            os.path.join(str(Path.home()), "snap", "firefox", "common", ".mozilla", "firefox", "*")]


def _db_freshness(db: str) -> float:
    times = [os.path.getmtime(db)]
    wal = db + "-wal"
    if os.path.exists(wal):
        times.append(os.path.getmtime(wal))
    return max(times)


def find_cookie_db(profile_override: str = "") -> str | None:
    if profile_override:
        candidate = os.path.join(os.path.expanduser(profile_override), "cookies.sqlite")
        return candidate if os.path.exists(candidate) else None
    dbs = []
    for pattern in firefox_profile_globs():
        for prof in glob.glob(pattern):
            db = os.path.join(prof, "cookies.sqlite")
            if os.path.exists(db):
                dbs.append(db)
    if not dbs:
        return None
    return max(dbs, key=_db_freshness)


def read_espn_cookies(db_path: str) -> dict:
    """Return {'SWID': ..., 'espn_s2': ...} from a Firefox cookie DB (lock-safe copy)."""
    workdir = tempfile.mkdtemp(prefix="ffb_cookies_")
    try:
        local = os.path.join(workdir, "cookies.sqlite")
        shutil.copy2(db_path, local)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                shutil.copy2(db_path + suffix, local + suffix)
        conn = sqlite3.connect(local)
        try:
            rows = conn.execute(
                "SELECT name, value FROM moz_cookies "
                "WHERE host LIKE '%espn.com' AND name IN ('SWID', 'espn_s2') "
                "ORDER BY lastAccessed ASC"
            ).fetchall()
        finally:
            conn.close()
        return dict(rows)  # later (more recent) rows overwrite earlier ones
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def sanitize_cookies(espn_s2: str, swid: str) -> tuple[str, str]:
    """Decode percent-encoding and guarantee SWID keeps its {braces}."""
    clean_s2 = urllib.parse.unquote((espn_s2 or "").strip("'\" "))
    clean_swid = urllib.parse.unquote((swid or "").strip("'\" "))
    if clean_swid and not clean_swid.startswith("{"):
        clean_swid = "{" + clean_swid
    if clean_swid and not clean_swid.endswith("}"):
        clean_swid = clean_swid + "}"
    return clean_s2, clean_swid


def sync_from_firefox(settings: Settings) -> AuthStatus:
    """Refresh ESPN cookies from Firefox into .env when they differ."""
    if not settings.firefox_auto_sync:
        return _static_status(settings)
    try:
        db = find_cookie_db(settings.firefox_profile_path)
        if not db:
            return _static_status(settings, note="No Firefox profile found; using .env cookies.")
        cookies = read_espn_cookies(db)
        s2, swid = cookies.get("espn_s2"), cookies.get("SWID")
        if not (s2 and swid):
            return _static_status(settings, note="Firefox has no ESPN session; log in at espn.com in Firefox.")
        if s2 != settings.espn_s2 or swid != settings.swid:
            update_env_file({"ESPN_S2": s2, "ESPN_SWID": swid})
            settings.espn_s2, settings.swid = s2, swid
            settings.missing = [m for m in settings.missing if m not in ("ESPN_S2", "ESPN_SWID")]
            return AuthStatus("firefox_synced", f"Rotated tokens pulled from Firefox (SWID {mask(swid)}).")
        return AuthStatus("firefox_synced", "Firefox session matches .env.")
    except Exception as exc:  # noqa: BLE001 - surface any sqlite/permission error to the UI
        status = _static_status(settings)
        status.message = f"Firefox sync failed ({exc}); {status.message}"
        return status


def _static_status(settings: Settings, note: str = "") -> AuthStatus:
    if not (settings.espn_s2 and settings.swid):
        return AuthStatus("missing", note or "ESPN_S2 / ESPN_SWID not set.")
    return AuthStatus("cookies_active", note or "Using cookies from .env.")


# ---------------------------------------------------------------- league/team discovery
def _copy_sqlite(db_path: str):
    workdir = tempfile.mkdtemp(prefix="ffb_places_")
    local = os.path.join(workdir, os.path.basename(db_path))
    shutil.copy2(db_path, local)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            shutil.copy2(db_path + suffix, local + suffix)
    return workdir, local


def espn_leagues_from_history(profile_override: str = "", limit: int = 5) -> list[dict]:
    """Distinct football leagues from Firefox history, most recently visited first.

    Reads places.sqlite (same profile as the cookie DB). Returns
    [{"league_id", "team_id" (may be None), "season" (may be None), "last_visit"}].
    """
    cookie_db = find_cookie_db(profile_override)
    if not cookie_db:
        return []
    places = os.path.join(os.path.dirname(cookie_db), "places.sqlite")
    if not os.path.exists(places):
        return []
    workdir, local = _copy_sqlite(places)
    try:
        conn = sqlite3.connect(local)
        try:
            rows = conn.execute(
                "SELECT url, last_visit_date FROM moz_places "
                "WHERE url LIKE '%fantasy.espn.com/football/%' AND url LIKE '%leagueId=%' "
                "ORDER BY last_visit_date DESC LIMIT 1000"
            ).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    found: dict[str, dict] = {}
    for url, visited in rows:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        league = (q.get("leagueId") or [""])[0]
        if not league.isdigit():
            continue
        entry = found.setdefault(league, {"league_id": league, "team_id": None, "season": None,
                                          "last_visit": visited or 0})
        team, season = (q.get("teamId") or [""])[0], (q.get("seasonId") or [""])[0]
        if entry["team_id"] is None and team.isdigit():
            entry["team_id"] = team
        if entry["season"] is None and season.isdigit():
            entry["season"] = season
    return list(found.values())[:limit]
