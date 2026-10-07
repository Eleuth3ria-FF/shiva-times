#!/usr/bin/env python3
"""
Sleeper league sync for The Shiva Times.

Walks the league's season chain (via previous_league_id), pulls
standings, head-to-head results, and trade history, and writes JSON
files into /data for the static site to read.

Runs on Sleeper's public, unauthenticated API — no API key needed.
Docs: https://docs.sleeper.com/
"""

import csv
import io
import json
import os
import random
import re
import statistics
import time
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None  # fallback handled where used — rare, but don't crash the whole sync over it

CURRENT_LEAGUE_ID = "1312396775717371904"
API_BASE = "https://api.sleeper.app/v1"
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# Known division assignments (Sleeper doesn't expose "divisions" reliably
# via API for this league's config, so this is maintained by hand).
# Backlog: automate this once/if the league configures divisions in Sleeper settings.
DIVISIONS = {
    "North": ["Scuderia Malo", "Mean Machine", "La Quiche", "Tebows before Hoes"],
    "Central": ["Montreal Weapon X", "T-Bone Ray's", "The Gridiron Sheriffs", "MAGRAUDERS"],
    "South": ["SGB Nation ⚖️", "Les Pingouins 🐧", "Lavaltree's Piggy", "MTL New Empire"],
}

# BACKLOG: at least one franchise has changed real-world owners across
# seasons (e.g. Julien Valiquette's team appears to have transferred to a
# current manager). This script currently attributes historical results
# to whoever owned the roster_id in THAT season. Until we confirm the
# transfer mapping, old-owner results will show under the old owner's name
# rather than being merged into the current owner's all-time record.


def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "shiva-times-sync/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return None


def get_season_chain(start_league_id):
    """Walk previous_league_id backwards to find every season on record."""
    chain = []
    league_id = start_league_id
    seen = set()
    while league_id and league_id != "0" and league_id not in seen:
        seen.add(league_id)
        league = fetch_json(f"{API_BASE}/league/{league_id}")
        if not league:
            break
        chain.append(league)
        league_id = league.get("previous_league_id")
    return chain  # newest first


def build_owner_map(league_id):
    """roster_id -> {owner_id, name, manager_name} for one season.

    owner_id is Sleeper's stable per-manager account ID — use THIS for
    aggregating across seasons. name is whatever that manager's TEAM was
    called THAT season (typos, emoji, rebrands happen) — display-only,
    never a merge key. manager_name is the Sleeper account's own display
    name (the actual person), kept separate so the site can show "Team X
    — run by Person Y" instead of guessing who owns what.
    """
    users = fetch_json(f"{API_BASE}/league/{league_id}/users") or []
    rosters = fetch_json(f"{API_BASE}/league/{league_id}/rosters") or []
    user_by_id = {u["user_id"]: u for u in users}
    owner_map = {}
    for r in rosters:
        owner_id = r.get("owner_id")
        u = user_by_id.get(owner_id, {})
        team_name = (u.get("metadata") or {}).get("team_name") or u.get("display_name") or f"Roster {r['roster_id']}"
        manager_name = u.get("display_name") or team_name
        owner_map[r["roster_id"]] = {"owner_id": owner_id, "name": team_name, "manager_name": manager_name}
    return owner_map, rosters


def sync_standings(current_league_id):
    """Current season standings, grouped into the known divisions."""
    owner_map, rosters = build_owner_map(current_league_id)
    league_info = fetch_json(f"{API_BASE}/league/{current_league_id}") or {}
    season_label = league_info.get("season", str(current_league_id))

    def normalize(s):
        return "".join(ch for ch in s.lower().strip() if ch.isalnum())

    by_name = {}
    by_normalized = {}
    for r in rosters:
        info = owner_map.get(r["roster_id"], {"owner_id": None, "name": "Unknown", "manager_name": "Unknown"})
        name = info["name"]
        s = r.get("settings", {})
        entry = {
            "team": name,
            "owner": info.get("manager_name", name),
            "wins": s.get("wins", 0),
            "losses": s.get("losses", 0),
            "ties": s.get("ties", 0),
            "pointsFor": round((s.get("fpts", 0) or 0) + (s.get("fpts_decimal", 0) or 0) / 100, 1),
            "pointsAgainst": round((s.get("fpts_against", 0) or 0) + (s.get("fpts_against_decimal", 0) or 0) / 100, 1),
        }
        by_name[name] = entry
        by_normalized[normalize(name)] = entry

    divisions = []
    placed = set()
    for div_name, team_names in DIVISIONS.items():
        teams = []
        for t in team_names:
            # Exact match first, then a normalized (lowercase/no-punctuation)
            # fallback — Sleeper team names drift season to season (emoji,
            # capitalization, stray spaces), so this catches most of that
            # without needing the hardcoded DIVISIONS list updated every year.
            match = by_name.get(t) or by_normalized.get(normalize(t))
            if match:
                teams.append(match)
                placed.add(t)
            else:
                # Still not found — show as unknown rather than silently
                # dropping it, so it's visible something needs mapping.
                teams.append({"team": t, "owner": None, "wins": 0, "losses": 0, "ties": 0, "pointsFor": None, "pointsAgainst": None})
        divisions.append({"name": div_name, "teams": teams})

    return {"season": season_label, "divisions": divisions}


def build_display_names(season_chain):
    """owner_id -> the most recent team name that owner has used.

    season_chain is newest-first, so the first name seen per owner_id is
    their current one — that's what every part of the site should show,
    regardless of which season a given stat came from.
    """
    display_name = {}
    for league in season_chain:
        owner_map, _ = build_owner_map(league["league_id"])
        for info in owner_map.values():
            display_name.setdefault(info["owner_id"], info["name"])
    return display_name


