"""
FFB AI Co-Manager — Streamlit dashboard.

Launch:  python launch.py      (or: python -m streamlit run app.py)
"""
from __future__ import annotations

import html
import json
import os
import signal
import threading
from datetime import datetime

import pandas as pd
import streamlit as st

from core import cache
from core.ai_advisor import Advisor, context_primer, gameplan_prompt, recommend_lineup
from core.auth import (AuthStatus, espn_leagues_from_history, find_cookie_db, sanitize_cookies,
                       sync_from_firefox)
from core.config import ENV_PATH, ENV_SCHEMA, data_dir, load_settings, mask, update_env_file
from core.dossier import build_markdown, spread_text
from core.espn_client import (EspnError, IR_SLOT, NON_STARTING_SLOTS, SLOT_NAMES, UNSTARTABLE,
                              build_snapshot, connect, discover_my_teams)
from core import espn_writer, lineup
from core.report_docx import build_docx

APP_TITLE = "Mir's ESPN FFB AI Analyzer"
st.set_page_config(page_title=APP_TITLE, page_icon="🏈", layout="wide")

# ---------------------------------------------------------------- look & feel
# System font stacks only: nothing is fetched from a third-party CDN.
st.markdown("""
<style>
:root { --ink:#1b1b1b; --paper:#f6f3ec; --rule:#d8d1c1; --signal:#c2410c; --good:#2f6b3a; --muted:#6b665c; }
.block-container { padding-top: 1.6rem; max-width: 1400px; }
.num { font-variant-numeric: tabular-nums; font-family: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace; }
.board { border-top: 3px solid var(--ink); border-bottom: 1px solid var(--rule); padding: .9rem 0 1rem; margin-bottom: 1rem;
         display:grid; grid-template-columns: 1fr auto 1fr; align-items:end; gap:1.5rem; }
.board .side { line-height:1.1; }
.board .side.right { text-align:right; }
.board .team { font-size:1.05rem; font-weight:600; color:var(--ink); }
.board .proj { font-size:3rem; font-weight:700; color:var(--ink); }
.board .live { font-size:.85rem; color:var(--muted); }
.board .mid { text-align:center; padding-bottom:.4rem; }
.board .spread { font-size:1.35rem; font-weight:700; }
.board .spread.fav { color: var(--good); } .board .spread.dog { color: var(--signal); }
.board .wk { font-size:.8rem; color:var(--muted); margin-top:.2rem; }
.alert { border-left:4px solid var(--rule); padding:.35rem .7rem; margin:.25rem 0; background:#fbf9f4; font-size:.92rem; }
.alert.critical { border-color: var(--signal); } .alert.warning { border-color:#b08900; }
.move { border:1px solid var(--rule); padding:.6rem .8rem; background:#fbf9f4; height:100%; font-size:.9rem; }
.move b { font-size:.95rem; } .move .in { color:var(--good); } .move .out { color:var(--signal); }
.pill { display:inline-block; padding:.05rem .45rem; border:1px solid var(--rule); font-size:.78rem; margin-left:.3rem; }
</style>
""", unsafe_allow_html=True)

STATUS_COLORS = {"Q": "#fff1c2", "D": "#ffd9b8", "O": "#f7c1b0", "IR": "#f7c1b0", "SSPD": "#f7c1b0",
                 "DTD": "#fff1c2", "P": "#e8f2df"}
PROFILE_LABELS = {"floor": "Floor — conservative", "ceiling": "Ceiling — aggressive"}
FOCUS_OPTIONS = {"current_week": "Waiver adds for this week's need",
                 "stashes": "Long-term handcuffs / stashes",
                 "faab": "Conserve FAAB budget"}


# ---------------------------------------------------------------- state
def _init_state():
    defaults = dict(snapshot=None, dossier="", ai_report="", ai_model="", advisor=None, advisor_sig=None,
                    chat=[], auth=None, candidate=None, candidate_src="", ai_rationale="",
                    write_result=None, loaded_from="")
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)
    if st.session_state.snapshot is None:  # FR-1.2: open instantly on the last cached run
        run = cache.load_run()
        if run:
            _adopt_run(run, "cache")


