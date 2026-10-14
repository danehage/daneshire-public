"""Decision-log scorer (#119): pure scoring rules, no DB, no FMP.

The orchestrator (``score_due_decisions``) is exercised end-to-end against
Postgres in ``tests/test_routes/test_internal_decisions.py``.
"""

from datetime import date, datetime, timedelta

import pytest
import pytz

from app.schemas.market import HistoryBar
from app.services.decision_scoring import (
    Bar,
    DecisionInput,
    bars_from_history,
    plan_outcomes,
    score_horizon,
    sessions_after,
)

ET = pytz.timezone("America/New_York")


def et(y, m, d, hh=11, mm=0):
    return ET.localize(datetime(y, m, d, hh, mm))


def weekdays(start: date, n: int, skip: set[date] = frozenset()) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5 and d not in skip:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars(dates, closes) -> list[Bar]:
    if callable(closes):
        return [Bar(d, closes(i)) for i, d in enumerate(dates)]
    return [Bar(d, c) for d, c in zip(dates, closes)]


# Mon 2026-03-02 onward. Prior Friday 02-27 is included as a pre-decision bar.
PRE = date(2026, 2, 27)
DATES = [PRE] + weekdays(date(2026, 3, 2), 160)
SPY_BARS = bars(DATES, lambda i: 500.0 + i)          # SPY 500, 501, ...
TICKER_BARS = bars(DATES, lambda i: 100.0 + 2 * i)   # ticker 100, 102, ...

DECIDED_INTRADAY = et(2026, 3, 2, 11, 0)   # Monday, mid-session
DECISION = DecisionInput(
    created_at=DECIDED_INTRADAY, price_at_decision=101.0, spy_at_decision=500.5
)


# ---------------------------------------------------------------------------
# Session counting on SPY bars
# ---------------------------------------------------------------------------


def test_intraday_decision_counts_that_days_close_as_session_one():
    after = sessions_after(DECIDED_INTRADAY, SPY_BARS)
    assert after[0].date == date(2026, 3, 2)


def test_after_close_decision_starts_the_next_day():
    after = sessions_after(et(2026, 3, 2, 16, 30), SPY_BARS)
    assert after[0].date == date(2026, 3, 3)


def test_close_at_exactly_decision_time_is_not_after_it():
    after = sessions_after(et(2026, 3, 2, 16, 0), SPY_BARS)
    assert after[0].date == date(2026, 3, 3)


def test_weekend_decision_starts_monday():
    after = sessions_after(et(2026, 3, 7, 10, 0), SPY_BARS)  # Saturday
    assert after[0].date == date(2026, 3, 9)


def test_holidays_come_from_spys_own_bars():
    """A day SPY didn't trade isn't a session, so the exit shifts by one."""
    good_friday = date(2026, 4, 3)
    with_holiday = [PRE] + weekdays(date(2026, 3, 2), 160, skip={good_friday})
    spy = bars(with_holiday, lambda i: 500.0 + i)
    ticker = bars(with_holiday, lambda i: 100.0 + i)

    draft = score_horizon(DECISION, 21, spy, ticker)
    plain = score_horizon(DECISION, 21, SPY_BARS, TICKER_BARS)

    assert good_friday not in [b.date for b in sessions_after(DECIDED_INTRADAY, spy)]
    assert plain.exit_date == date(2026, 3, 30)
    assert draft.exit_date == date(2026, 3, 30)  # before the holiday: unchanged
    # After the holiday the exit lands one trading day later than it would
    # on a plain weekday count.
    long = score_horizon(DECISION, 63, spy, ticker)
    long_plain = score_horizon(DECISION, 63, SPY_BARS, TICKER_BARS)
    assert long.exit_date == sessions_after(DECIDED_INTRADAY, spy)[62].date
    plain_dates = [b.date for b in SPY_BARS]
    assert long.exit_date == plain_dates[plain_dates.index(long_plain.exit_date) + 1]


# ---------------------------------------------------------------------------
# The three horizons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("horizon", [21, 63, 126])
def test_each_horizon_exits_at_the_nth_spy_close(horizon):
    draft = score_horizon(DECISION, horizon, SPY_BARS, TICKER_BARS)
    after = sessions_after(DECIDED_INTRADAY, SPY_BARS)
    exit_bar = after[horizon - 1]

    assert draft.status == "scored"
    assert draft.exit_date == exit_bar.date
    assert draft.spy_exit_price == exit_bar.close
    # Bar index i = horizon (index 0 is the pre-decision Friday).
    assert draft.exit_price == 100.0 + 2 * horizon
    assert draft.ticker_return == pytest.approx((100.0 + 2 * horizon) / 101.0 - 1)
    assert draft.spy_return == pytest.approx((500.0 + horizon) / 500.5 - 1)
    assert draft.excess_return == pytest.approx(draft.ticker_return - draft.spy_return)