def sync_head_to_head(season_chain, display_name):
    """All-time head-to-head record between every pair, across every season.

    Aggregated by Sleeper's stable owner_id, NOT by team name — team names
    change season to season (rebrands, typos, emoji), so name-keyed
    aggregation would fragment one manager's history into several
    look-alike "teams". Display name shown is each owner's current name.
    """
    pair_records = {}  # (owner_a, owner_b) sorted -> {owner_a, owner_b, aWins, bWins, ties}

    for league in season_chain:
        league_id = league["league_id"]
        owner_map, _ = build_owner_map(league_id)

        max_week = (league.get("settings") or {}).get("last_scored_leg") or 17

        for week in range(1, max_week + 1):
            matchups = fetch_matchups(league_id, week)
            if not matchups:
                continue
            by_matchup_id = {}
            for m in matchups:
                mid = m.get("matchup_id")
                if mid is None:
                    continue
                by_matchup_id.setdefault(mid, []).append(m)

            for mid, pair in by_matchup_id.items():
                if len(pair) != 2:
                    continue
                a, b = pair
                owner_a = owner_map.get(a["roster_id"], {}).get("owner_id")
                owner_b = owner_map.get(b["roster_id"], {}).get("owner_id")
                if not owner_a or not owner_b:
                    continue
                pts_a, pts_b = a.get("points", 0), b.get("points", 0)
                key = tuple(sorted([owner_a, owner_b]))
                rec = pair_records.setdefault(key, {"owner_a": key[0], "owner_b": key[1], "aWins": 0, "bWins": 0, "ties": 0})
                if pts_a > pts_b:
                    rec["aWins" if owner_a == key[0] else "bWins"] += 1
                elif pts_b > pts_a:
                    rec["bWins" if owner_a == key[0] else "aWins"] += 1
                else:
                    rec["ties"] += 1

    pairs = []
    for rec in pair_records.values():
        pairs.append({
            "a": display_name.get(rec["owner_a"], rec["owner_a"]),
            "b": display_name.get(rec["owner_b"], rec["owner_b"]),
            "aWins": rec["aWins"],
            "bWins": rec["bWins"],
            "ties": rec["ties"],
        })
    return {"pairs": pairs}


def sync_trades(season_chain, player_lookup, display_name):
    """Every trade transaction across every season, shown under each
    manager's CURRENT team name regardless of what it was called when the
    trade happened — keeps the site's manager-vs-manager filter working
    across renamed teams. Includes both players and draft picks moved,
    dated by the trade's actual processed timestamp (not just the season)."""
    trades = []
    for league in season_chain:
        league_id = league["league_id"]
        season_label = league.get("season", league_id)
        owner_map, _ = build_owner_map(league_id)
        max_week = (league.get("settings") or {}).get("last_scored_leg") or 17

        for week in range(1, max_week + 1):
            txns = fetch_json(f"{API_BASE}/league/{league_id}/transactions/{week}")
            if not txns:
                continue
            for t in txns:
                if t.get("type") != "trade" or t.get("status") != "complete":
                    continue
                roster_ids = t.get("roster_ids", [])
                owner_ids = [owner_map.get(rid, {}).get("owner_id") for rid in roster_ids]
                teams = [display_name.get(oid, f"Unknown ({oid})") for oid in owner_ids if oid]
                adds = t.get("adds") or {}
                draft_picks = t.get("draft_picks") or []

                sides = []
                parts = []
                for roster_id in roster_ids:
                    owner_id = owner_map.get(roster_id, {}).get("owner_id")
                    name = display_name.get(owner_id, f"Roster {roster_id}")
                    received_items = []

                    received_players = [pid for pid, rid in adds.items() if rid == roster_id]
                    received_items += [player_lookup.get(pid, pid) for pid in received_players]

                    for pick in draft_picks:
                        if pick.get("owner_id") != roster_id:
                            continue
                        pick_season = pick.get("season", "?")
                        pick_round = pick.get("round", "?")
                        orig_roster = pick.get("roster_id")
                        orig_owner_id = owner_map.get(orig_roster, {}).get("owner_id") if orig_roster else None
                        orig_name = display_name.get(orig_owner_id) if orig_owner_id else None
                        pick_desc = f"{pick_season} Round {pick_round} pick"
                        if orig_name and orig_roster != roster_id:
                            pick_desc += f" (via {orig_name})"
                        received_items.append(pick_desc)

                    if received_items:
                        sides.append({"team": name, "items": received_items})
                        parts.append(f"{name} gets {', '.join(received_items)}")

                # "note" kept as a plain-text fallback summary; "sides" is the
                # structured form the site actually renders (one line per
                # item, not one wall-of-text paragraph).
                note = " · ".join(parts) if parts else " / ".join(teams) + " swap picks/players"

                created_ms = t.get("created")
                if created_ms:
                    date_str = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).strftime("%b %d, %Y")
                else:
                    date_str = season_label  # fallback if Sleeper didn't give a timestamp

                trades.append({
                    "date": date_str,
                    "teams": teams,
                    "sides": sides,
                    "note": note,
                    "_sort_key": created_ms or 0,  # raw epoch ms, stripped before output — formatted date strings don't sort chronologically as text
                })
    trades.sort(key=lambda t: t["_sort_key"], reverse=True)
    for t in trades:
        del t["_sort_key"]
    return {"trades": trades}


def get_playoff_results(league_id):
    """Champion / runner-up (from the winners bracket) and Sacko (from the
    losers/consolation bracket, if the league ran one) for one season.

    Sleeper marks placement games with a "p" field (p=1 is the championship
    game in the winners bracket; the losers bracket's placement games work
    the same way, with the highest "p" value being the true last-place game).
    Returns roster_ids (None where not determinable — e.g. no losers bracket
    was run that season, or the season isn't finished).
    """
    winners = fetch_json(f"{API_BASE}/league/{league_id}/winners_bracket") or []
    losers = fetch_json(f"{API_BASE}/league/{league_id}/losers_bracket") or []

    champion_roster = None
    runner_up_roster = None
    for m in winners:
        if m.get("p") == 1 and m.get("w") is not None:
            champion_roster = m.get("w")
            runner_up_roster = m.get("l")

    sacko_roster = None
    if losers:
        placement_matches = [m for m in losers if m.get("p")]
        if placement_matches:
            last_place_match = max(placement_matches, key=lambda m: m["p"])
            sacko_roster = last_place_match.get("l")

    return champion_roster, runner_up_roster, sacko_roster


def get_regular_season_champion(rosters):
    """Best regular-season record: most wins, points as tiebreak."""
    if not rosters:
        return None
    def sort_key(r):
        s = r.get("settings", {})
        pf = (s.get("fpts", 0) or 0) + (s.get("fpts_decimal", 0) or 0) / 100
        return (s.get("wins", 0) or 0, pf)
    best = max(rosters, key=sort_key)
    return best.get("roster_id")


def get_worst_regular_season(rosters):
    """Fallback Sacko determination when no losers bracket exists:
    worst record, points as tiebreak (fewest wins, then fewest points)."""
    if not rosters:
        return None
    def sort_key(r):
        s = r.get("settings", {})
        pf = (s.get("fpts", 0) or 0) + (s.get("fpts_decimal", 0) or 0) / 100
        return (s.get("wins", 0) or 0, pf)
    worst = min(rosters, key=sort_key)
    return worst.get("roster_id")


