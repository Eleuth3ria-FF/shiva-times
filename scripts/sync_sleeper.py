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
    """roster_id -> {owner_id, name} for one season.

    owner_id is Sleeper's stable per-manager account ID — use THIS for
    aggregating across seasons. name is whatever that manager's team was
    called THAT season, which can and does change year to year (typos,
    emoji, rebrands) — display-only, never a merge key.
    """
    users = fetch_json(f"{API_BASE}/league/{league_id}/users") or []
    rosters = fetch_json(f"{API_BASE}/league/{league_id}/rosters") or []
    user_by_id = {u["user_id"]: u for u in users}
    owner_map = {}
    for r in rosters:
        owner_id = r.get("owner_id")
        u = user_by_id.get(owner_id, {})
        name = (u.get("metadata") or {}).get("team_name") or u.get("display_name") or f"Roster {r['roster_id']}"
        owner_map[r["roster_id"]] = {"owner_id": owner_id, "name": name}
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
        info = owner_map.get(r["roster_id"], {"owner_id": None, "name": "Unknown"})
        name = info["name"]
        s = r.get("settings", {})
        entry = {
            "team": name,
            "owner": name,
            "wins": s.get("wins", 0),
            "losses": s.get("losses", 0),
            "ties": s.get("ties", 0),
            "pointsFor": round((s.get("fpts", 0) or 0) + (s.get("fpts_decimal", 0) or 0) / 100, 1),
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
                teams.append({"team": t, "owner": None, "wins": 0, "losses": 0, "ties": 0, "pointsFor": None})
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
    across renamed teams."""
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
                parts = []
                for roster_id in roster_ids:
                    owner_id = owner_map.get(roster_id, {}).get("owner_id")
                    name = display_name.get(owner_id, f"Roster {roster_id}")
                    received = [pid for pid, rid in adds.items() if rid == roster_id]
                    received_names = [player_lookup.get(pid, pid) for pid in received]
                    if received_names:
                        parts.append(f"{name} gets {', '.join(received_names)}")
                note = " · ".join(parts) if parts else " / ".join(teams) + " swap picks/players"
                trades.append({
                    "date": season_label,
                    "teams": teams,
                    "note": note,
                })
    trades.sort(key=lambda t: t["date"], reverse=True)
    return {"trades": trades}


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

    managers = []
    for row in rows[header_idx + 1:]:
        if len(row) < 3 or not row[1].strip():
            continue
        name = row[1].strip()
        total_str = row[2].replace("$", "").replace(",", "").strip()
        try:
            total = float(total_str) if total_str else 0
        except ValueError:
            total = 0
        managers.append({"name": name, "total": total})

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

    print("Done.")


if __name__ == "__main__":
    main()