def test_plan_outcomes_returns_all_three_when_all_due():
    drafts = plan_outcomes(DECISION, set(), SPY_BARS, TICKER_BARS)
    assert [d.horizon_sessions for d in drafts] == [21, 63, 126]


def test_not_due_until_the_nth_session_has_closed():
    after = sessions_after(DECIDED_INTRADAY, SPY_BARS)
    spy = [b for b in SPY_BARS if b.date < after[20].date]  # only 20 sessions
    assert score_horizon(DECISION, 21, spy, TICKER_BARS) is None

    spy = [b for b in SPY_BARS if b.date <= after[20].date]  # exactly 21
    assert score_horizon(DECISION, 21, spy, TICKER_BARS).exit_date == after[20].date
    assert [d.horizon_sessions for d in plan_outcomes(DECISION, set(), spy, TICKER_BARS)] == [21]


def test_passes_are_not_sign_flipped():
    """A pass on a stock that beat SPY shows positive excess: a missed winner."""
    draft = score_horizon(DECISION, 21, SPY_BARS, TICKER_BARS)
    assert draft.excess_return > 0  # same maths whatever the call was


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_already_scored_horizons_are_skipped():
    drafts = plan_outcomes(DECISION, {21}, SPY_BARS, TICKER_BARS)
    assert [d.horizon_sessions for d in drafts] == [63, 126]
    assert plan_outcomes(DECISION, {21, 63, 126}, SPY_BARS, TICKER_BARS) == []


def test_scoring_is_deterministic_across_runs():
    assert plan_outcomes(DECISION, set(), SPY_BARS, TICKER_BARS) == plan_outcomes(
        DECISION, set(), SPY_BARS, TICKER_BARS
    )


# ---------------------------------------------------------------------------
# Truncated
# ---------------------------------------------------------------------------


def _ticker_until(session_index: int) -> list[Bar]:
    """Ticker bars through session N (1-based), then nothing (delisted)."""
    return TICKER_BARS[: session_index + 1]  # +1 for the pre-decision bar


def test_data_ending_before_exit_is_truncated_at_the_last_close():
    ticker = _ticker_until(10)
    last = ticker[-1]
    draft = score_horizon(DECISION, 21, SPY_BARS, ticker)

    assert draft.status == "truncated"
    assert draft.exit_date == last.date
    assert draft.exit_price == last.close
    spy_same_day = next(b for b in SPY_BARS if b.date == last.date)
    assert draft.spy_exit_price == spy_same_day.close  # same shortened window
    assert draft.ticker_return == pytest.approx(last.close / 101.0 - 1)
    assert draft.spy_return == pytest.approx(spy_same_day.close / 500.5 - 1)


def test_truncation_waits_until_spy_is_more_than_five_sessions_past():
    after = sessions_after(DECIDED_INTRADAY, SPY_BARS)
    ticker = _ticker_until(18)
    # SPY through session 23: only 5 sessions past the ticker's last bar.
    spy = [b for b in SPY_BARS if b.date <= after[22].date]
    assert score_horizon(DECISION, 21, spy, ticker) is None
    # One more SPY session: 6 past, so it's truncated.
    spy = [b for b in SPY_BARS if b.date <= after[23].date]
    assert score_horizon(DECISION, 21, spy, ticker).status == "truncated"


def test_all_horizons_truncate_at_the_same_last_close():
    ticker = _ticker_until(30)
    drafts = plan_outcomes(DECISION, set(), SPY_BARS, ticker)
    assert [d.status for d in drafts] == ["scored", "truncated", "truncated"]
    assert drafts[1].exit_date == drafts[2].exit_date == ticker[-1].date


def test_halted_exit_day_uses_the_last_close_before_it():
    after = sessions_after(DECIDED_INTRADAY, SPY_BARS)
    exit_day = after[20].date
    ticker = [b for b in TICKER_BARS if b.date != exit_day]
    draft = score_horizon(DECISION, 21, SPY_BARS, ticker)
    assert draft.status == "scored"
    assert draft.exit_date == after[19].date
    assert draft.spy_exit_price == after[20].close


# ---------------------------------------------------------------------------
# No data
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ticker_bars", [None, [], [Bar(PRE, 99.0)]])
def test_no_data_only_after_ten_sessions_past_due(ticker_bars):
    after = sessions_after(DECIDED_INTRADAY, SPY_BARS)
    spy = [b for b in SPY_BARS if b.date <= after[29].date]  # 9 past due
    assert score_horizon(DECISION, 21, spy, ticker_bars) is None

    spy = [b for b in SPY_BARS if b.date <= after[30].date]  # 10 past due
    draft = score_horizon(DECISION, 21, spy, ticker_bars)
    assert draft.status == "no_data"
    assert draft.exit_date is None and draft.excess_return is None
    # Longer horizons aren't due yet, so they stay pending.
    assert [d.horizon_sessions for d in plan_outcomes(DECISION, set(), spy, ticker_bars)] == [21]


