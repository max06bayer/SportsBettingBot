#!/usr/bin/env python3
import csv
import json
import hashlib
from dataclasses import dataclass
from datetime import datetime
import time

import live_data_runner


# =========================================================
# SETTINGS — edit these, then press Run
# =========================================================

START_CASH = 10000.0

BUY_THRESHOLD_MIN = 75.0
BUY_THRESHOLD_MAX = 97.0
SELL_THRESHOLD = 50.0
TAKE_PROFIT_THRESHOLD = 99.0

MIN_VOLUME = 1000.0
BET_SIZE_PCT = 10.0

WATCH_LEAGUES = [
    "mlb", "nba", "epl", "laliga", "bundesliga", "ligue1",
    "serie_a", "champions_league", "mls", "nfl", "nhl"
]

PORTFOLIO_FILE = "./portfolio.csv"
ACTION_LOG_FILE = "./bot_action_log.csv"
STATE_FILE = "./bot_state.json"

INCLUDE_EPL_DRAW = False

# =========================================================


@dataclass
class Position:
    key: str
    event_key: str
    league: str
    game: str
    option: str
    label: str
    shares: float
    entry_price: float
    cost: float
    opened_at: str
    last_price: float


def pct_to_prob(value):
    x = float(value)
    return x / 100.0 if x > 1 else x


BUY_THRESHOLD_MIN_PROB = pct_to_prob(BUY_THRESHOLD_MIN)
BUY_THRESHOLD_MAX_PROB = pct_to_prob(BUY_THRESHOLD_MAX)
SELL_THRESHOLD_PROB = pct_to_prob(SELL_THRESHOLD)
BET_SIZE_PCT_PROB = pct_to_prob(BET_SIZE_PCT)
TAKE_PROFIT_THRESHOLD_PROB = pct_to_prob(TAKE_PROFIT_THRESHOLD)


def validate_settings():
    if not (0 < BUY_THRESHOLD_MIN_PROB <= 1):
        raise ValueError("BUY_THRESHOLD_MIN must be between 0 and 100")
    if not (0 < BUY_THRESHOLD_MAX_PROB <= 1):
        raise ValueError("BUY_THRESHOLD_MAX must be between 0 and 100")
    if BUY_THRESHOLD_MIN_PROB > BUY_THRESHOLD_MAX_PROB:
        raise ValueError("BUY_THRESHOLD_MIN must be <= BUY_THRESHOLD_MAX")
    if not (0 < SELL_THRESHOLD_PROB < BUY_THRESHOLD_MIN_PROB):
        raise ValueError("SELL_THRESHOLD must be lower than BUY_THRESHOLD_MIN")
    if not (0 < TAKE_PROFIT_THRESHOLD_PROB <= 1):
        raise ValueError("TAKE_PROFIT_THRESHOLD must be between 0 and 100")
    if TAKE_PROFIT_THRESHOLD_PROB < BUY_THRESHOLD_MIN_PROB:
        raise ValueError("TAKE_PROFIT_THRESHOLD should usually be >= BUY_THRESHOLD_MIN")
    if not (0 < BET_SIZE_PCT_PROB <= 1):
        raise ValueError("BET_SIZE_PCT must be between 0 and 100")


def parse_probability(value):
    if value is None:
        return None
    s = str(value).strip().replace("%", "")
    if not s:
        return None
    try:
        x = float(s)
    except Exception:
        return None
    if x > 1:
        x = x / 100.0
    if x < 0 or x > 1:
        return None
    return x