def _adopt_run(run: dict, source: str):
    st.session_state.snapshot = run["snapshot"]
    st.session_state.dossier = run.get("dossier_md", "")
    st.session_state.ai_report = run.get("ai_report", "")
    st.session_state.ai_model = run.get("ai_model", "")
    st.session_state.candidate = None
    st.session_state.write_result = None
    st.session_state.loaded_from = source


_init_state()
settings = load_settings()


def _advisor(profile: str, focus: list[str]) -> Advisor:
    sig = (settings.ai_provider, profile, tuple(sorted(focus)),
           settings.gemini_model, settings.anthropic_model, settings.openai_model)
    if st.session_state.advisor is None or st.session_state.advisor_sig != sig:
        st.session_state.advisor = Advisor(settings, profile, focus)
        st.session_state.advisor_sig = sig
        st.session_state.chat = []
    return st.session_state.advisor


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("### Controls")
    run_live = st.button("Run analysis", type="primary", width="stretch",
                         help="Firefox cookie sync → ESPN pull → optional AI game plan")
    include_ai = st.toggle("Include AI game plan", value=settings.ai_key_present(),
                           disabled=not settings.ai_key_present(),
                           help="Off = fast ESPN-only refresh. Needs an API key for the selected provider.")

    auth = st.session_state.auth or (AuthStatus("cookies_active", "Using .env cookies.")
                                     if not settings.missing else
                                     AuthStatus("missing", "Not set: " + ", ".join(settings.missing)))
    icon = {"firefox_synced": "🟢", "cookies_active": "🟢", "missing": "⚪", "expired": "🔴", "error": "🟠"}
    st.caption(f"{icon.get(auth.state, '⚪')} **{auth.label}** — {auth.message}")

    runs = cache.list_runs()
    if runs:
        labels = {r: datetime.strptime(r.stem[4:], "%Y%m%d-%H%M%S").strftime("%a %b %d · %I:%M:%S %p") for r in runs}
        pick = st.selectbox("Cached runs", runs, format_func=lambda r: labels[r])
        if st.button("Load cached run", width="stretch"):
            _adopt_run(cache.load_run(pick), "cache")
            st.rerun()

    st.divider()
    profile = st.segmented_control("Risk profile", list(PROFILE_LABELS), default="floor",
                                   format_func=lambda k: PROFILE_LABELS[k]) or "floor"
    focus = [k for k, label in FOCUS_OPTIONS.items() if st.checkbox(label, value=(k == "current_week"))]
    st.caption(f"AI: **{settings.ai_provider}** · writes "
               f"{'**enabled**' if settings.enable_lineup_writes else 'disabled'}")

    st.divider()
    if st.button("Shut down app", width="stretch"):
        st.session_state.confirm_shutdown = True
    if st.session_state.get("confirm_shutdown"):
        st.warning("Stop the local server?")
        y, n = st.columns(2)
        if y.button("Shut down", type="primary"):
            st.success("Server stopped. You can close this tab.")
            # short delay so the message reaches the browser before the process exits
            threading.Timer(0.75, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        if n.button("Cancel"):
            st.session_state.confirm_shutdown = False
            st.rerun()


# ---------------------------------------------------------------- live run
if run_live:
    with st.status("Running analysis…", expanded=True) as status:
        try:
            st.write("Syncing ESPN session from Firefox…")
            st.session_state.auth = sync_from_firefox(settings)
            st.write(f"→ {st.session_state.auth.message}")
            st.write("Connecting to ESPN…")
            league, team = connect(settings)
            st.write("Pulling matchup, roster, pending moves, waiver wire…")
            snap = build_snapshot(league, team, settings)
            dossier = build_markdown(snap)
            cache.save_run(snap, dossier)
            _adopt_run({"snapshot": snap, "dossier_md": dossier}, "live")
            st.session_state.advisor = None
            if include_ai:
                st.write(f"Requesting game plan from {settings.ai_provider}…")
                try:
                    adv = _advisor(profile, focus)
                    st.session_state.ai_report = adv.send(gameplan_prompt(dossier))
                    st.session_state.ai_model = adv.model_used
                    cache.update_latest_report(st.session_state.ai_report, adv.model_used)
                except Exception as exc:
                    st.warning(f"ESPN data saved; AI step failed: {exc}")
            status.update(label="Analysis complete", state="complete", expanded=False)
        except EspnError as exc:
            if exc.kind == "auth":
                st.session_state.auth = AuthStatus("expired", "ESPN rejected the cookies. Log in to ESPN in Firefox, then re-run.")
            status.update(label=f"ESPN error: {exc}", state="error")
        except Exception as exc:  # AI or network failure: keep ESPN data if we got it
            status.update(label=f"Run failed: {exc}", state="error")


snap = st.session_state.snapshot

# ---------------------------------------------------------------- header
st.title(APP_TITLE)
if snap:
    m, mu = snap["meta"], snap["matchup"]
    esc = html.escape
    cls = "fav" if mu["spread"] > 0 else ("dog" if mu["spread"] < 0 else "")
    st.markdown(f"""
<div class="board">
  <div class="side"><div class="team">{esc(m['team_name'])} <span class="pill">{m['record']}</span></div>
    <div class="proj num">{mu['my_projected']:.1f}</div><div class="live num">live {mu['my_score']:.1f}</div></div>
  <div class="mid"><div class="spread {cls} num">{spread_text(mu['spread'])}</div>
    <div class="wk">Week {m['week']} · {'playoffs' if mu.get('is_playoff') else 'regular season'} · data {m['fetched_at'].replace('T', ' ')}
    ({st.session_state.loaded_from})</div></div>
  <div class="side right"><div class="team">{esc(mu['opponent'])}</div>
    <div class="proj num">{mu['opp_projected']:.1f}</div><div class="live num">live {mu['opp_score']:.1f}</div></div>
</div>""", unsafe_allow_html=True)

    if snap["pending"]:
        cols = st.columns(min(len(snap["pending"]), 3))
        for i, pm in enumerate(snap["pending"]):
            with cols[i % len(cols)]:
                ins = "<br>".join(f"<span class='in'>+ {esc(x)}</span>" for x in pm["incoming"])
                outs = "<br>".join(f"<span class='out'>− {esc(x)}</span>" for x in pm["outgoing"])
                bid = f" · ${pm['bid']}" if pm.get("bid") else ""
                st.markdown(f"<div class='move'><b>{pm['type'].title()}</b> · {pm['status'].title()}{bid}<br>"
                            f"Processes <span class='num'>{pm['process_date']}</span> · {esc(pm['partner'])}<br>{ins}"
                            f"{'<br>' if ins and outs else ''}{outs}</div>", unsafe_allow_html=True)
    for a in snap["alerts"]:
        st.markdown(f"<div class='alert {a['level']}'>{esc(a['text'])}</div>", unsafe_allow_html=True)
else:
    st.info("No data yet. Fill in the **Configuration** tab, then press **Run analysis**.")

tabs = st.tabs(["Lineup", "Recommended lineup", "Waiver wire", "AI strategy", "Export", "Configuration"])


def _styled(df: pd.DataFrame):
    def color(v):
        return f"background-color: {STATUS_COLORS[v]}" if v in STATUS_COLORS else ""
    return df.style.map(color, subset=["Status"]).format({"Proj": "{:.1f}", "Pts": "{:.1f}"})


def _roster_df(players):
    return pd.DataFrame([{
        "Slot": p["slot"], "Player": p["name"] + (" 🔒" if p["locked"] else ""), "Pos": p["pos"],
        "Team": p["pro_team"], "Opp": p["opponent"], "Status": p["status"] or "", "Proj": p["projected"],
        "Pts": p["actual"]} for p in players])


def _slot_units(slot_counts: dict) -> list[int]:
    order = [0, 2, 4, 6, 3, 5, 23, 7, 16, 17]
    counts = lineup.normalize_slot_counts(slot_counts)
    units = []
    for s in order + [s for s in counts if s not in order]:
        units += [s] * counts.get(s, 0)
    return units


# ---------------------------------------------------------------- Lineup tab
with tabs[0]:
    if not snap:
        st.caption("Run an analysis first.")
    else:
        starters = [p for p in snap["roster"] if p["slot_id"] not in NON_STARTING_SLOTS]
        bench = [p for p in snap["roster"] if p["slot_id"] == 20]
        ir = [p for p in snap["roster"] if p["slot_id"] == IR_SLOT]
        c1, c2 = st.columns([1.25, 1])
        with c1:
            st.subheader("Starting lineup")
            st.dataframe(_styled(_roster_df(starters)), hide_index=True, width="stretch")
            st.caption(f"Roster projection (healthy starters): **{lineup.projected_total(snap['roster'])}** · "
                       f"ESPN matchup projection: **{snap['matchup']['my_projected']}**")
        with c2:
            st.subheader("Bench")
            st.dataframe(_styled(_roster_df(bench)), hide_index=True, width="stretch")
            if ir:
                st.subheader("IR")
                st.dataframe(_styled(_roster_df(ir)), hide_index=True, width="stretch")

        with st.expander("Scenario tester — swap players and see the projected total", expanded=False):
            roster = snap["roster"]
            movable = [p for p in roster if not p["locked"] and p["slot_id"] != IR_SLOT]
            by_id = {p["player_id"]: p for p in roster}
            current_by_slot: dict[int, list] = {}
            for p in roster:
                current_by_slot.setdefault(p["slot_id"], []).append(p["player_id"])
            target = {p["player_id"]: p["slot_id"] for p in roster}
            for p in movable:
                target[p["player_id"]] = 20
            chosen, cols = [], st.columns(3)
            for i, slot in enumerate(_slot_units(snap["slot_counts"])):
                locked_pid = next((pid for pid in current_by_slot.get(slot, [])
                                   if by_id[pid]["locked"] and pid not in chosen), None)
                if locked_pid is not None:
                    chosen.append(locked_pid)
                    cols[i % 3].text_input(SLOT_NAMES.get(slot, slot), by_id[locked_pid]["name"] + " 🔒",
                                           disabled=True, key=f"lock_{i}")
                    continue
                options = [None] + [p["player_id"] for p in movable if slot in p["eligible_slot_ids"]]
                default_pid = next((pid for pid in current_by_slot.get(slot, [])
                                    if pid in options and pid not in chosen), None)
                pick = cols[i % 3].selectbox(
                    SLOT_NAMES.get(slot, slot), options, index=options.index(default_pid),
                    format_func=lambda pid: "— empty —" if pid is None else
                    f"{by_id[pid]['name']} ({by_id[pid]['status'] or 'OK'}, {by_id[pid]['projected']})",
                    key=f"scen_{i}_{slot}")
                if pick is not None:
                    chosen.append(pick)
                    target[pick] = slot
            dupes = {by_id[p]["name"] for p in chosen if chosen.count(p) > 1}
            base, sim = lineup.projected_total(roster), lineup.projected_total(roster, target)
            st.metric("Simulated projection", f"{sim:.1f}", f"{sim - base:+.1f} vs current")
            if dupes:
                st.error("Same player in two slots: " + ", ".join(sorted(dupes)))
            elif st.button("Send this scenario to the Apply panel"):
                st.session_state.candidate, st.session_state.candidate_src = target, "Scenario tester"
                st.success("Loaded into the Recommended lineup tab.")


# ---------------------------------------------------------------- Recommended lineup tab
with tabs[1]:
    if not snap:
        st.caption("Run an analysis first.")
    else:
        roster = snap["roster"]
        src = st.radio("Recommendation source", ["Optimizer (projections)", "AI recommendation", "Scenario tester"],
                       horizontal=True)
        rationale = ""
        if src == "Optimizer (projections)":
            target = lineup.optimize(roster, snap["slot_counts"], profile)
            st.caption("Exact assignment over ESPN projections. Q/D players are discounted by risk profile; "
                       "O/IR/bye players score zero. Locked and IR players are never moved.")
        elif src == "AI recommendation":
            if not settings.ai_key_present():
                st.warning("Set an AI key in Configuration first.")
                target = None
            else:
                if st.button("Ask AI for a lineup"):
                    try:
                        with st.spinner("Asking the model…"):
                            starters, st.session_state.ai_rationale = recommend_lineup(
                                _advisor(profile, focus), snap, profile)
                        t = {p["player_id"]: (p["slot_id"] if p["locked"] or p["slot_id"] == IR_SLOT else 20)
                             for p in roster}
                        t.update({pid: slot for pid, slot in starters.items() if pid in t})
                        st.session_state.candidate, st.session_state.candidate_src = t, "AI recommendation"
                    except Exception as exc:
                        st.error(f"AI lineup failed: {exc}")
                target = st.session_state.candidate if st.session_state.candidate_src == "AI recommendation" else None
                rationale = st.session_state.ai_rationale if target else ""
        else:
            target = st.session_state.candidate if st.session_state.candidate_src == "Scenario tester" else None
            if target is None:
                st.caption("Build one in Lineup → Scenario tester.")

        if target:
            errors = lineup.validate(roster, target, snap["slot_counts"])
            moves = lineup.diff(roster, target)
            st.session_state.export_moves = moves if not errors else None
            base, new = lineup.projected_total(roster), lineup.projected_total(roster, target)
            c1, c2, c3 = st.columns(3)
            c1.metric("Current projection", f"{base:.1f}")
            c2.metric("Recommended projection", f"{new:.1f}", f"{new - base:+.1f}")
            c3.metric("Slot changes", len(moves))
            if rationale:
                st.info(rationale)

            by_id = {p["player_id"]: p for p in roster}
            rows = []
            for slot in _slot_units(snap["slot_counts"]):
                rows.append({"Slot": SLOT_NAMES.get(slot, slot), "slot_id": slot})
            cur_fill, rec_fill = {}, {}
            for p in roster:
                cur_fill.setdefault(p["slot_id"], []).append(p)
                rec_fill.setdefault(target.get(p["player_id"], p["slot_id"]), []).append(p)
            for r in rows:
                cur = cur_fill.get(r["slot_id"], [])
                rec = rec_fill.get(r["slot_id"], [])
                cp = cur.pop(0) if cur else None
                rp = rec.pop(0) if rec else None
                r["Current"] = f"{cp['name']} ({cp['projected']})" if cp else "—"
                r["Recommended"] = f"{rp['name']} ({rp['projected']})" if rp else "—"
                r["Change"] = "" if (cp and rp and cp["player_id"] == rp["player_id"]) else "●"
            st.dataframe(pd.DataFrame(rows).drop(columns="slot_id"), hide_index=True, width="stretch")

            if errors:
                st.error("This lineup is not legal:\n\n- " + "\n- ".join(errors))
            elif not moves:
                st.success("Your current lineup already matches this recommendation.")
            else:
                st.markdown("#### Apply to ESPN")
                problems = espn_writer.preflight(snap, moves, settings)
                with st.expander("Exact request that will be sent"):
                    payload = espn_writer.build_payload(snap, moves, settings)
                    payload_view = dict(payload, memberId=mask(payload["memberId"]))
                    st.json(payload_view)
                    st.caption("ESPN has no server-side dry run; this preview is the dry run. "
                               "The change is atomic: all moves land or none do.")
                if problems:
                    st.warning("Can't apply yet:\n\n- " + "\n- ".join(problems))
                confirm = st.checkbox("I understand this changes my live ESPN lineup via ESPN's unofficial API.")
                if st.button("Apply lineup to ESPN", type="primary", disabled=bool(problems) or not confirm):
                    with st.spinner("Submitting and reading back…"):
                        st.session_state.write_result = espn_writer.submit(snap, moves, settings)
                res = st.session_state.write_result
                if res:
                    (st.success if res.get("ok") else st.error)(res.get("message") or "; ".join(res.get("problems", [])))
                    if res.get("verify", {}).get("details"):
                        st.dataframe(pd.DataFrame(res["verify"]["details"]), hide_index=True)
                    if res.get("ok"):
                        st.caption("Press **Run analysis** to refresh the dashboard from ESPN.")


# ---------------------------------------------------------------- Waiver tab
with tabs[2]:
    if not snap:
        st.caption("Run an analysis first.")
    else:
        fa = pd.DataFrame(snap["free_agents"])
        c1, c2, c3 = st.columns([2.2, 2, 1.2])
        pos = c1.pills("Position", ["ALL", "QB", "RB", "WR", "TE", "D/ST", "K"], default="ALL") or "ALL"
        mode = c2.segmented_control("View", ["High-add velocity", "Sub-35% sleepers", "Projection"],
                                    default="High-add velocity") or "High-add velocity"
        hide_out = c3.toggle("Hide O/IR", value=True)
        if fa.empty:
            st.caption("No free agents returned.")
        else:
            view = fa if pos == "ALL" else fa[fa["pos"] == pos]
            if hide_out:
                view = view[~view["status"].isin(list(UNSTARTABLE))]
            if fa["pct_change"].isna().all():
                st.warning("ESPN returned no % change values in this pull, so velocity sort falls back to projection.")
            chg = view["pct_change"].fillna(0)
            if mode == "High-add velocity":
                view = view.assign(_k=chg).sort_values(["_k", "projected"], ascending=False)
            elif mode == "Sub-35% sleepers":
                view = view[view["pct_owned"] < 35].assign(_k=chg).sort_values(["_k", "projected"], ascending=False)
                st.caption("Under 35% rostered, ranked by ownership momentum then projection. ESPN's free-agent "
                           "feed doesn't include targets or snap share, so ask the AI tab about role/handcuff value.")
            else:
                view = view.sort_values("projected", ascending=False)
            show = view.head(40).rename(columns={"name": "Player", "pos": "Pos", "pro_team": "Team", "opponent": "Opp",
                                                 "projected": "Proj", "pct_owned": "% Rost", "pct_change": "% Chg",
                                                 "status": "Status", "on_waivers": "On waivers"})
            st.dataframe(show[["Player", "Pos", "Team", "Opp", "Proj", "% Rost", "% Chg", "Status", "On waivers"]],
                         hide_index=True, width="stretch",
                         column_config={
                             "% Rost": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100),
                             "% Chg": st.column_config.NumberColumn(format="%+.2f%%"),
                             "Proj": st.column_config.NumberColumn(format="%.1f")})


