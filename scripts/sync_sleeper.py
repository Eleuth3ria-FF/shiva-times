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
    "North": ["Scuderia Malo", "Mean Machine", "La Quiche", "Tebows Before Hoes"],
    "Central": ["Weapon X", "T-Boy Rays", "Gridiron Sheriffs", "Magorama"],
    "South": ["SGB Nation", "Les Pingouins", "Lavaltrée's Piggy", "MTL New Empire"],
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
    """roster_id -> display name, for one season."""
    users = fetch_json(f"{API_BASE}/league/{league_id}/users") or []
    rosters = fetch_json(f"{API_BASE}/league/{league_id}/rosters") or []
    user_by_id = {u["user_id"]: u for u in users}
    owner_map = {}
    for r in rosters:
        u = user_by_id.get(r.get("owner_id"), {})
        name = (u.get("metadata") or {}).get("team_name") or u.get("display_name") or f"Roster {r['roster_id']}"
        owner_map[r["roster_id"]] = name
    return owner_map, rosters


def sync_standings(current_league_id):
    """Current season standings, grouped into the known divisions."""
    owner_map, rosters = build_owner_map(current_league_id)

    by_name = {}
    for r in rosters:
        name = owner_map.get(r["roster_id"], "Unknown")
        s = r.get("settings", {})
        by_name[name] = {
            "team": name,
            "owner": name,
            "wins": s.get("wins", 0),
            "losses": s.get("losses", 0),
            "ties": s.get("ties", 0),
            "pointsFor": round((s.get("fpts", 0) or 0) + (s.get("fpts_decimal", 0) or 0) / 100, 1),
        }

    divisions = []
    placed = set()
    for div_name, team_names in DIVISIONS.items():
        teams = []
        for t in team_names:
            match = by_name.get(t)
            if match:
                teams.append(match)
                placed.add(t)
            else:
                # Team not found under expected name this season — show as unknown
                # rather than silently dropping it, so it's visible something needs mapping.
                teams.append({"team": t, "owner": None, "wins": 0, "losses": 0, "ties": 0, "pointsFor": None})
        divisions.append({"name": div_name, "teams": teams})

    return {"season": str(current_league_id), "divisions": divisions}


def sync_head_to_head(season_chain):
    """All-time head-to-head record between every pair, across every season."""
    pair_records = {}  # frozenset({a,b}) -> {a: wins, b: wins, ties}

    for league in season_chain:
        league_id = league["league_id"]
        owner_map, _ = build_owner_map(league_id)
        # Weeks: use league settings if available, else assume up to 17
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
                name_a = owner_map.get(a["roster_id"])
                name_b = owner_map.get(b["roster_id"])
                if not name_a or not name_b:
                    continue
                pts_a, pts_b = a.get("points", 0), b.get("points", 0)
                key = tuple(sorted([name_a, name_b]))
                rec = pair_records.setdefault(key, {"a": key[0], "b": key[1], "aWins": 0, "bWins": 0, "ties": 0})
                if pts_a > pts_b:
                    rec["aWins" if name_a == key[0] else "bWins"] += 1
                elif pts_b > pts_a:
                    rec["bWins" if name_a == key[0] else "aWins"] += 1
                else:
                    rec["ties"] += 1

    return {"pairs": list(pair_records.values())}


def sync_trades(season_chain, player_lookup):
    """Every trade transaction across every season."""
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
                teams = [owner_map.get(rid, f"Roster {rid}") for rid in t.get("roster_ids", [])]
                adds = t.get("adds") or {}
                parts = []
                for roster_id in t.get("roster_ids", []):
                    name = owner_map.get(roster_id, f"Roster {roster_id}")
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

    print("Syncing head-to-head (this walks every week of every season, slowest step)...")
    h2h = sync_head_to_head(season_chain)
    with open(os.path.join(DATA_DIR, "headtohead.json"), "w", encoding="utf-8") as f:
        json.dump(h2h, f, ensure_ascii=False, indent=2)

    print("Fetching player name lookup...")
    player_lookup = fetch_player_lookup()

    print("Syncing trade history...")
    trades = sync_trades(season_chain, player_lookup)
    with open(os.path.join(DATA_DIR, "trades.json"), "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)

    print("Done.")


if __name__ == "__main__":
    main()