def parse_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def parse_compact_dollars(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip().replace("$", "").replace(",", "").upper()
    if not s:
        return 0.0

    mult = 1.0
    if s.endswith("K"):
        mult = 1_000.0
        s = s[:-1]
    elif s.endswith("M"):
        mult = 1_000_000.0
        s = s[:-1]
    elif s.endswith("B"):
        mult = 1_000_000_000.0
        s = s[:-1]

    try:
        return float(s) * mult
    except Exception:
        return 0.0


def format_money(value):
    return f"{float(value):.2f}"


def format_prob(value):
    return f"{value * 100:.1f}%"


def normalize_text(value):
    return " ".join(str(value or "").strip().lower().split())


def split_match_name(name):
    name = (name or "").strip()
    if " vs. " in name:
        return [x.strip() for x in name.split(" vs. ", 1)]
    if " vs " in name:
        return [x.strip() for x in name.split(" vs ", 1)]
    return [None, None]


def canonical_game_token(game):
    a, b = split_match_name(game)
    if a and b:
        teams = sorted([normalize_text(a), normalize_text(b)])
        return " vs ".join(teams)
    return normalize_text(game)


def make_event_key(league, game):
    return f"{normalize_text(league)}|{canonical_game_token(game)}"


def normalize_selection_label(label):
    return normalize_text(label)


def make_position_key(league, game, label):
    return f"{make_event_key(league, game)}|{normalize_selection_label(label)}"


def option_display_name(game, option):
    a, b = split_match_name(game)
    if option == "Team A" and a:
        return a
    if option == "Team B" and b:
        return b
    return option


def canonical_option(raw_col):
    mapping = {
        "Price Buy Team A": "Team A",
        "Price Buy Team B": "Team B",
        "Team A": "Team A",
        "Team B": "Team B",
        "Draw": "Draw",
    }
    return mapping.get(raw_col, raw_col)


def discover_option_columns(row, include_draw=False):
    cols = []
    for raw_col in ["Team A", "Team B", "Draw", "Price Buy Team A", "Price Buy Team B"]:
        if raw_col not in row:
            continue
        opt = canonical_option(raw_col)
        if opt == "Draw" and not include_draw:
            continue
        value = str(row.get(raw_col, "")).strip()
        if not value:
            continue
        pair = (raw_col, opt)
        if pair not in cols:
            cols.append(pair)
    return cols


def load_snapshot():
    snapshot = {}
    warnings = []

    for league in WATCH_LEAGUES:
        path = f"./live_data/{league}.csv"

        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    game = str(row.get("Name", "")).strip()
                    if not game:
                        continue

                    volume = parse_compact_dollars(row.get("Volume"))
                    event_key = make_event_key(league, game)

                    for raw_col, opt in discover_option_columns(row, include_draw=INCLUDE_EPL_DRAW):
                        price = parse_probability(row.get(raw_col))
                        if price is None or price <= 0:
                            continue

                        label = option_display_name(game, opt)
                        key = make_position_key(league, game, label)

                        market = {
                            "key": key,
                            "event_key": event_key,
                            "league": league,
                            "game": game,
                            "option": opt,
                            "label": label,
                            "price": price,
                            "volume": volume,
                        }

                        prev = snapshot.get(key)
                        if prev is None:
                            snapshot[key] = market
                        else:
                            prev_score = (prev["volume"], prev["price"])
                            new_score = (market["volume"], market["price"])
                            if new_score > prev_score:
                                snapshot[key] = market

        except FileNotFoundError:
            warnings.append(f"{league}: file not found -> {path}")
        except Exception as e:
            warnings.append(f"{league}: failed to read {path} -> {e}")

    return snapshot, warnings


def ensure_portfolio_file():
    try:
        with open(PORTFOLIO_FILE, "r", newline="", encoding="utf-8"):
            return
    except FileNotFoundError:
        pass

    with open(PORTFOLIO_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Cash",
            "Total Cash Now",
            "Up/Down",
            "Match",
            "Bet On",
            "Stake",
            "Current Value",
            "Current Probability",
            "Entry Probability",
            "League",
            "Opened At",
            "Shares",
            "Key",
            "Event Key",
            "Option",
        ])
        writer.writerow([
            format_money(START_CASH),
            format_money(START_CASH),
            format_money(0.0),
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ])
        writer.writerow([])


