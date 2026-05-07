#!/usr/bin/env python3
import argparse
import csv
import json
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "polymarket-sports-bot/3.0",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
})

LEAGUE_TAG_IDS = {
    "laliga": "780",
    "bundesliga": "1494",
    "ligue1": "102070",
    "serie_a": "101962",
    "champions_league": "1234",
    "mls": "100100",
    "nfl": "450",
    "nhl": "899",
    "epl": "82",
    "mlb": "100381",
    "nba": "745",
}

def parse_args():
    parser = argparse.ArgumentParser(description="Export Polymarket sports rows to CSV using CLOB live prices")
    parser.add_argument("league", help="League name: mlb, nba, or epl")
    parser.add_argument("--hours", type=float, default=24.0, help="Look-ahead window in hours")
    parser.add_argument("--live-grace-hours", type=float, default=8.0, help="Include games that started within the last N hours")
    parser.add_argument("--interval", type=int, default=60, help="Refresh interval in seconds")
    parser.add_argument("--limit", type=int, default=500, help="Events per page")
    parser.add_argument("--max-pages", type=int, default=20, help="Maximum pages to fetch")
    parser.add_argument("--timezone", default="Europe/Berlin", help="Output timezone, e.g. Europe/Berlin")
    parser.add_argument("--once", action="store_true", help="Run once instead of looping")
    parser.add_argument("--debug", action="store_true", help="Print debug info")
    return parser.parse_args()