# ---------------------------------------------------------------- AI strategy tab
with tabs[3]:
    if not snap:
        st.caption("Run an analysis first.")
    elif not settings.ai_key_present():
        st.warning(f"No key configured for provider '{settings.ai_provider}'. Add one in Configuration.")
        if st.session_state.ai_report:
            st.markdown(st.session_state.ai_report)
    else:
        top = st.columns([1, 1, 3])
        if top[0].button("Generate game plan", type="primary"):
            try:
                with st.spinner(f"Asking {settings.ai_provider}…"):
                    adv = _advisor(profile, focus)
                    adv.history, adv._gemini_chat = [], None
                    st.session_state.chat = []
                    st.session_state.ai_report = adv.send(gameplan_prompt(st.session_state.dossier))
                    st.session_state.ai_model = adv.model_used
                    cache.update_latest_report(st.session_state.ai_report, adv.model_used)
            except Exception as exc:
                st.error(f"AI request failed: {exc}")
        if top[1].button("Reset chat"):
            st.session_state.advisor = None
            st.session_state.chat = []
        top[2].caption(f"Profile: **{PROFILE_LABELS[profile]}** · Focus: "
                       f"{', '.join(FOCUS_OPTIONS[f] for f in focus) or 'none'}")

        if st.session_state.ai_report:
            with st.container(border=True):
                st.caption(f"Game plan · {st.session_state.ai_model or settings.ai_provider}")
                st.markdown(st.session_state.ai_report)

        st.markdown("#### Ask a follow-up")
        for msg in st.session_state.chat:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
        question = st.chat_input("e.g. Who's the pivot if my questionable WR sits?")
        if question:
            st.session_state.chat.append({"role": "user", "content": question})
            try:
                adv = _advisor(profile, focus)
                if not adv.history:  # seed context once per advisor
                    adv.send(context_primer(st.session_state.dossier, st.session_state.ai_report))
                with st.spinner("Thinking…"):
                    answer = adv.send(question)
            except Exception as exc:
                answer = f"AI request failed: {exc}"
            st.session_state.chat.append({"role": "assistant", "content": answer})
            st.rerun()