def load_portfolio():
    ensure_portfolio_file()

    cash = START_CASH
    positions = {}

    with open(PORTFOLIO_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        first_data_row_used = False

        for row in reader:
            if not any(str(v).strip() for v in row.values() if v is not None):
                continue

            match = str(row.get("Match", "")).strip()
            key = str(row.get("Key", "")).strip()
            option = str(row.get("Option", "")).strip()
            event_key = str(row.get("Event Key", "")).strip()

            if not first_data_row_used and not match:
                cash = parse_float(row.get("Cash"), START_CASH)
                first_data_row_used = True
                continue

            if not match:
                continue

            league = str(row.get("League", "")).strip().lower()
            bet_on = str(row.get("Bet On", "")).strip()
            opened_at = str(row.get("Opened At", "")).strip()
            shares = parse_float(row.get("Shares"), 0.0)
            entry_price = parse_probability(row.get("Entry Probability"))
            stake = parse_float(row.get("Stake"), 0.0)
            current_prob = parse_probability(row.get("Current Probability"))

            if not option:
                a, b = split_match_name(match)
                if bet_on == a:
                    option = "Team A"
                elif bet_on == b:
                    option = "Team B"
                elif bet_on.lower() == "draw":
                    option = "Draw"
                else:
                    option = bet_on

            if not event_key:
                event_key = make_event_key(league, match)

            if not key:
                key = make_position_key(league, match, bet_on or option)

            pos = Position(
                key=key,
                event_key=event_key,
                league=league,
                game=match,
                option=option,
                label=bet_on if bet_on else option_display_name(match, option),
                shares=shares,
                entry_price=entry_price if entry_price is not None else 0.0,
                cost=stake,
                opened_at=opened_at,
                last_price=current_prob if current_prob is not None else 0.0,
            )
            positions[key] = pos

    return cash, positions


def ensure_action_log_file():
    try:
        with open(ACTION_LOG_FILE, "r", newline="", encoding="utf-8"):
            return
    except FileNotFoundError:
        pass

    with open(ACTION_LOG_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "Timestamp",
                "Run ID",
                "Action",
                "League",
                "Event Key",
                "Key",
                "Match",
                "Bet On",
                "Option",
                "Price",
                "Entry Price",
                "Exit Price",
                "Shares",
                "Stake",
                "Proceeds",
                "PnL",
                "Volume",
                "Cash Before",
                "Cash After",
                "Total Cash Before",
                "Total Cash After",
                "Open Positions Before",
                "Open Positions After",
                "Reason",
                "Note",
            ],
        )
        writer.writeheader()


def append_action_log(rows):
    if not rows:
        return

    ensure_action_log_file()

    with open(ACTION_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "Timestamp",
                "Run ID",
                "Action",
                "League",
                "Event Key",
                "Key",
                "Match",
                "Bet On",
                "Option",
                "Price",
                "Entry Price",
                "Exit Price",
                "Shares",
                "Stake",
                "Proceeds",
                "PnL",
                "Volume",
                "Cash Before",
                "Cash After",
                "Total Cash Before",
                "Total Cash After",
                "Open Positions Before",
                "Open Positions After",
                "Reason",
                "Note",
            ],
            extrasaction="ignore",
        )
        writer.writerows(rows)