def parse_json_array(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return []


def safe_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        try:
            value = value.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None


def sanitize_filename(name):
    return f"{re.sub(r'[^a-z0-9_-]+', '_', name.strip().lower())}"


def dt_to_date_time(dt, tz_name="Europe/Berlin"):
    if not dt:
        return "", ""
    local_dt = dt.astimezone(ZoneInfo(tz_name))
    return local_dt.strftime("%Y-%m-%d"), local_dt.strftime("%H:%M:%S")


def format_dollar_compact(value):
    v = safe_float(value, 0.0) or 0.0
    abs_v = abs(v)

    if abs_v >= 1_000_000_000:
        num = v / 1_000_000_000
        s = f"{num:.1f}".rstrip("0").rstrip(".")
        return f"${s}B"

    if abs_v >= 1_000_000:
        num = v / 1_000_000
        s = f"{num:.1f}".rstrip("0").rstrip(".")
        return f"${s}M"

    if abs_v >= 1_000:
        num = v / 1_000
        s = f"{num:.1f}".rstrip("0").rstrip(".")
        return f"${s}K"

    s = f"{v:.0f}"
    return f"${s}"


def get_league_specific_tag_id(league):
    league = league.strip().lower()
    tag_id = LEAGUE_TAG_IDS.get(league)
    if not tag_id:
        raise ValueError(f"Unsupported league. Use one of: {', '.join(sorted(LEAGUE_TAG_IDS))}")
    return tag_id


def fetch_events_for_tag(tag_id, limit=500, max_pages=20):
    events = []
    offset = 0

    for _ in range(max_pages):
        params = {
            "tag_id": tag_id,
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
            "order": "volume",
            "ascending": "false",
            "_ts": int(time.time() * 1000),
        }
        resp = SESSION.get(f"{GAMMA_BASE}/events", params=params, timeout=30)
        resp.raise_for_status()
        page = resp.json()

        if not page:
            break

        events.extend(page)

        if len(page) < limit:
            break

        offset += limit

    return events


def dedupe_events(events):
    out = {}
    for e in events:
        eid = str(e.get("id"))
        out[eid] = e
    return list(out.values())


def is_game_title(title):
    t = (title or "").strip().lower()
    return " vs. " in t or " vs " in t


def split_teams_from_title(title):
    title = (title or "").strip()
    if " vs. " in title:
        a, b = title.split(" vs. ", 1)
        return a.strip(), b.strip()
    if " vs " in title:
        a, b = title.split(" vs ", 1)
        return a.strip(), b.strip()
    return None, None


def normalize_team_name(name):
    return re.sub(r"\s+", " ", str(name).strip().lower())


def same_teams(title, outcomes):
    team_a, team_b = split_teams_from_title(title)
    if not team_a or not team_b or len(outcomes) < 2:
        return False

    title_set = {normalize_team_name(team_a), normalize_team_name(team_b)}
    outcome_set = {normalize_team_name(outcomes[0]), normalize_team_name(outcomes[1])}
    return title_set == outcome_set


def is_moneyline_market(event_title, question, outcomes):
    q = (question or "").strip()
    outs = parse_json_array(outcomes)

    if q != event_title:
        return False

    if len(outs) != 2:
        return False

    if not same_teams(event_title, outs):
        return False

    low_outs = [str(x).strip().lower() for x in outs]
    if set(low_outs) == {"yes", "no"}:
        return False
    if any(o in {"over", "under"} for o in low_outs):
        return False

    return True


def is_same_game_side_market(event_title, question, outcomes):
    q = (question or "").strip()
    outs = parse_json_array(outcomes)

    if len(outs) != 2:
        return False

    if not same_teams(event_title, outs):
        return False

    low_q = q.lower()
    low_outs = [str(x).strip().lower() for x in outs]

    if set(low_outs) == {"yes", "no"}:
        return False
    if any(o in {"over", "under"} for o in low_outs):
        return False

    if q == event_title:
        return True
    if low_q.startswith("spread:"):
        return True

    return False


def infer_game_datetime(event, moneyline_market):
    title = (event.get("title") or "").strip()
    markets = event.get("markets") or []

    sibling_times = []

    for m in markets:
        if m is moneyline_market:
            continue

        if m.get("closed") or not m.get("active") or not m.get("enableOrderBook"):
            continue

        q = (m.get("question") or "").strip()
        outcomes = parse_json_array(m.get("outcomes"))

        if not is_same_game_side_market(title, q, outcomes):
            continue

        dt = parse_dt(m.get("endDate")) or parse_dt(m.get("startDate"))
        if dt:
            sibling_times.append(dt)

    if sibling_times:
        sibling_times.sort()
        return sibling_times[0]

    dt = parse_dt(moneyline_market.get("endDate")) or parse_dt(moneyline_market.get("startDate"))
    if dt:
        return dt

    return parse_dt(event.get("endDate")) or parse_dt(event.get("startDate"))


def parse_clob_token_ids(market):
    token_ids = parse_json_array(market.get("clobTokenIds"))
    return [str(x) for x in token_ids if str(x).strip()]


def get_book(token_id):
    resp = SESSION.get(
        f"{CLOB_BASE}/book",
        params={"token_id": token_id},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def best_price_from_book_side(levels, pick="bid"):
    vals = []
    for lvl in levels or []:
        if isinstance(lvl, dict):
            p = safe_float(lvl.get("price"))
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 1:
            p = safe_float(lvl[0])
        else:
            p = None
        if p is not None:
            vals.append(p)

    if not vals:
        return None

    if pick == "bid":
        return max(vals)
    return min(vals)


def live_price_for_token(token_id):
    try:
        book = get_book(token_id)
    except Exception:
        return None

    bids = book.get("bids") or []
    asks = book.get("asks") or []

    best_bid = best_price_from_book_side(bids, pick="bid")
    best_ask = best_price_from_book_side(asks, pick="ask")

    if best_bid is not None and best_ask is not None:
        return round((best_bid + best_ask) / 2.0, 6)

    last_trade = safe_float(book.get("lastTradePrice"))
    if last_trade is not None:
        return round(last_trade, 6)

    if best_bid is not None:
        return round(best_bid, 6)

    if best_ask is not None:
        return round(best_ask, 6)

    return None


def live_prices_for_market(market):
    outcomes = parse_json_array(market.get("outcomes"))
    token_ids = parse_clob_token_ids(market)

    if len(outcomes) != len(token_ids):
        return None

    prices = []
    for token_id in token_ids:
        p = live_price_for_token(token_id)
        if p is None:
            return None
        prices.append(p)

    return prices


def choose_moneyline_market(event, now, end_window, live_grace_hours=8):
    title = (event.get("title") or "").strip()
    markets = event.get("markets") or []
    start_floor = now - timedelta(hours=live_grace_hours)

    candidates = []

    for m in markets:
        if m.get("closed") or not m.get("active") or not m.get("enableOrderBook"):
            continue

        q = (m.get("question") or "").strip()
        outcomes = parse_json_array(m.get("outcomes"))

        if len(outcomes) != 2:
            continue

        if not is_moneyline_market(title, q, outcomes):
            continue

        if len(parse_clob_token_ids(m)) != 2:
            continue

        game_dt = infer_game_datetime(event, m)
        if not game_dt:
            continue

        if not (start_floor <= game_dt <= end_window):
            continue

        volume = safe_float(m.get("volume"), 0.0) or 0.0

        candidates.append({
            "volume": volume,
            "game_dt": game_dt,
            "market": m,
        })

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x["game_dt"], -x["volume"]))
    return candidates[0]