def get_division_champions(league_obj, rosters):
    """{division_label: roster_id} for whichever divisions Sleeper actually
    has configured on each roster (roster.settings.division, 1/2/3/...).
    Division NAMES come from the league's own metadata when the commissioner
    set them there; falls back to "Division N" when not available. Returns
    {} for seasons where rosters carry no division field at all — better to
    show nothing than guess at a division structure that might not have
    existed that season.
    """
    by_division = {}
    for r in rosters:
        div_num = (r.get("settings") or {}).get("division")
        if div_num is None:
            continue
        by_division.setdefault(div_num, []).append(r)

    if not by_division:
        return {}

    metadata = league_obj.get("metadata") or {}
    results = {}
    for div_num, div_rosters in by_division.items():
        label = metadata.get(f"division_{div_num}") or f"Division {div_num}"
        champ_roster_id = get_regular_season_champion(div_rosters)
        results[label] = champ_roster_id
    return results


def sync_trophies(season_chain, display_name):
    """Full historical winner list for The Shiva, The Sacko, each Division
    Championship, and the Regular Season Champion — one season at a time,
    computed straight from Sleeper's own results (not the old manual
    workbook). Excludes the current, still-in-progress season (no playoff
    results exist yet to crown anything)."""
    shiva_history = []
    runner_up_history = []
    sacko_history = []
    regular_season_history = []
    division_histories = {}  # label -> [ {season, winner}, ... ]

    for league in season_chain:
        league_id = league["league_id"]
        season_label = league.get("season", league_id)
        status = league.get("status")
        if status != "complete":
            continue  # skip the current/in-progress season — nothing's been decided yet

        owner_map, rosters = build_owner_map(league_id)

        def roster_owner_name(roster_id):
            if roster_id is None:
                return None
            owner_id = owner_map.get(roster_id, {}).get("owner_id")
            return display_name.get(owner_id) if owner_id else None

        champion_roster, runner_up_roster, sacko_roster = get_playoff_results(league_id)
        if champion_roster is not None:
            shiva_history.append({"season": season_label, "winner": roster_owner_name(champion_roster)})
        if runner_up_roster is not None:
            runner_up_history.append({"season": season_label, "winner": roster_owner_name(runner_up_roster)})

        if sacko_roster is None:
            sacko_roster = get_worst_regular_season(rosters)  # fallback: no losers bracket that season
        if sacko_roster is not None:
            sacko_history.append({"season": season_label, "winner": roster_owner_name(sacko_roster)})

        reg_champ_roster = get_regular_season_champion(rosters)
        if reg_champ_roster is not None:
            regular_season_history.append({"season": season_label, "winner": roster_owner_name(reg_champ_roster)})

        for label, roster_id in get_division_champions(league, rosters).items():
            division_histories.setdefault(label, []).append(
                {"season": season_label, "winner": roster_owner_name(roster_id)}
            )

    trophies = [
        {"key": "shiva", "name": "The Shiva", "description": "League Champion", "history": shiva_history},
        {"key": "runner-up", "name": "Runner-Up", "description": "Lost the championship game", "history": runner_up_history},
        {"key": "sacko", "name": "The Sacko", "description": "Last place", "history": sacko_history},
        {"key": "regular-season", "name": "Regular Season Champion", "description": "#1 overall seed", "history": regular_season_history},
    ]
    for label, history in division_histories.items():
        trophies.append({
            "key": "division-" + label.lower().replace(" ", "-"),
            "name": label + " Champion",
            "description": "Best division record",
            "history": history,
        })

    return {"trophies": trophies}


def compute_prediction_lock_at(state):
    """Thursday 8pm ET of the SAME NFL week as the matchups being shown.

    Tied to Sleeper's week number (not "the next Thursday from right now"),
    so once Thursday night passes the lock stays in the past for the rest of
    that week instead of jumping ahead to next week's Thursday while the
    page is still showing this week's games.

    Primary method: Sleeper's /state/nfl reports season_start_date. The
    first Thursday on/after that date is Week 1's Thursday; week N's lock
    is that Thursday + 7*(N-1) days, at 20:00 Eastern (DST handled by the
    timezone database, not by hand).

    Fallback (if season_start_date is missing/unparseable): treat each NFL
    week as running Tuesday -> Monday, matching when Sleeper rolls its week
    number, and lock on that window's Thursday.
    """
    tz = _ET if _ET is not None else timezone(timedelta(hours=-5))  # fixed-offset safety net only

    week = state.get("week")
    start_str = state.get("season_start_date")
    if start_str and week:
        try:
            start = datetime.strptime(start_str, "%Y-%m-%d")
            days_to_thursday = (3 - start.weekday()) % 7
            week1_thursday = start + timedelta(days=days_to_thursday)
            lock_day = week1_thursday + timedelta(days=7 * (int(week) - 1))
            lock = datetime(lock_day.year, lock_day.month, lock_day.day, 20, 0, tzinfo=tz)
            return lock.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError):
            pass  # fall through to the Tuesday-window fallback below

    now = datetime.now(tz)
    days_since_tuesday = (now.weekday() - 1) % 7  # Tue=0 ... Mon=6
    week_start = (now - timedelta(days=days_since_tuesday)).replace(hour=0, minute=0, second=0, microsecond=0)
    lock = (week_start + timedelta(days=2)).replace(hour=20)  # Tuesday + 2 days = Thursday
    return lock.astimezone(timezone.utc).isoformat()


def sync_current_week_predictions(current_league_id):
    """This week's matchups for the Predictions voting feature, with the
    Thursday-8pm-ET lock time. Empty/None week means Sleeper doesn't
    consider the season live right now (offseason)."""
    state = fetch_json(f"{API_BASE}/state/nfl") or {}
    week = state.get("week")
    if not week:
        return {"week": None, "lockAt": None, "matchups": []}

    owner_map, _ = build_owner_map(current_league_id)
    matchups = fetch_json(f"{API_BASE}/league/{current_league_id}/matchups/{week}") or []

    by_matchup_id = {}
    for m in matchups:
        mid = m.get("matchup_id")
        if mid is None:
            continue
        by_matchup_id.setdefault(mid, []).append(m)

    pairs = []
    for pair in by_matchup_id.values():
        if len(pair) != 2:
            continue
        a, b = pair
        name_a = owner_map.get(a["roster_id"], {}).get("name")
        name_b = owner_map.get(b["roster_id"], {}).get("name")
        if name_a and name_b:
            pairs.append({"id": name_a + "__" + name_b, "teamA": name_a, "teamB": name_b})

    return {
        "week": week,
        "lockAt": compute_prediction_lock_at(state),
        "syncedAt": datetime.now(timezone.utc).isoformat(),
        "matchups": pairs,
    }