def make_log_row(
    run_id,
    action,
    league="",
    event_key="",
    key="",
    match="",
    bet_on="",
    option="",
    price="",
    entry_price="",
    exit_price="",
    shares="",
    stake="",
    proceeds="",
    pnl="",
    volume="",
    cash_before="",
    cash_after="",
    total_cash_before="",
    total_cash_after="",
    open_positions_before="",
    open_positions_after="",
    reason="",
    note="",
):
    return {
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Run ID": run_id,
        "Action": action,
        "League": league,
        "Event Key": event_key,
        "Key": key,
        "Match": match,
        "Bet On": bet_on,
        "Option": option,
        "Price": price,
        "Entry Price": entry_price,
        "Exit Price": exit_price,
        "Shares": shares,
        "Stake": stake,
        "Proceeds": proceeds,
        "PnL": pnl,
        "Volume": volume,
        "Cash Before": cash_before,
        "Cash After": cash_after,
        "Total Cash Before": total_cash_before,
        "Total Cash After": total_cash_after,
        "Open Positions Before": open_positions_before,
        "Open Positions After": open_positions_after,
        "Reason": reason,
        "Note": note,
    }


def process_sells(positions, snapshot, cash, run_id):
    trades = []
    log_rows = []

    for key in list(positions.keys()):
        pos = positions[key]
        market = snapshot.get(key)

        if not market:
            continue

        current_price = market["price"]
        pos.last_price = current_price

        should_stop_loss_sell = current_price < SELL_THRESHOLD_PROB
        should_take_profit_sell = current_price >= TAKE_PROFIT_THRESHOLD_PROB

        if should_stop_loss_sell or should_take_profit_sell:
            cash_before = cash
            open_before = len(positions)

            proceeds = pos.shares * current_price
            pnl = proceeds - pos.cost
            cash += proceeds

            reason = "TP>=99%" if should_take_profit_sell else "SL<50%"

            trades.append(
                f"SELL {pos.league.upper()} | {pos.game} | {pos.label} | "
                f"{format_prob(current_price)} | proceeds ${proceeds:.2f} | pnl ${pnl:.2f} | {reason}"
            )

            log_rows.append(
                make_log_row(
                    run_id=run_id,
                    action="SELL",
                    league=pos.league,
                    event_key=pos.event_key,
                    key=pos.key,
                    match=pos.game,
                    bet_on=pos.label,
                    option=pos.option,
                    price=f"{current_price:.6f}",
                    entry_price=f"{pos.entry_price:.6f}",
                    exit_price=f"{current_price:.6f}",
                    shares=f"{pos.shares:.10f}",
                    stake=f"{pos.cost:.2f}",
                    proceeds=f"{proceeds:.2f}",
                    pnl=f"{pnl:.2f}",
                    volume=f"{market.get('volume', 0.0):.2f}",
                    cash_before=f"{cash_before:.2f}",
                    cash_after=f"{cash:.2f}",
                    open_positions_before=str(open_before),
                    open_positions_after=str(open_before - 1),
                    reason=reason,
                    note="sold at current market price used for exit check",
                )
            )

            del positions[key]

    return cash, trades, log_rows


def process_buys(snapshot, positions, cash, run_id):
    trades = []
    log_rows = []
    candidates = []

    held_event_keys = {pos.event_key for pos in positions.values()}

    for key, market in snapshot.items():
        if key in positions:
            continue
        if market["event_key"] in held_event_keys:
            continue
        if market["price"] < BUY_THRESHOLD_MIN_PROB:
            continue
        if market["price"] > BUY_THRESHOLD_MAX_PROB:
            continue
        if market["volume"] < MIN_VOLUME:
            continue
        candidates.append(market)

    candidates.sort(key=lambda m: (-m["volume"], -m["price"], m["league"], m["game"], m["option"]))

    bought_this_run_event_keys = set()

    for market in candidates:
        if cash <= 0:
            break

        if market["event_key"] in held_event_keys:
            continue
        if market["event_key"] in bought_this_run_event_keys:
            continue

        stake = cash * BET_SIZE_PCT_PROB
        if stake < 1:
            break

        price = market["price"]
        shares = stake / price

        cash_before = cash
        open_before = len(positions)

        pos = Position(
            key=market["key"],
            event_key=market["event_key"],
            league=market["league"],
            game=market["game"],
            option=market["option"],
            label=market["label"],
            shares=shares,
            entry_price=price,
            cost=stake,
            opened_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            last_price=price,
        )
        positions[market["key"]] = pos
        cash -= stake
        held_event_keys.add(market["event_key"])
        bought_this_run_event_keys.add(market["event_key"])

        trades.append(
            f"BUY  {pos.league.upper()} | {pos.game} | {pos.label} | "
            f"{format_prob(price)} | stake ${stake:.2f}"
        )

        log_rows.append(
            make_log_row(
                run_id=run_id,
                action="BUY",
                league=pos.league,
                event_key=pos.event_key,
                key=pos.key,
                match=pos.game,
                bet_on=pos.label,
                option=pos.option,
                price=f"{price:.6f}",
                entry_price=f"{price:.6f}",
                shares=f"{shares:.10f}",
                stake=f"{stake:.2f}",
                volume=f"{market.get('volume', 0.0):.2f}",
                cash_before=f"{cash_before:.2f}",
                cash_after=f"{cash:.2f}",
                open_positions_before=str(open_before),
                open_positions_after=str(open_before + 1),
                reason="BUY_THRESHOLD",
                note="one active position per event only",
            )
        )

    return cash, trades, log_rows