def is_epl_home_win_question(question, team_a):
    q = (question or "").strip().lower()
    a = normalize_team_name(team_a)
    return q.startswith("will ") and a in q and " win on " in q


def is_epl_away_win_question(question, team_b):
    q = (question or "").strip().lower()
    b = normalize_team_name(team_b)
    return q.startswith("will ") and b in q and " win on " in q


def is_epl_draw_question(question, team_a, team_b):
    q = (question or "").strip().lower()
    a = normalize_team_name(team_a)
    b = normalize_team_name(team_b)
    return a in q and b in q and " end in a draw" in q


def yes_live_price_from_market(market):
    outcomes = parse_json_array(market.get("outcomes"))
    token_ids = parse_clob_token_ids(market)

    if len(outcomes) != 2 or len(token_ids) != 2:
        return None

    for i, outcome in enumerate(outcomes):
        if str(outcome).strip().lower() == "yes":
            return live_price_for_token(token_ids[i])

    return None


def choose_epl_match_markets(event, now, end_window, live_grace_hours=8):
    title = (event.get("title") or "").strip()
    team_a, team_b = split_teams_from_title(title)
    if not team_a or not team_b:
        return None

    start_floor = now - timedelta(hours=live_grace_hours)
    event_dt = parse_dt(event.get("endDate")) or parse_dt(event.get("startDate"))
    if not event_dt:
        return None

    if not (start_floor <= event_dt <= end_window):
        return None

    home_market = None
    draw_market = None
    away_market = None

    for m in (event.get("markets") or []):
        if m.get("closed") or not m.get("active") or not m.get("enableOrderBook"):
            continue

        outcomes = parse_json_array(m.get("outcomes"))
        if [str(x).strip().lower() for x in outcomes] != ["yes", "no"]:
            continue

        if len(parse_clob_token_ids(m)) != 2:
            continue

        q = (m.get("question") or "").strip()

        if is_epl_home_win_question(q, team_a):
            home_market = m
        elif is_epl_draw_question(q, team_a, team_b):
            draw_market = m
        elif is_epl_away_win_question(q, team_b):
            away_market = m

    if not (home_market and draw_market and away_market):
        return None

    return {
        "game_dt": event_dt,
        "home_market": home_market,
        "draw_market": draw_market,
        "away_market": away_market,
    }


def extract_rows_standard(events, hours, live_grace_hours=8, debug=False, log=None, tz_name="Europe/Berlin"):
    if log is None:
        log = lambda *args, **kwargs: None

    now = datetime.now(timezone.utc)
    end_window = now + timedelta(hours=hours)

    rows = []

    total_events = len(events)
    title_games = 0
    chosen_markets = 0
    priced_markets = 0

    for event in events:
        title = (event.get("title") or "").strip()

        if not is_game_title(title):
            continue
        title_games += 1

        chosen = choose_moneyline_market(
            event,
            now=now,
            end_window=end_window,
            live_grace_hours=live_grace_hours,
        )
        if not chosen:
            continue
        chosen_markets += 1

        market = chosen["market"]
        game_dt = chosen["game_dt"]

        live_prices = live_prices_for_market(market)
        if not live_prices or len(live_prices) != 2:
            continue
        priced_markets += 1

        price_a = safe_float(live_prices[0])
        price_b = safe_float(live_prices[1])
        if price_a is None or price_b is None:
            continue

        date_str, time_str = dt_to_date_time(game_dt, tz_name=tz_name)

        rows.append({
            "Name": title,
            "Date": date_str,
            "Time": time_str,
            "Team A": price_a,
            "Team B": price_b,
            "Volume": format_dollar_compact(market.get("volume")),
            "_sort": game_dt.isoformat(),
        })

    rows.sort(key=lambda r: r["_sort"])
    for r in rows:
        r.pop("_sort", None)

    if debug:
        log(f"league_events={total_events}")
        log(f"title_games={title_games}")
        log(f"chosen_markets={chosen_markets}")
        log(f"priced_markets={priced_markets}")
        log(f"final_rows={len(rows)}")
        for row in rows[:10]:
            log(f"SAMPLE_ROW: {row}")

    return rows