def sync_prediction_results(current_league_id):
    """Who actually won every FINAL matchup this season, so the Predictions
    scoreboard can grade people's picks.

    Only weeks that are completely over are included. Sleeper's week number
    rolls over after Monday night, so while the season is live, every week
    before the current one is final. A week where both sides have identical
    points (a tie, or a game that was never played) gets winner=None and is
    simply not scored for anyone.

    Matchup ids are the two team names sorted alphabetically and joined with
    "__", so they match regardless of which side Sleeper happens to list first.
    """
    state = fetch_json(f"{API_BASE}/state/nfl") or {}
    league = fetch_json(f"{API_BASE}/league/{current_league_id}") or {}
    last_final_week = compute_last_final_week(league, state)

    owner_map, _ = build_owner_map(current_league_id)
    results = {}
    for w in range(1, last_final_week + 1):
        matchups = fetch_matchups(current_league_id, w)
        by_id = {}
        for m in matchups:
            mid = m.get("matchup_id")
            if mid is None:
                continue
            by_id.setdefault(mid, []).append(m)

        week_results = []
        for pair in by_id.values():
            if len(pair) != 2:
                continue
            a, b = pair
            name_a = owner_map.get(a["roster_id"], {}).get("name")
            name_b = owner_map.get(b["roster_id"], {}).get("name")
            if not name_a or not name_b:
                continue
            pts_a = a.get("points") or 0
            pts_b = b.get("points") or 0
            if pts_a > pts_b:
                winner = name_a
            elif pts_b > pts_a:
                winner = name_b
            else:
                winner = None
            week_results.append({"id": "__".join(sorted([name_a, name_b])), "winner": winner})
        if week_results:
            results[str(w)] = week_results

    return {"season": league.get("season"), "results": results}


def fetch_player_lookup():
    """id -> 'First Last' for all NFL players. One big cached call."""
    players = fetch_json(f"{API_BASE}/players/nfl") or {}
    lookup = {}
    for pid, p in players.items():
        name = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()
        if name:
            lookup[pid] = name
    return lookup