# ---------------------------------------------------------------------------
# No look-ahead
# ---------------------------------------------------------------------------


def test_anchor_is_the_decision_quote_not_a_later_close():
    """Session 1's close (102) is after the call; the anchor stays at 101."""
    draft = score_horizon(DECISION, 21, SPY_BARS, TICKER_BARS)
    assert draft.ticker_return == pytest.approx(draft.exit_price / 101.0 - 1)
    assert draft.spy_return == pytest.approx(draft.spy_exit_price / 500.5 - 1)


def test_later_bars_never_change_an_exit():
    after = sessions_after(DECIDED_INTRADAY, SPY_BARS)
    short = [b for b in SPY_BARS if b.date <= after[20].date]
    assert score_horizon(DECISION, 21, short, TICKER_BARS) == score_horizon(
        DECISION, 21, SPY_BARS, TICKER_BARS
    )


def _raw(dates_closes):
    return [HistoryBar(date=d.isoformat(), close=c) for d, c in dates_closes]


def test_future_dated_bars_are_dropped():
    now = et(2026, 3, 10, 18, 30)
    raw = _raw([(date(2026, 3, 9), 1.0), (date(2026, 3, 10), 2.0), (date(2026, 3, 11), 3.0)])
    assert [b.date for b in bars_from_history(raw, now)] == [date(2026, 3, 9), date(2026, 3, 10)]


def test_todays_bar_is_ignored_until_it_settles():
    raw = _raw([(date(2026, 3, 9), 1.0), (date(2026, 3, 10), 2.0)])
    before = bars_from_history(raw, et(2026, 3, 10, 16, 30))
    after = bars_from_history(raw, et(2026, 3, 10, 17, 0))
    assert [b.date for b in before] == [date(2026, 3, 9)]
    assert [b.date for b in after] == [date(2026, 3, 9), date(2026, 3, 10)]


def test_exit_day_not_scored_from_a_partial_bar():
    """Run during the exit session: the exit bar isn't settled, so no score."""
    after = sessions_after(DECIDED_INTRADAY, SPY_BARS)
    exit_day = after[20].date
    raw_spy = [HistoryBar(date=b.date.isoformat(), close=b.close) for b in SPY_BARS]
    raw_tkr = [HistoryBar(date=b.date.isoformat(), close=b.close) for b in TICKER_BARS]

    mid_session = et(exit_day.year, exit_day.month, exit_day.day, 12, 0)
    spy = bars_from_history(raw_spy, mid_session)
    tkr = bars_from_history(raw_tkr, mid_session)
    assert score_horizon(DECISION, 21, spy, tkr) is None

    evening = et(exit_day.year, exit_day.month, exit_day.day, 18, 30)
    spy = bars_from_history(raw_spy, evening)
    tkr = bars_from_history(raw_tkr, evening)
    assert score_horizon(DECISION, 21, spy, tkr).exit_date == exit_day


# ---------------------------------------------------------------------------
# Input handling + split guard
# ---------------------------------------------------------------------------


def test_bars_from_history_accepts_dicts_and_skips_bad_rows():
    now = et(2026, 3, 20, 18, 30)
    raw = [
        {"date": "2026-03-10", "close": 5.0},
        {"date": "2026-03-09", "close": 4.0},   # out of order
        {"date": "2026-03-11", "close": 0},     # no price
        {"date": "garbage", "close": 1.0},
        {"date": "2026-03-12", "close": None},
    ]
    assert bars_from_history(raw, now) == [Bar(date(2026, 3, 9), 4.0), Bar(date(2026, 3, 10), 5.0)]


def test_split_after_the_decision_anchors_on_the_prior_adjusted_close():
    """FMP's series is split-adjusted; the stored quote is not. A 2:1 split
    halves every pre-split close, so the 200 quote is compared with a
    100 close and both legs anchor on that close instead."""
    split_quote = DecisionInput(
        created_at=DECIDED_INTRADAY, price_at_decision=200.0, spy_at_decision=500.5
    )
    draft = score_horizon(split_quote, 21, SPY_BARS, TICKER_BARS)
    assert draft.ticker_return == pytest.approx(draft.exit_price / TICKER_BARS[0].close - 1)
    assert draft.spy_return == pytest.approx(draft.spy_exit_price / SPY_BARS[0].close - 1)


def test_normal_day_move_does_not_trigger_the_split_guard():
    big_move = DecisionInput(
        created_at=DECIDED_INTRADAY, price_at_decision=130.0, spy_at_decision=500.5
    )
    draft = score_horizon(big_move, 21, SPY_BARS, TICKER_BARS)
    assert draft.ticker_return == pytest.approx(draft.exit_price / 130.0 - 1)
