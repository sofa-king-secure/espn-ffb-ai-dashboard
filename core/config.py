"""
Configuration, paths, and .env persistence.

All paths are resolved with pathlib / os.path so the same code runs on
Windows, macOS, and Linux. Nothing here depends on the current working
directory: the .env file always lives next to app.py, regardless of where
`streamlit run` was launched from.
"""
from __future__ import annotations

import hashlib
import os
import platform
import stat
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

APP_NAME = "FFBDashboard"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def data_dir() -> Path:
    """Per-user data directory for snapshots, reports, and run history.

    Windows : %LOCALAPPDATA%\\FFBDashboard\\<install>  (falls back to %APPDATA%)
    macOS   : ~/Library/Application Support/FFBDashboard/<install>
    Linux   : $XDG_DATA_HOME/FFBDashboard/<install> or ~/.local/share/FFBDashboard/<install>
    <install> = folder name + hash of the install path, so every clone starts empty
    and two installs never share history. Override with FFB_DATA_DIR in .env.
    """
    override = _env_value("FFB_DATA_DIR")
    if override:
        base = Path(override).expanduser()
    else:
        system = platform.system()
        if system == "Windows":
            root = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or str(Path.home())
            base = Path(root) / APP_NAME
        elif system == "Darwin":
            base = Path.home() / "Library" / "Application Support" / APP_NAME
        else:
            xdg = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
            base = Path(xdg) / APP_NAME
        install_id = hashlib.sha256(str(PROJECT_ROOT).lower().encode()).hexdigest()[:10]
        base = base / f"{PROJECT_ROOT.name}-{install_id}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _env_value(key: str, default: str = "") -> str:
    """.env file wins over process environment so UI edits apply immediately."""
    file_vals = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    val = file_vals.get(key)
    if val is None:
        val = os.getenv(key, default)
    return (val or default).strip().strip("'\"").strip()


def _as_bool(val: str) -> bool:
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


# Keys the Configuration panel manages, with defaults and whether they are secrets.
ENV_SCHEMA: list[tuple[str, str, bool, str]] = [
    # key, default, secret, help
    ("ESPN_LEAGUE_ID", "", False, "From the league URL: fantasy.espn.com/football/league?leagueId=<id>"),
    ("ESPN_TEAM_ID", "", False, "Your team id inside the league"),
    ("SEASON_YEAR", str(datetime.now().year), False, "Season year"),
    ("ESPN_SWID", "", True, "SWID cookie, curly braces included"),
    ("ESPN_S2", "", True, "espn_s2 cookie (percent-encoded is fine)"),
    ("FIREFOX_AUTO_SYNC", "true", False, "Pull fresh ESPN cookies from Firefox on every live run"),
    ("FIREFOX_PROFILE_PATH", "", False, "Optional: pin a specific Firefox profile folder"),
    ("AI_PROVIDER", "gemini", False, "gemini | anthropic | openai"),
    ("GEMINI_API_KEY", "", True, "Google AI Studio key"),
    ("GEMINI_MODEL", "gemini-3.1-pro-preview", False, "Primary Gemini model"),
    ("GEMINI_FALLBACK_MODEL", "gemini-3-flash-preview", False, "Used if the primary model errors"),
    ("ANTHROPIC_API_KEY", "", True, "Anthropic Console key"),
    ("ANTHROPIC_MODEL", "claude-sonnet-5", False, "Claude model id"),
    ("OPENAI_API_KEY", "", True, "OpenAI key (or any OpenAI-compatible server key)"),
    ("OPENAI_MODEL", "gpt-5.5", False, "Model id on the OpenAI-compatible endpoint"),
    ("OPENAI_BASE_URL", "", False, "Blank = api.openai.com. e.g. http://localhost:11434/v1 for Ollama"),
    ("ENABLE_LINEUP_WRITES", "false", False, "Kill switch. Must be true before the dashboard can POST lineup changes"),
    ("FFB_DATA_DIR", "", False, "Optional override for snapshot/report storage"),
]