# ---------------------------------------------------------------- Export tab
with tabs[4]:
    if not snap:
        st.caption("Run an analysis first.")
    else:
        stamp = f"wk{snap['meta']['week']}_{datetime.now():%Y%m%d_%H%M}"
        rec_moves = st.session_state.get("export_moves") or None
        c1, c2, c3, c4 = st.columns(4)
        c1.download_button("Word report (.docx)",
                           build_docx(snap, st.session_state.ai_report, st.session_state.ai_model, rec_moves),
                           file_name=f"ffb_gameplan_{stamp}.docx", type="primary", width="stretch",
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        c2.download_button("Dossier (.md)", st.session_state.dossier, file_name=f"fantasy_status_{stamp}.md",
                           width="stretch")
        c3.download_button("AI report (.md)", st.session_state.ai_report or "No AI report in this run.",
                           file_name=f"fantasy_recommendations_{stamp}.md", width="stretch",
                           disabled=not st.session_state.ai_report)
        c4.download_button("Raw snapshot (.json)", json.dumps(snap, indent=1, default=str),
                           file_name=f"snapshot_{stamp}.json", width="stretch")
        st.caption(f"Every run is also saved to `{data_dir()}` (last {cache.KEEP_RUNS} kept).")
        with st.expander("Dossier preview"):
            st.markdown(st.session_state.dossier)


# ---------------------------------------------------------------- Configuration tab
with tabs[5]:
    st.caption(f"Settings are stored in `{ENV_PATH}`. Secret fields are never displayed; leave them blank to keep the "
               "current value.")
    with st.container(border=True):
        st.markdown("**Find my league & team**")
        st.caption("Reads fantasy.espn.com URLs from Firefox history, then confirms with ESPN which team your "
                   "SWID owns. Nothing is saved until you click Use.")
        if st.button("Detect from Firefox"):
            with st.spinner("Reading Firefox history and checking with ESPN…"):
                sync_from_firefox(settings)
                cands = espn_leagues_from_history(settings.firefox_profile_path)
                st.session_state.discovered = discover_my_teams(settings, cands) if cands else []
            if not cands:
                st.warning("No fantasy.espn.com league URLs in Firefox history. Open your league once in Firefox, "
                           "then retry.")
        found = [d for d in st.session_state.get("discovered", []) if "team_id" in d]
        for d in st.session_state.get("discovered", []):
            if "error" in d:
                st.caption(f"League {d['league_id']}: {d['error']}")
        if found:
            pick = st.selectbox("Leagues where you own a team", found,
                                format_func=lambda d: f"{d['league_name'] or 'League'} ({d['league_id']}) · "
                                                      f"{d['team_name']} (team {d['team_id']}) · {d['season']}")
            if st.button("Use this league & team", type="primary"):
                update_env_file({"ESPN_LEAGUE_ID": str(pick["league_id"]), "ESPN_TEAM_ID": str(pick["team_id"]),
                                 "SEASON_YEAR": str(pick["season"])})
                st.session_state.discovered = []
                st.success("Saved to .env.")
                st.rerun()

    with st.form("env_form"):
        updates = {}
        groups = {"ESPN": ENV_SCHEMA[:7], "AI providers": ENV_SCHEMA[7:16], "Safety & storage": ENV_SCHEMA[16:]}
        for gname, fields in groups.items():
            st.markdown(f"**{gname}**")
            cols = st.columns(2)
            for i, (key, default, secret, help_text) in enumerate(fields):
                col = cols[i % 2]
                live = {"ESPN_LEAGUE_ID": settings.league_id, "ESPN_TEAM_ID": settings.team_id,
                        "SEASON_YEAR": str(settings.season), "FIREFOX_PROFILE_PATH": settings.firefox_profile_path,
                        "GEMINI_MODEL": settings.gemini_model, "GEMINI_FALLBACK_MODEL": settings.gemini_fallback_model,
                        "ANTHROPIC_MODEL": settings.anthropic_model, "OPENAI_MODEL": settings.openai_model,
                        "OPENAI_BASE_URL": settings.openai_base_url}.get(key, default)
                if key == "AI_PROVIDER":
                    opts = ["gemini", "anthropic", "openai"]
                    val = col.selectbox(key, opts, index=opts.index(settings.ai_provider)
                                        if settings.ai_provider in opts else 0, help=help_text)
                elif key in ("FIREFOX_AUTO_SYNC", "ENABLE_LINEUP_WRITES"):
                    cur = settings.firefox_auto_sync if key == "FIREFOX_AUTO_SYNC" else settings.enable_lineup_writes
                    val = "true" if col.checkbox(key, value=cur, help=help_text) else "false"
                elif secret:
                    present = {"ESPN_SWID": settings.swid, "ESPN_S2": settings.espn_s2,
                               "GEMINI_API_KEY": settings.gemini_api_key, "ANTHROPIC_API_KEY": settings.anthropic_api_key,
                               "OPENAI_API_KEY": settings.openai_api_key}[key]
                    val = col.text_input(key, type="password", help=help_text,
                                         placeholder=f"set · {mask(present)}" if present else "not set")
                    if not val:
                        continue
                else:
                    val = col.text_input(key, value=live, help=help_text)
                updates[key] = val
        if st.form_submit_button("Save to .env", type="primary"):
            update_env_file(updates)
            st.session_state.advisor = None
            st.success("Saved.")
            st.rerun()

    st.markdown("**Checks**")
    b1, b2, b3 = st.columns(3)
    if b1.button("Sync cookies from Firefox now", width="stretch"):
        st.session_state.auth = sync_from_firefox(settings)
        st.info(st.session_state.auth.message)
    if b2.button("Test ESPN connection", width="stretch"):
        try:
            lg, tm = connect(load_settings())
            owners = [o.get("id", "") if isinstance(o, dict) else str(o) for o in tm.owners or []]
            owns = sanitize_cookies("", settings.swid)[1].lower() in {o.lower() for o in owners}
            st.success(f"Connected: {tm.team_name} ({tm.wins}-{tm.losses}), week {lg.current_week}. "
                       f"SWID owns this team: {'yes' if owns else 'NO — writes will be refused'}.")
        except Exception as exc:
            st.error(str(exc))
    if b3.button("Test AI provider", width="stretch"):
        try:
            adv = Advisor(load_settings(), "floor", [])
            reply = adv.send("Reply with exactly: OK")
            st.success(f"{adv.model_used}: {reply.strip()[:80]}")
        except Exception as exc:
            st.error(str(exc))
    st.caption(f"Firefox cookie DB: `{find_cookie_db(settings.firefox_profile_path) or 'not found'}` · "
               f"Data dir: `{data_dir()}`")
