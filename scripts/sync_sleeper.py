#!/usr/bin/env python3
"""
Sleeper league sync for The Shiva Times.

Walks the league's season chain (via previous_league_id), pulls
standings, head-to-head results, and trade history, and writes JSON
files into /data for the static site to read.

Runs on Sleeper's public, unauthenticated API — no API key needed.
Docs: https://docs.sleeper.com/
"""

import json
import os
import time
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
            matchups = fetch_json(f"{API_BASE}/league/{league_id}/matchups/{week}")
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
    status = league.get("status")
    season_type = state.get("season_type")

    try:
        week = int(state.get("week") or 0)
    except (TypeError, ValueError):
        week = 0

    if status == "complete":
        last_final_week = 18  # whole season is in the books
    elif status == "in_season" and season_type == "regular":
        last_final_week = max(week - 1, 0)
    elif status == "in_season" and season_type == "post":
        last_final_week = 18
    else:
        last_final_week = 0  # pre-draft / drafting / offseason: nothing to grade yet

    owner_map, _ = build_owner_map(current_league_id)
    results = {}
    for w in range(1, last_final_week + 1):
        matchups = fetch_json(f"{API_BASE}/league/{current_league_id}/matchups/{w}") or []
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

    print("Done.")


if __name__ == "__main__":
    main()