def build_position_rows(positions, snapshot):
    rows = []
    market_value = 0.0

    for pos in positions.values():
        market = snapshot.get(pos.key)
        current_prob = market["price"] if market else pos.last_price
        pos.last_price = current_prob

        current_value = pos.shares * current_prob
        market_value += current_value

        rows.append({
            "Cash": "",
            "Total Cash Now": "",
            "Up/Down": "",
            "Match": pos.game,
            "Bet On": pos.label,
            "Stake": format_money(pos.cost),
            "Current Value": format_money(current_value),
            "Current Probability": format_prob(current_prob),
            "Entry Probability": format_prob(pos.entry_price),
            "League": pos.league,
            "Opened At": pos.opened_at,
            "Shares": f"{pos.shares:.10f}",
            "Key": pos.key,
            "Event Key": pos.event_key,
            "Option": pos.option,
        })

    rows.sort(key=lambda r: float(r["Current Value"]), reverse=True)
    return rows, market_value


def save_portfolio(cash, total_cash_now, up_down, position_rows):
    with open(PORTFOLIO_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        writer.writerow([
            "Cash",
            "Total Cash Now",
            "Up/Down",
            "Match",
            "Bet On",
            "Stake",
            "Current Value",
            "Current Probability",
            "Entry Probability",
            "League",
            "Opened At",
            "Shares",
            "Key",
            "Event Key",
            "Option",
        ])

        writer.writerow([
            format_money(cash),
            format_money(total_cash_now),
            format_money(up_down),
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ])

        writer.writerow([])

        for row in position_rows:
            writer.writerow([
                row["Cash"],
                row["Total Cash Now"],
                row["Up/Down"],
                row["Match"],
                row["Bet On"],
                row["Stake"],
                row["Current Value"],
                row["Current Probability"],
                row["Entry Probability"],
                row["League"],
                row["Opened At"],
                row["Shares"],
                row["Key"],
                row["Event Key"],
                row["Option"],
            ])


def print_summary(cash, total_cash_now, up_down, warnings, sell_trades, buy_trades, position_rows):
    print("=" * 120)
    print(f"BOT RUN COMPLETE | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 120)
    print(f"Cash: ${cash:.2f}")
    print(f"Total Cash Now: ${total_cash_now:.2f}")
    print(f"Up/Down: ${up_down:.2f}")
    print(f"Portfolio file: {PORTFOLIO_FILE}")
    print(f"Action log file: {ACTION_LOG_FILE}")
    print()

    if warnings:
        print("WARNINGS")
        print("-" * 120)
        for w in warnings:
            print(w)
        print()

    print("TRADES THIS RUN")
    print("-" * 120)
    if not sell_trades and not buy_trades:
        print("No trades this run.")
    else:
        for line in sell_trades:
            print(line)
        for line in buy_trades:
            print(line)
    print()

    print("POSITIONS")
    print("-" * 120)
    if not position_rows:
        print("No open positions.")
    else:
        for row in position_rows:
            print(
                f"{row['League'].upper()} | {row['Match']} | {row['Bet On']} | "
                f"Stake ${row['Stake']} | Value ${row['Current Value']} | "
                f"Now {row['Current Probability']} | Then {row['Entry Probability']}"
            )


def load_previous_state_hash():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return str(data.get("state_hash", "")).strip()
    except Exception:
        return ""


def save_state_hash(state_hash):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "state_hash": state_hash,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, f, indent=2)


def compute_state_hash(cash, total_cash_now, up_down, positions):
    state = {
        "cash": round(float(cash), 6),
        "total_cash_now": round(float(total_cash_now), 6),
        "up_down": round(float(up_down), 6),
        "positions": sorted(
            [
                {
                    "key": pos.key,
                    "event_key": pos.event_key,
                    "league": pos.league,
                    "game": pos.game,
                    "label": pos.label,
                    "shares": round(float(pos.shares), 10),
                    "entry_price": round(float(pos.entry_price), 6),
                    "last_price": round(float(pos.last_price), 6),
                    "cost": round(float(pos.cost), 6),
                }
                for pos in positions.values()
            ],
            key=lambda x: x["key"],
        ),
    }
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main():
    validate_settings()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_rows = []

    print("Updating live data...")
    live_data_runner.update_data()
    print()

    snapshot, warnings = load_snapshot()
    cash, positions = load_portfolio()

    _, market_value_before = build_position_rows(positions, snapshot)
    total_cash_before = cash + market_value_before

    for w in warnings:
        log_rows.append(
            make_log_row(
                run_id=run_id,
                action="WARNING",
                note=w,
                total_cash_before=f"{total_cash_before:.2f}",
                open_positions_before=str(len(positions)),
            )
        )

    cash, sell_trades, sell_log_rows = process_sells(
        positions=positions,
        snapshot=snapshot,
        cash=cash,
        run_id=run_id,
    )
    log_rows.extend(sell_log_rows)

    cash, buy_trades, buy_log_rows = process_buys(
        snapshot=snapshot,
        positions=positions,
        cash=cash,
        run_id=run_id,
    )
    log_rows.extend(buy_log_rows)

    position_rows, market_value = build_position_rows(positions, snapshot)
    total_cash_now = cash + market_value
    up_down = total_cash_now - START_CASH

    save_portfolio(
        cash=cash,
        total_cash_now=total_cash_now,
        up_down=up_down,
        position_rows=position_rows,
    )

    current_state_hash = compute_state_hash(cash, total_cash_now, up_down, positions)
    previous_state_hash = load_previous_state_hash()

    if current_state_hash != previous_state_hash:
        log_rows.append(
            make_log_row(
                run_id=run_id,
                action="RUN_STATE",
                cash_after=f"{cash:.2f}",
                total_cash_before=f"{total_cash_before:.2f}",
                total_cash_after=f"{total_cash_now:.2f}",
                open_positions_before="",
                open_positions_after=str(len(positions)),
                note=(
                    f"state_changed=1 | snapshot_markets={len(snapshot)} | "
                    f"warnings={len(warnings)} | sells={len(sell_trades)} | buys={len(buy_trades)} | "
                    f"up_down={up_down:.2f}"
                ),
            )
        )
        save_state_hash(current_state_hash)

    append_action_log(log_rows)

    print_summary(
        cash=cash,
        total_cash_now=total_cash_now,
        up_down=up_down,
        warnings=warnings,
        sell_trades=sell_trades,
        buy_trades=buy_trades,
        position_rows=position_rows,
    )


if __name__ == "__main__":
    print("Starting Sports Betting Bot...")
    while True:
        main()
        time.sleep(60*5)