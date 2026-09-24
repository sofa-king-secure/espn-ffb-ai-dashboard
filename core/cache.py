"""Local JSON snapshot cache + run history (PRD FR-1.2)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import data_dir

KEEP_RUNS = 40


def _runs_dir() -> Path:
    d = data_dir() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_run(snapshot: dict, dossier_md: str, ai_report: str = "", ai_model: str = "") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    m = snapshot["meta"]
    path = _runs_dir() / f"run-{m['league_id']}-{m['team_id']}-{stamp}.json"
    payload = {"snapshot": snapshot, "dossier_md": dossier_md, "ai_report": ai_report, "ai_model": ai_model}
    path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    (data_dir() / "fantasy_status.md").write_text(dossier_md, encoding="utf-8")
    if ai_report:
        (data_dir() / "fantasy_recommendations.md").write_text(ai_report, encoding="utf-8")
    for old in list_runs(m['league_id'], m['team_id'])[KEEP_RUNS:]:
        old.unlink(missing_ok=True)
    return path


def update_latest_report(ai_report: str, ai_model: str, league_id="", team_id="") -> None:
    runs = list_runs(league_id, team_id)
    if not runs:
        return
    data = json.loads(runs[0].read_text(encoding="utf-8"))
    data.update(ai_report=ai_report, ai_model=ai_model)
    runs[0].write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    (data_dir() / "fantasy_recommendations.md").write_text(ai_report, encoding="utf-8")


def list_runs(league_id: str = "", team_id: str = "") -> list[Path]:
    """Runs for one league/team only. With no league/team configured, returns nothing."""
    if not (league_id and team_id):
        return []
    runs = _runs_dir().glob(f"run-{league_id}-{team_id}-*.json")
    return sorted(runs, key=lambda r: r.stem[-15:], reverse=True)


def run_label(path: Path) -> str:
    return datetime.strptime(path.stem[-15:], "%Y%m%d-%H%M%S").strftime("%a %b %d · %I:%M:%S %p")


def clear_all() -> int:
    """Delete every saved run and the latest markdown outputs for this install."""
    n = 0
    for f in list(_runs_dir().glob("run-*.json")) + [data_dir() / "fantasy_status.md",
                                                     data_dir() / "fantasy_recommendations.md"]:
        if f.exists():
            f.unlink()
            n += 1
    return n


def load_run(path: Path | None = None, league_id: str = "", team_id: str = "") -> dict | None:
    runs = list_runs(league_id, team_id)
    target = path or (runs[0] if runs else None)
    if not target or not Path(target).exists():
        return None
    return json.loads(Path(target).read_text(encoding="utf-8"))

