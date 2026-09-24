#!/usr/bin/env python3
"""Headless run for Task Scheduler / launchd / cron. Same services as the dashboard."""
import sys

from core import cache
from core.ai_advisor import Advisor, gameplan_prompt
from core.auth import sync_from_firefox
from core.config import load_settings, data_dir
from core.dossier import build_markdown
from core.espn_client import EspnError, build_snapshot, connect


def main() -> int:
    settings = load_settings()
    print(f"[*] {sync_from_firefox(settings).message}")
    try:
        league, team = connect(settings)
    except EspnError as exc:
        print(f"[-] {exc}")
        return 1
    snap = build_snapshot(league, team, settings)
    dossier = build_markdown(snap)
    report, model = "", ""
    if settings.ai_key_present():
        try:
            adv = Advisor(settings, "floor", ["current_week"])
            report, model = adv.send(gameplan_prompt(dossier)), adv.model_used
        except Exception as exc:
            print(f"[!] AI step failed: {exc}")
    path = cache.save_run(snap, dossier, report, model)
    print(f"[+] Saved {path}\n[+] Markdown in {data_dir()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