def fetch_prizepool():
    """Pull the Prize Pool Google Sheet (published as CSV) and convert to JSON.

    Done server-side here rather than fetched client-side by the site,
    because Google's "publish to web" CSV endpoint doesn't reliably send
    CORS headers a browser fetch() would need — a plain HTTP request from
    the Action has no such restriction.
    """
    import csv
    import io

    csv_url = (
        "https://docs.google.com/spreadsheets/d/e/"
        "2PACX-1vRuvZYP99HrxQc5DE_4nI6Gv-bm_J8rCCGymyQaALbnTDwgiDv-Vywq8RzbrFBvOw6ZnmgPfrzgYx4X/"
        "pub?gid=1107810375&single=true&output=csv"
    )
    req = urllib.request.Request(csv_url, headers={"User-Agent": "shiva-times-sync/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")

    reader = csv.reader(io.StringIO(raw))
    rows = list(reader)

    # This sheet's real header row (Rank, Manager, Total ($), W1...) is not
    # row 0 — there are title/subtitle rows above it. Find it by looking
    # for the row that starts with "Rank".
    header_idx = next((i for i, r in enumerate(rows) if r and r[0].strip() == "Rank"), None)
    if header_idx is None:
        return {"managers": [], "updated": None}

    header = rows[header_idx]

    # Columns beyond Rank/Manager/Total are the category breakdown (weeks +
    # awards). "BB 2ND"/"BB 1ST" are relabeled here — they're actually the
    # Toilet Bowl payout, not "Best Ball" as originally assumed before the
    # league constitution clarified it.
    LABEL_OVERRIDES = {
        "BB 2ND": "Toilet Bowl — 2nd",
        "BB 1ST": "Toilet Bowl — Winner",
    }
    # The categories are one unbroken run of headers right after "Total ($)"
    # (W1...W14, then the awards). Stop at the first blank header: anything
    # further right is the sheet's hidden sorting-helper block, not prize
    # money. (Those helper cells share the header row, so scanning every
    # non-empty header used to pull them in as fake categories.)
    breakdown_cols = []  # list of (column_index, label)
    for i in range(3, len(header)):
        label = header[i].strip()
        if not label:
            break
        breakdown_cols.append((i, LABEL_OVERRIDES.get(label, label)))

    def parse_money(s):
        s = (s or "").replace("$", "").replace(",", "").strip()
        if not s:
            return 0
        try:
            return float(s)
        except ValueError:
            return 0

    managers = []
    for row in rows[header_idx + 1:]:
        if len(row) < 3 or not row[1].strip():
            continue
        name = row[1].strip()
        total = parse_money(row[2])

        breakdown = []
        for col_idx, label in breakdown_cols:
            if col_idx >= len(row):
                continue
            amount = parse_money(row[col_idx])
            if amount:  # only nonzero entries — most categories are $0 for most managers
                breakdown.append({"label": label, "amount": amount})

        breakdown_sum = sum(b["amount"] for b in breakdown)
        if abs(breakdown_sum - total) > 0.01:
            print(f"WARNING: prize pool mismatch for {name}: categories add up to "
                  f"${breakdown_sum:g} but the sheet's total says ${total:g} "
                  f"- check the published tab for stray columns or edited headers.")

        managers.append({"name": name, "total": total, "breakdown": breakdown})

    import datetime
    return {"managers": managers, "updated": datetime.date.today().isoformat()}


# =====================================================================
# FRONT PAGE: weekly recap (auto-written from the scores), announcements
# (from a tab in the Prize Pool Google Sheet), and the league calendar.
# Each of these runs inside safe_step() in main(), so a problem here can
# never stop the core sync (standings, head-to-head, trades, ...).
# =====================================================================

_MATCHUP_CACHE = {}


def fetch_matchups(league_id, week):
    """One week of matchup rows for a league. Cached, so the several sync steps
    that look at the same week (head-to-head, results, recaps) share one request."""
    key = (str(league_id), int(week))
    if key not in _MATCHUP_CACHE:
        _MATCHUP_CACHE[key] = fetch_json(f"{API_BASE}/league/{league_id}/matchups/{week}") or []
    return _MATCHUP_CACHE[key]


def compute_last_final_week(league, state):
    """Highest week that is completely over. Sleeper rolls its week number after
    Monday night, so during the regular season every week before the current one is final."""
    status = league.get("status")
    season_type = state.get("season_type")
    try:
        week = int(state.get("week") or 0)
    except (TypeError, ValueError):
        week = 0
    if status == "complete":
        return 18
    if status == "in_season" and season_type == "regular":
        return max(week - 1, 0)
    if status == "in_season" and season_type == "post":
        return 18
    return 0  # pre-draft / drafting / offseason


def load_local_json(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def write_json(name, data):
    with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def safe_step(label, build, filename):
    """Run an optional step. If it fails, say so loudly in the log but keep the
    previous file and carry on, so the core sync is never taken down by an extra."""
    try:
        write_json(filename, build())
    except Exception as e:  # deliberately broad: this is the safety net
        print(f"WARNING: {label} failed ({type(e).__name__}: {e}). Keeping the previous {filename}.")


# ---------- small text helpers ----------

def clean_name(name):
    return re.sub(r"\s+", " ", str(name)).strip()


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode().lower())


def _num(x):
    """121.5, 98.24, 100 (no trailing zeros)."""
    return ("%.2f" % x).rstrip("0").rstrip(".")


def _suffix(n):
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


_HIGH = {1: "highest", 2: "second-highest", 3: "third-highest", 4: "fourth-highest", 5: "fifth-highest", 6: "sixth-highest"}
_LOW = {1: "lowest", 2: "second-lowest", 3: "third-lowest", 4: "fourth-lowest", 5: "fifth-lowest", 6: "sixth-lowest"}


def high_word(n):
    return _HIGH.get(n, f"{n}{_suffix(n)}-highest")


def low_word(n):
    return _LOW.get(n, f"{n}{_suffix(n)}-lowest")


# ---------- the phrase bank (every line is built only from the week's real numbers) ----------
# Fields: T team, O opponent, S/OS their scores, W/L winner/loser, WS/LS their scores, M margin,
# MED league median, DIFF score minus median, RANKW "third-highest" etc, N streak length, REC "4-1".
RECAP_PHRASES = {
    # Written to read correctly for ANY team name: past tense, no possessives ("MAGRAUDERS's"), no is/has/are after a name.
    "head_explosion": ["{T} Dropped {S} on the League", "{T} Went Nuclear for {S}", "{S} Points: {T} Played a Different Game"],
    "head_season_high": ["{T} Posted a Season-Best {S}", "A New Season High: {S} From {T}", "{T} Set the Season Bar at {S}"],
    "head_blowout": ["{L} Steamrolled by {W}, {WS}-{LS}", "{W} Over {L} in a {M}-Point Rout", "A {M}-Point Rout: {W} Over {L}"],
    "head_squeaker": ["{W} Survived {L} by {M}", "{W} Edged {L} by {M}", "Separated by {M}: {W} and {L}"],
    "head_tie": ["{W} and {L} Settled for a Tie"],
    "lead_blowout": [
        "It was less a game than a demolition: {W} {WS}, {L} {LS}. The {M}-point margin was the widest of the week.",
        "It was never close. {W} put up {WS} against {L} ({LS}), and the {M}-point gap was the biggest anyone managed all week.",
        "{L} showed up, which is about the nicest thing that can be said. {W} won {WS} to {LS}, a {M}-point beating that was the week's widest.",
    ],
    "lead_squeaker": [
        "The week's closest game was decided by {M}: {W} {WS}, {L} {LS}. On the losing side, someone is replaying every lineup decision.",
        "{W} survived {L}, {WS} to {LS}. The {M}-point margin was the tightest of the week, and one side will claim it was never in doubt.",
        "By {M}. That is all that separated {W} ({WS}) from {L} ({LS}), the tightest margin of the week.",
    ],
    "lead_tie": ["{W} and {L} finished in a dead heat at {WS}. Nobody won, everybody is mad."],
    "lead_top_win": [
        "{T} scored {S}, the highest total in the league, and beat {O} ({OS}) with room to spare. The league median this week was {MED}.",
        "Nobody topped {S} this week, and that number belonged to {T}, who beat {O} ({OS}). The league median was {MED}.",
        "{T} put up {S} against {O} ({OS}). The league median was {MED}; {T} cleared it by {DIFF}.",
    ],
    "lead_top_loss": [
        "{T} scored {S}, the highest total in the league, and still lost to {O}, who put up {OS}. That is the kind of week that makes people question the hobby.",
        "{T} led the league with {S} and took a loss anyway; {O} won {OS} to {S}. The schedule is a cruel master.",
    ],
    "lead_top_tie": ["{T} scored {S}, the highest total in the league, and still only managed a tie with {O}."],
    "season_best": ["That is the best score of the season so far.", "It is also the best score of the season so far."],
    "season_worst": ["That is the lowest score of the season so far.", "It is also the lowest score of the season so far."],
    "dek_widest": ["The widest margin of the week: {W} over {L} by {M}."],
    "dek_closest": ["The closest game was decided by {M}: {W} over {L}."],
    "dek_low": ["{T} scored {S}, the lowest total in the league."],
    "top_line": ["{T} led the league with {S}.", "{T} paced all scorers at {S}.", "The week's top score belonged to {T}: {S}."],
    "low_line": [
        "At the other end, {T} managed {S}, the lowest total in the league.",
        "{T} brought up the rear with {S}. The bench would like a word.",
        "Thoughts and prayers to {T}, who scored a league-worst {S}.",
    ],
    "unlucky": [
        "{T} scored {S}, the {RANKW} total of the week, and still lost to {O} ({OS}). The fantasy gods are cruel.",
        "Hard-luck award: {T}. {S} points was the {RANKW} score of the week, and it lost to {O} ({OS}).",
        "{T} put up {S}, the {RANKW} score in the league, and took a loss for it. {O} won with {OS}.",
    ],
    "lucky": [
        "{T} won with {S}, the {RANKW} score of the week. Nobody asked how; the win counts.",
        "{T} beat {O} with a modest {S}, the {RANKW} total in the league. The standings do not check how you got there.",
        "Free win of the week: {T}, who scored {S} (the {RANKW} total) and still beat {O} ({OS}).",
    ],
    "win_streak": ["Longest winning streak: {T}, at {N} straight.", "On a roll: {T}, {N} wins in a row and counting.", "{T} arrived at {N} consecutive wins this week, with no apparent plans to stop."],
    "loss_streak": ["Longest losing streak: {T}, at {N} straight. Send help.", "Cold streak: {T}, {N} losses in a row and still falling.", "{T} sank to {N} straight losses this week."],
    "leader": ["The league's best record, {REC}, belongs to {T}.", "Atop the standings: {T}, at {REC}.", "Leading the league at {REC}: {T}."],
    "leader_tied": ["Out front at {REC}: {T}, ahead of {OTHERS} with that record on the points tiebreak.", "{T} headed a crowded top at {REC}, edging {OTHERS} on points."],
    "last": ["Last place belongs to {T}, at {REC}.", "Holding down last place, technically: {T}, {REC}.", "At the bottom of the standings: {T}, {REC}."],
    "week1": ["Week 1 is in the books, which means every team is either undefeated or winless. Do not get attached to either."],
    "rivalry_intro": ["It was Rivalry Week, and the grudges were settled the only way this league knows how."],
    "rivalry_win": ["{TITLE} went to {W}, {WS} to {LS}."],
    "rivalry_tie": ["{TITLE} ended in a tie at {WS}."],
}


def _others(k):
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
    return f"{words.get(k, k)} other team" + ("" if k == 1 else "s")


def build_recap(week, games_by_week, season, published=None, rivalries=None):
    """Write one week's recap from its real scores. Pure function: same input, same
    article, every time (phrase choices are seeded by season + week, not by chance)."""
    games = games_by_week.get(week) or []
    played = [g for g in games if g["ascore"] or g["bscore"]]
    if not played:
        return None

    def say(key, **f):
        options = RECAP_PHRASES[key]
        return random.Random(f"{season}|{week}|{key}").choice(options).format(**f)

    def margin(g):
        return abs(g["ascore"] - g["bscore"])

    def wl(g):  # winner, loser, winner score, loser score
        if g["ascore"] >= g["bscore"]:
            return g["a"], g["b"], g["ascore"], g["bscore"]
        return g["b"], g["a"], g["bscore"], g["ascore"]

    # every team's line for the week
    lines = []
    for g in played:
        for me, opp, ms, os_ in ((g["a"], g["b"], g["ascore"], g["bscore"]), (g["b"], g["a"], g["bscore"], g["ascore"])):
            lines.append({"team": me, "score": ms, "opp": opp, "opp_score": os_,
                          "res": "W" if ms > os_ else "L" if ms < os_ else "T"})
    n = len(lines)
    by_score = sorted(lines, key=lambda l: (-l["score"], l["team"]))
    rank = {l["team"]: i + 1 for i, l in enumerate(by_score)}
    top, bottom = by_score[0], by_score[-1]
    median = statistics.median(l["score"] for l in lines)

    blowout = max(played, key=lambda g: (margin(g), g["a"]))
    closest = min(played, key=lambda g: (margin(g), g["a"]))
    bw, bl, bws, bls = wl(blowout)
    cw, cl, cws, cls_ = wl(closest)
    bm, cm = margin(blowout), margin(closest)

    # season context
    prev = [x for w in range(1, week) for g in (games_by_week.get(w) or []) if (g["ascore"] or g["bscore"]) for x in (g["ascore"], g["bscore"])]
    season_high = bool(prev) and top["score"] > max(prev)
    season_low = bool(prev) and bottom["score"] < min(prev)

    rec = {}
    for w in range(1, week + 1):
        for g in games_by_week.get(w) or []:
            if not (g["ascore"] or g["bscore"]):
                continue
            for me, ms, os_ in ((g["a"], g["ascore"], g["bscore"]), (g["b"], g["bscore"], g["ascore"])):
                r = rec.setdefault(me, {"w": 0, "l": 0, "t": 0, "pf": 0.0, "seq": []})
                r["pf"] += ms
                key = "w" if ms > os_ else "l" if ms < os_ else "t"
                r[key] += 1
                r["seq"].append(key.upper())

    def rec_str(r):
        return f"{r['w']}-{r['l']}" + (f"-{r['t']}" if r["t"] else "")

    def streak(r):
        if not r["seq"]:
            return None, 0
        last, run = r["seq"][-1], 0
        for x in reversed(r["seq"]):
            if x != last:
                break
            run += 1
        return last, run

    # which story leads
    if top["score"] >= 150:
        kind = "explosion"
    elif bm >= 45:
        kind = "blowout"
    elif cm <= 3:
        kind = "squeaker"
    elif season_high:
        kind = "season_high"
    else:
        kind = "blowout"

    med_s = _num(median)
    top_f = dict(T=top["team"], S=_num(top["score"]), O=top["opp"], OS=_num(top["opp_score"]), MED=med_s, DIFF=_num(top["score"] - median))

    # headline + lead paragraph
    if kind in ("explosion", "season_high"):
        headline = say("head_explosion" if kind == "explosion" else "head_season_high", **top_f)
        lead_key = {"W": "lead_top_win", "L": "lead_top_loss", "T": "lead_top_tie"}[top["res"]]
        lead = say(lead_key, **top_f)
        if season_high:
            lead += " " + say("season_best")
    elif kind == "squeaker":
        f = dict(W=cw, L=cl, WS=_num(cws), LS=_num(cls_), M=_num(cm))
        if cm == 0:
            headline, lead = say("head_tie", **f), say("lead_tie", **f)
        else:
            headline, lead = say("head_squeaker", **f), say("lead_squeaker", **f)
    else:
        f = dict(W=bw, L=bl, WS=_num(bws), LS=_num(bls), M=_num(bm))
        headline, lead = say("head_blowout", **f), say("lead_blowout", **f)

    # dek: a different fact from the lead
    if kind != "blowout":
        dek = say("dek_widest", W=bw, L=bl, M=_num(bm))
    elif cm <= 10 and closest is not blowout:
        dek = say("dek_closest", W=cw, L=cl, M=_num(cm))
    else:
        dek = say("dek_low", T=bottom["team"], S=_num(bottom["score"]))

    paragraphs = [lead]

    # Rivalry Week: results of each named rivalry
    if rivalries and str(rivalries.get("week")) == str(week):
        sents = []
        for r in rivalries.get("rivalries") or []:
            try:
                pa, pb = _norm(r["a"]["team"]), _norm(r["b"]["team"])
            except (KeyError, TypeError):
                continue
            for g in played:
                if {_norm(g["a"]), _norm(g["b"])} == {pa, pb}:
                    w_, l_, ws_, ls_ = wl(g)
                    if margin(g) == 0:
                        sents.append(say("rivalry_tie", TITLE=r.get("title", "The rivalry"), WS=_num(ws_)))
                    else:
                        sents.append(say("rivalry_win", TITLE=r.get("title", "The rivalry"), W=w_, WS=_num(ws_), LS=_num(ls_)))
                    break
        if sents:
            paragraphs.append(say("rivalry_intro") + " " + " ".join(sents))

    # extremes
    ext = []
    if kind not in ("explosion", "season_high"):
        ext.append(say("top_line", T=top["team"], S=_num(top["score"])))
        if season_high:
            ext.append(say("season_best"))
    if bottom["team"] != top["team"]:
        ext.append(say("low_line", T=bottom["team"], S=_num(bottom["score"])))
        if season_low:
            ext.append(say("season_worst"))
    if ext:
        paragraphs.append(" ".join(ext))

    # luck
    luck = []
    third = -(-n // 3)  # ceil(n / 3)
    losers = [l for l in lines if l["res"] == "L"]
    winners = [l for l in lines if l["res"] == "W"]
    if losers:
        u = max(losers, key=lambda l: (l["score"], l["team"]))
        if rank[u["team"]] <= third and not (kind in ("explosion", "season_high") and u["team"] == top["team"]):
            luck.append(say("unlucky", T=u["team"], S=_num(u["score"]), RANKW=high_word(rank[u["team"]]), O=u["opp"], OS=_num(u["opp_score"])))
    if winners:
        k = min(winners, key=lambda l: (l["score"], l["team"]))
        if rank[k["team"]] > n - third:
            luck.append(say("lucky", T=k["team"], S=_num(k["score"]), RANKW=low_word(n - rank[k["team"]] + 1), O=k["opp"], OS=_num(k["opp_score"])))
    if luck:
        paragraphs.append(" ".join(luck))

    # standings, streaks
    if week == 1:
        paragraphs.append(say("week1"))
    elif rec:
        table = sorted(rec.items(), key=lambda kv: (-kv[1]["w"], -kv[1]["pf"], kv[0]))
        lead_rec = (table[0][1]["w"], table[0][1]["l"], table[0][1]["t"])
        sharing = sum(1 for _, r in table[1:] if (r["w"], r["l"], r["t"]) == lead_rec)
        if sharing:
            tail = [say("leader_tied", T=table[0][0], REC=rec_str(table[0][1]), OTHERS=_others(sharing))]
        else:
            tail = [say("leader", T=table[0][0], REC=rec_str(table[0][1]))]
        streaks = [(t, *streak(r)) for t, r in rec.items()]
        # longest streak wins the mention; ties go to the team with the most points (win streaks) or the fewest (losing streaks)
        wins = sorted([s for s in streaks if s[1] == "W" and s[2] >= 3], key=lambda s: (-s[2], -rec[s[0]]["pf"], s[0]))
        loss = sorted([s for s in streaks if s[1] == "L" and s[2] >= 3], key=lambda s: (-s[2], rec[s[0]]["pf"], s[0]))
        if wins:
            tail.append(say("win_streak", T=wins[0][0], N=wins[0][2]))
        if loss:
            tail.append(say("loss_streak", T=loss[0][0], N=loss[0][2]))
        tail.append(say("last", T=table[-1][0], REC=rec_str(table[-1][1])))
        paragraphs.append(" ".join(tail))

    return {
        "id": f"recap-{week}", "type": "recap", "week": week, "date": published, "kind": kind,
        "headline": headline, "dek": dek, "byline": "The Shiva Times Staff",
        "paragraphs": paragraphs,
        "results": [{"a": g["a"], "b": g["b"], "aScore": round(g["ascore"], 2), "bScore": round(g["bscore"], 2)} for g in played],
    }


def collect_games(league_id, weeks, owner_map):
    games_by_week = {}
    for w in weeks:
        by_id = {}
        for m in fetch_matchups(league_id, w):
            mid = m.get("matchup_id")
            if mid is None:
                continue
            by_id.setdefault(mid, []).append(m)
        games = []
        for mid in sorted(by_id):
            pair = by_id[mid]
            if len(pair) != 2:
                continue
            a, b = pair
            na = owner_map.get(a["roster_id"], {}).get("name")
            nb = owner_map.get(b["roster_id"], {}).get("name")
            if not na or not nb:
                continue
            games.append({"a": clean_name(na), "b": clean_name(nb),
                          "ascore": float(a.get("points") or 0), "bscore": float(b.get("points") or 0)})
        games_by_week[w] = games
    return games_by_week


def week_date(start_str, week, edge="start"):
    """Date of an NFL/Sleeper week: its Thursday (start) or the Monday that closes it (end)."""
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    thursday = start + timedelta(days=(3 - start.weekday()) % 7) + timedelta(days=7 * (int(week) - 1))
    return (thursday + timedelta(days=4)).date() if edge == "end" else thursday.date()


def sync_recaps(current_league_id):
    state = fetch_json(f"{API_BASE}/state/nfl") or {}
    league = fetch_json(f"{API_BASE}/league/{current_league_id}") or {}
    season = str(league.get("season") or state.get("season") or "")
    settings = league.get("settings") or {}
    try:
        regular_weeks = int(settings.get("playoff_week_start") or 15) - 1
    except (TypeError, ValueError):
        regular_weeks = 14
    upto = min(compute_last_final_week(league, state), regular_weeks)  # regular season only for now
    if upto < 1:
        return {"season": season, "recaps": []}
    owner_map, _ = build_owner_map(current_league_id)
    games = collect_games(current_league_id, range(1, upto + 1), owner_map)
    try:
        rivalries = load_local_json("rivalries.json")
    except Exception:
        rivalries = None
    recaps = []
    for w in range(1, upto + 1):
        d = week_date(state.get("season_start_date"), w)
        published = (d + timedelta(days=5)).isoformat() if d else None  # the Tuesday after the week
        r = build_recap(w, games, season, published, rivalries)
        if r:
            recaps.append(r)
    recaps.sort(key=lambda r: -r["week"])
    return {"season": season, "recaps": recaps}


# ---------- announcements (a tab in the Prize Pool Google Sheet, published as CSV) ----------
# Columns: Date | Type | Title | Body | Pinned      (Type is "post" or "event"; blank means post)
# To turn this on: publish that tab (File > Share > Publish to web > CSV) and paste its link here.
ANNOUNCEMENTS_CSV_URL = ""

_TRUTHY = {"yes", "y", "true", "1", "x", "pin", "pinned", "oui"}


def parse_flexible_date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_announcements(text):
    rows = [r for r in csv.reader(io.StringIO(text.lstrip("\ufeff")))]
    head_i = next((i for i, r in enumerate(rows) if any(c.strip().lower() == "title" for c in r)), None)
    if head_i is None:
        return {"posts": [], "events": []}
    cols = {c.strip().lower(): i for i, c in enumerate(rows[head_i])}

    def cell(r, name):
        i = cols.get(name)
        return r[i].strip() if i is not None and i < len(r) else ""

    posts, events = [], []
    for r in rows[head_i + 1:]:
        title = cell(r, "title")
        if not title:
            continue
        date = parse_flexible_date(cell(r, "date"))
        body = cell(r, "body")
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", body) if p.strip()]
        if cell(r, "type").lower().startswith("event"):
            if date:  # an event with no usable date can't be placed on a calendar
                events.append({"date": date, "title": title, "note": " ".join(paragraphs)})
        else:
            posts.append({"id": f"post-{len(posts) + 1}", "type": "announcement", "date": date, "title": title,
                          "paragraphs": paragraphs, "pinned": cell(r, "pinned").lower() in _TRUTHY})
    posts.sort(key=lambda p: (not p["pinned"], -(int(p["date"].replace("-", "")) if p["date"] else 0)))
    events.sort(key=lambda e: e["date"])
    return {"posts": posts, "events": events}


def fetch_announcements(url=None):
    url = ANNOUNCEMENTS_CSV_URL if url is None else url
    if not url:
        print("Announcements tab not connected yet (ANNOUNCEMENTS_CSV_URL is empty); skipping.")
        return {"configured": False, "posts": [], "events": []}
    req = urllib.request.Request(url, headers={"User-Agent": "shiva-times-sync/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8")
    out = parse_announcements(text)
    out["configured"] = True
    return out


# ---------- league calendar (fixed by the constitution; dates computed from Sleeper's season start) ----------
CALENDAR_RULES = [
    {"key": "rivalry", "label": "Rivalry Week", "week": 9, "edge": "start", "note": "Every team plays its designated rival"},
    {"key": "trade_deadline", "label": "Trade deadline", "week": 13, "edge": "end", "note": "End of Week 13. Trades reopen the day after the Shiva"},
    {"key": "regular_season_end", "label": "Regular season ends", "week": 14, "edge": "end", "note": "End of Week 14"},
    {"key": "playoffs", "label": "Playoffs begin", "week": 15, "edge": "start", "note": "Top two division winners get a bye"},
    {"key": "playoffs_round2", "label": "Playoffs, round two", "week": 16, "edge": "start", "note": ""},
    {"key": "shiva", "label": "The Shiva final", "week": 17, "edge": "start", "note": ""},
]


def sync_calendar(current_league_id):
    state = fetch_json(f"{API_BASE}/state/nfl") or {}
    league = fetch_json(f"{API_BASE}/league/{current_league_id}") or {}
    start = state.get("season_start_date")
    rivalry_week = 9
    try:
        rivalry_week = int(load_local_json("rivalries.json").get("week") or 9)
    except Exception:
        pass
    events = []
    for rule in CALENDAR_RULES:
        week = rivalry_week if rule["key"] == "rivalry" else rule["week"]
        d = week_date(start, week, rule["edge"])
        events.append({"key": rule["key"], "label": rule["label"], "week": week, "edge": rule["edge"],
                       "date": d.isoformat() if d else None, "note": rule["note"]})
    return {"season": str(league.get("season") or state.get("season") or ""), "seasonStart": start,
            "currentWeek": state.get("week"), "events": events}



def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    print("Walking season chain...")
    season_chain = get_season_chain(CURRENT_LEAGUE_ID)
    print(f"Found {len(season_chain)} season(s): "
          f"{[s.get('season') for s in season_chain]}")

    print("Syncing standings...")
    standings = sync_standings(CURRENT_LEAGUE_ID)
    with open(os.path.join(DATA_DIR, "standings.json"), "w", encoding="utf-8") as f:
        json.dump(standings, f, ensure_ascii=False, indent=2)

    print("Building current display names for each manager...")
    display_name = build_display_names(season_chain)

    print("Syncing head-to-head (this walks every week of every season, slowest step)...")
    h2h = sync_head_to_head(season_chain, display_name)
    with open(os.path.join(DATA_DIR, "headtohead.json"), "w", encoding="utf-8") as f:
        json.dump(h2h, f, ensure_ascii=False, indent=2)

    print("Fetching player name lookup...")
    player_lookup = fetch_player_lookup()

    print("Syncing trade history...")
    trades = sync_trades(season_chain, player_lookup, display_name)
    with open(os.path.join(DATA_DIR, "trades.json"), "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)

    print("Syncing prize pool from Google Sheet...")
    prizepool = fetch_prizepool()
    with open(os.path.join(DATA_DIR, "prizepool.json"), "w", encoding="utf-8") as f:
        json.dump(prizepool, f, ensure_ascii=False, indent=2)

    print("Syncing trophies (Shiva, Sacko, Division Champs, Regular Season Champ)...")
    trophies = sync_trophies(season_chain, display_name)
    with open(os.path.join(DATA_DIR, "trophies.json"), "w", encoding="utf-8") as f:
        json.dump(trophies, f, ensure_ascii=False, indent=2)

    print("Syncing this week's matchups for Predictions voting...")
    predictions = sync_current_week_predictions(CURRENT_LEAGUE_ID)
    with open(os.path.join(DATA_DIR, "predictions_matchups.json"), "w", encoding="utf-8") as f:
        json.dump(predictions, f, ensure_ascii=False, indent=2)

    print("Syncing final matchup results for the Predictions scoreboard...")
    prediction_results = sync_prediction_results(CURRENT_LEAGUE_ID)
    with open(os.path.join(DATA_DIR, "predictions_results.json"), "w", encoding="utf-8") as f:
        json.dump(prediction_results, f, ensure_ascii=False, indent=2)

    print("Writing weekly recaps from the scores...")
    safe_step("weekly recaps", lambda: sync_recaps(CURRENT_LEAGUE_ID), "recap.json")

    print("Reading announcements from the Google Sheet...")
    safe_step("announcements", fetch_announcements, "announcements.json")

    print("Building the league calendar...")
    safe_step("calendar", lambda: sync_calendar(CURRENT_LEAGUE_ID), "calendar.json")

    print("Done.")


if __name__ == "__main__":
    main()