@dataclass
class Settings:
    league_id: str = ""
    team_id: str = ""
    season: int = datetime.now().year
    swid: str = ""
    espn_s2: str = ""
    firefox_auto_sync: bool = True
    firefox_profile_path: str = ""
    ai_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-pro-preview"
    gemini_fallback_model: str = "gemini-3-flash-preview"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    openai_api_key: str = ""
    openai_model: str = "gpt-5.5"
    openai_base_url: str = ""
    enable_lineup_writes: bool = False
    missing: list = field(default_factory=list)

    def ai_key_present(self) -> bool:
        return bool({
            "gemini": self.gemini_api_key,
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key or self.openai_base_url,  # local servers may not need a key
        }.get(self.ai_provider, ""))

    def public_dict(self) -> dict:
        """Settings with secrets masked, safe for display/logging."""
        d = asdict(self)
        for k in ("swid", "espn_s2", "gemini_api_key", "anthropic_api_key", "openai_api_key"):
            d[k] = mask(d[k])
        return d


def load_settings() -> Settings:
    try:
        season = int(_env_value("SEASON_YEAR", str(datetime.now().year)))
    except ValueError:
        season = datetime.now().year
    s = Settings(
        league_id=_env_value("ESPN_LEAGUE_ID"),
        team_id=_env_value("ESPN_TEAM_ID"),
        season=season,
        swid=_env_value("ESPN_SWID"),
        espn_s2=_env_value("ESPN_S2"),
        firefox_auto_sync=_as_bool(_env_value("FIREFOX_AUTO_SYNC", "true")),
        firefox_profile_path=_env_value("FIREFOX_PROFILE_PATH"),
        ai_provider=(_env_value("AI_PROVIDER", "gemini") or "gemini").lower(),
        gemini_api_key=_env_value("GEMINI_API_KEY"),
        gemini_model=_env_value("GEMINI_MODEL", "gemini-3.1-pro-preview"),
        gemini_fallback_model=_env_value("GEMINI_FALLBACK_MODEL", "gemini-3-flash-preview"),
        anthropic_api_key=_env_value("ANTHROPIC_API_KEY"),
        anthropic_model=_env_value("ANTHROPIC_MODEL", "claude-sonnet-5"),
        openai_api_key=_env_value("OPENAI_API_KEY"),
        openai_model=_env_value("OPENAI_MODEL", "gpt-5.5"),
        openai_base_url=_env_value("OPENAI_BASE_URL"),
        enable_lineup_writes=_as_bool(_env_value("ENABLE_LINEUP_WRITES", "false")),
    )
    for key, attr in (("ESPN_LEAGUE_ID", "league_id"), ("ESPN_TEAM_ID", "team_id"),
                      ("ESPN_SWID", "swid"), ("ESPN_S2", "espn_s2")):
        if not getattr(s, attr):
            s.missing.append(key)
    return s


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    v = value.strip("{}")
    if len(v) <= keep * 2:
        return "•" * len(v)
    return f"{v[:keep]}…{v[-keep:]} ({len(value)} chars)"


def _quote(val: str) -> str:
    val = str(val).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{val}"'


def update_env_file(updates: dict, env_path: Path = ENV_PATH) -> None:
    """Upsert keys into .env, preserving comments, ordering, and unrelated keys.

    Written atomically (temp file + os.replace) so a crash mid-write can't
    truncate credentials. On POSIX the file is set to 0600.
    """
    env_path = Path(env_path)
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True) if env_path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        body = stripped[len("export "):] if stripped.startswith("export ") else stripped
        key = body.split("=", 1)[0].strip() if "=" in body and not body.startswith("#") else None
        if key and key in remaining:
            out.append(f"{key}={_quote(remaining.pop(key))}\n")
        else:
            out.append(line if line.endswith("\n") else line + "\n")
    for key, val in remaining.items():
        out.append(f"{key}={_quote(val)}\n")

    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(env_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(out)
        os.replace(tmp, env_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if platform.system() != "Windows":
        try:
            os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    # Keep the running process in sync too.
    for k, v in updates.items():
        os.environ[k] = str(v)
