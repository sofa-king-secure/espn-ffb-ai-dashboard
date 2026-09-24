# Mir's ESPN FFB AI Analyzer

Local Streamlit dashboard for ESPN Fantasy Football: matchup/spread board, lineup vs bench with injury flags,
pending-moves banner, waiver explorer, AI strategy chat (Gemini / Claude / OpenAI-compatible), Word export,
and optional one-click lineup apply to ESPN.

## Layout
```
app.py                 Streamlit UI
launch.py              cross-platform launcher
ffb_cli.py             headless run (Task Scheduler / launchd)
core/config.py         .env load/save, per-OS data dir
core/auth.py           Firefox cookie sync (WAL-aware), cookie sanitizing
core/espn_client.py    ESPN reads: matchup, roster, pending moves, waiver wire
core/lineup.py         exact optimizer, validator, diff
core/espn_writer.py    lineup POST, preflight guards, read-back verification
core/ai_advisor.py     provider-agnostic AI chat + JSON lineup requests
core/dossier.py        markdown dossier
core/report_docx.py    Word export
core/cache.py          run history / cached mode
```

## Install — Windows (PowerShell)
```powershell
git clone https://github.com/sofa-king-secure/espn-ffb-ai-dashboard.git
cd espn-ffb-ai-dashboard
py -3.12 -m venv .venv      # any Python 3.11+ works; `py -0` lists what's installed
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python launch.py
```
If `Activate.ps1` is blocked by execution policy: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## Install — macOS (Terminal)
```bash
git clone https://github.com/sofa-king-secure/espn-ffb-ai-dashboard.git
cd espn-ffb-ai-dashboard
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python launch.py
```

No git? Use **Code → Download ZIP** on GitHub, extract it, and run the same steps from inside the
extracted `espn-ffb-ai-dashboard-main` folder (skip the `git clone` line).

Opens http://localhost:8501 (bound to 127.0.0.1 only). `python -m streamlit run app.py` also works.

Data (run history, `fantasy_status.md`, `fantasy_recommendations.md`) lives in a per-install folder under
`%LOCALAPPDATA%\FFBDashboard\` on Windows and `~/Library/Application Support/FFBDashboard/` on macOS, so a
fresh clone always starts empty, and only runs for the currently configured league/team are shown. The exact path is shown at the bottom of the Configuration tab, which also
has **Clear cached runs**.

## First run
Open the **Configuration** tab. With Firefox logged in to ESPN, **Detect from Firefox** fills in your
league, team, and season; cookies sync automatically. Add an AI key for your chosen provider, save, then
press **Run analysis**.

## Updating
```bash
git pull
python -m pip install -r requirements.txt
```
Your `.env` and run history are untouched: `.env` is git-ignored and history lives outside the repo, keyed to the install folder (moving the folder starts a fresh history).

## Shutting down
Use **Shut down app** at the bottom of the sidebar, or press `Ctrl+C` in the terminal that launched it.
Closing the browser tab alone leaves the local server running.

## Lineup writes
Off by default. To enable: Configuration → check `ENABLE_LINEUP_WRITES` → Save. Then Recommended lineup →
pick a source → review the exact payload → confirm → Apply. Guards: snapshot must be < 20 min old, your SWID
must own the team, locked/IR players are never moved, and every write is read back and verified.
ESPN's write endpoint is unofficial and can change without notice.