def extract_rows_epl(events, hours, live_grace_hours=8, debug=False, log=None, tz_name="Europe/Berlin"):
    if log is None:
        log = lambda *args, **kwargs: None

    now = datetime.now(timezone.utc)
    end_window = now + timedelta(hours=hours)

    rows = []

    total_events = len(events)
    title_games = 0
    chosen_matches = 0
    priced_matches = 0

    for event in events:
        title = (event.get("title") or "").strip()

        if not is_game_title(title):
            continue
        if title.endswith(" - More Markets"):
            continue
        title_games += 1

        chosen = choose_epl_match_markets(
            event,
            now=now,
            end_window=end_window,
            live_grace_hours=live_grace_hours,
        )
        if not chosen:
            continue
        chosen_matches += 1

        game_dt = chosen["game_dt"]
        home_price = yes_live_price_from_market(chosen["home_market"])
        draw_price = yes_live_price_from_market(chosen["draw_market"])
        away_price = yes_live_price_from_market(chosen["away_market"])

        if home_price is None or draw_price is None or away_price is None:
            continue
        priced_matches += 1

        volume = (
            (safe_float(chosen["home_market"].get("volume"), 0.0) or 0.0) +
            (safe_float(chosen["draw_market"].get("volume"), 0.0) or 0.0) +
            (safe_float(chosen["away_market"].get("volume"), 0.0) or 0.0)
        )

        date_str, time_str = dt_to_date_time(game_dt, tz_name=tz_name)

        rows.append({
            "Name": title,
            "Date": date_str,
            "Time": time_str,
            "Team A": home_price,
            "Draw": draw_price,
            "Team B": away_price,
            "Volume": format_dollar_compact(volume),
            "_sort": game_dt.isoformat(),
        })

    rows.sort(key=lambda r: r["_sort"])
    for r in rows:
        r.pop("_sort", None)

    if debug:
        log(f"league_events={total_events}")
        log(f"title_games={title_games}")
        log(f"chosen_matches={chosen_matches}")
        log(f"priced_matches={priced_matches}")
        log(f"final_rows={len(rows)}")
        for row in rows[:10]:
            log(f"SAMPLE_ROW: {row}")

    return rows


def write_csv(filename, rows, league):
    league_key = league.strip().lower()

    if league_key in SOCCER_3WAY_LEAGUES:
        fieldnames = ["Name", "Date", "Time", "Team A", "Draw", "Team B", "Volume"]
    else:
        fieldnames = ["Name", "Date", "Time", "Team A", "Team B", "Volume"]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


SOCCER_3WAY_LEAGUES = {
    "epl",
    "laliga", "bundesliga", "ligue1", "serie_a",
    "champions_league", "mls"
}

def export_league_csv(
    league,
    hours=24,
    limit=500,
    max_pages=20,
    live_grace_hours=8,
    debug=False,
    log=None,
    tz_name="Europe/Berlin",
):
    if log is None:
        log = lambda *args, **kwargs: None

    league_key = league.strip().lower()
    tag_id = get_league_specific_tag_id(league_key)
    if debug:
        log(f"league_tag_id={tag_id}")

    events = fetch_events_for_tag(tag_id, limit=limit, max_pages=max_pages)
    events = dedupe_events(events)

    if league_key in SOCCER_3WAY_LEAGUES:
        rows = extract_rows_epl(
            events,
            hours=hours,
            live_grace_hours=live_grace_hours,
            debug=debug,
            log=log,
            tz_name=tz_name,
        )
    else:
        rows = extract_rows_standard(
            events,
            hours=hours,
            live_grace_hours=live_grace_hours,
            debug=debug,
            log=log,
            tz_name=tz_name,
        )

    filename = f"./live_data/{sanitize_filename(league_key)}.csv"
    write_csv(filename, rows, league_key)
    return filename, rows


def main():
    args = parse_args()

    if args.once:
        fname, rows = export_league_csv(
            args.league,
            hours=args.hours,
            limit=args.limit,
            max_pages=args.max_pages,
            live_grace_hours=args.live_grace_hours,
            debug=args.debug,
            log=print,
            tz_name=args.timezone,
        )
        print(f"Wrote {len(rows)} rows to {fname}")
        return

    while True:
        try:
            fname, rows = export_league_csv(
                args.league,
                hours=args.hours,
                limit=args.limit,
                max_pages=args.max_pages,
                live_grace_hours=args.live_grace_hours,
                debug=args.debug,
                log=print,
                tz_name=args.timezone,
            )
            print(f"Updated {fname} with {len(rows)} rows at {datetime.now().strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(args.interval)
