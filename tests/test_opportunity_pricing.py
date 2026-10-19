"""Immutable pricing snapshots preserve paper consumption and live admission."""
from dataclasses import replace
from decimal import Decimal as D, localcontext
from fractions import Fraction as F
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from opportunity_core import BookSnapshot
import opportunity_timing as timing
from test_opportunity_live import state, submissions
from test_opportunity_trade import NOW, SECOND, make_plan, make_state, set_book


def depth_book(venue='HL', stamp=NOW):
    return BookSnapshot(venue, 'BTC',
        ((D('99.99'), D('2.00')), (D('99.98'), D('3.50'))),
        ((D('100'), D('2.00')), (D('100.01'), D('3.50'))),
        stamp, stamp, coverage_limit=2)


def test_untouched_depth_is_shared_then_consumed_liquidity_is_deducted():
    paper, _ = make_state()
    book = depth_book()
    paper.books['BTC.HL'] = book
    assert paper._remaining_book('BTC.HL') is book
    paper._consume('BTC.HL', 'asks', F('2.25'))
    remaining = paper._remaining_book('BTC.HL')
    assert remaining.asks == ((D('100.01'), D('3.25')),)
    assert remaining.bids == book.bids
    assert book.asks == depth_book().asks  # Original evidence stays immutable.
    assert paper._remaining_book('BTC.HL') == remaining  # Reads cannot refill liquidity.
    paper._consume('BTC.HL', 'bids', F('0.75'))
    assert paper._remaining_book('BTC.HL').bids[0][1] == D('1.25')


def test_received_timestamp_alone_does_not_reset_paper_consumption():
    paper, _ = make_state()
    book = depth_book()
    paper.books['BTC.HL'] = book
    paper._consume('BTC.HL', 'asks', F('1.25'))
    paper.books['BTC.HL'] = replace(book, ts_received_ns=NOW + 1)
    assert paper._remaining_book('BTC.HL').asks[0][1] == D('0.75')
    fresh = replace(book, ts_event_ns=NOW + 2, ts_received_ns=NOW + 2)
    paper.books['BTC.HL'] = fresh
    assert paper._remaining_book('BTC.HL') is fresh
    assert paper.consumed['BTC.HL']['asks'] == {}


@pytest.mark.parametrize('change', ['price', 'size'])
def test_changed_levels_reset_consumption_even_at_same_timestamp(change):
    paper, _ = make_state()
    book = depth_book()
    paper.books['BTC.HL'] = book
    paper._consume('BTC.HL', 'asks', F('1.25'))
    asks = ((D('100.001'), D('2.00')), book.asks[1]) if change == 'price' else (
        (D('100'), D('2.50')), book.asks[1])
    fresh = replace(book, asks=asks)
    paper.books['BTC.HL'] = fresh
    assert paper._remaining_book('BTC.HL') is fresh


def test_consumption_uses_native_multiplier_and_exact_decimal_under_small_context():
    paper, _ = make_state()
    paper.books['BTC.HL'] = depth_book()
    paper.metadata['BTC.HL'] = replace(paper.metadata['BTC.HL'], multiplier=D('0.001'))
    with localcontext() as context:
        context.prec = 2
        paper._consume('BTC.HL', 'asks', F('0.00123456789'))
        remaining = paper._remaining_book('BTC.HL')
    assert remaining.asks[0][1] == D('0.76543211')


@pytest.mark.parametrize('invalid', ['stale', 'future', 'invalid', 'skew', 'bad_depth', 'missing_fee'])
def test_shared_snapshot_path_still_refuses_invalid_live_pricing(invalid):
    live, _ = state()
    set_book(live, 'HL', '99.99', '100')
    set_book(live, 'ASTER', '100.4', '100.41')
    now = NOW
    if invalid == 'stale':
        now += 3 * SECOND
    elif invalid == 'future':
        live.books['BTC.HL'] = replace(live.books['BTC.HL'], ts_received_ns=NOW + 1)
    elif invalid == 'invalid':
        live.books['BTC.HL'] = replace(live.books['BTC.HL'], valid=False)
    elif invalid == 'skew':
        live.books['BTC.HL'] = replace(live.books['BTC.HL'], ts_event_ns=NOW - SECOND)
    elif invalid == 'bad_depth':
        live.books['BTC.HL'] = replace(live.books['BTC.HL'], asks=((D(100), D(-1)),))
    else:
        live.metadata['BTC.HL'] = replace(live.metadata['BTC.HL'], taker_fee_bps=None)
    assert live.pricer.entry('BTC.HL', 'BTC.ASTER', now) is None
    live.evaluate_symbol('BTC', now)
    assert not live.positions and not submissions(live)


@pytest.mark.parametrize('timing_enabled', [False, True])
def test_pricing_sampling_keeps_live_net_boundary_and_emits_only_fixed_names(monkeypatch, timing_enabled):
    live, _ = state(make_plan(reserve='1'))
    recorder = timing.TimingRecorder() if timing_enabled else None
    monkeypatch.setattr(timing, '_active', recorder)
    set_book(live, 'HL', '99.99', '100')
    set_book(live, 'ASTER', '100.309999999999999999', '100.32')
    for _ in range(300):
        live.evaluate_symbol('BTC', NOW)
    assert not live.positions and not submissions(live)
    set_book(live, 'ASTER', '100.31', '100.32')
    live.evaluate_symbol('BTC', NOW)
    assert len(submissions(live)) == 2
    if timing_enabled:
        sampled = [row for row in recorder.document()['operations'] if row['category'] == 'sampled_span']
        assert {row['name'] for row in sampled} == {
            'pricing.entry', 'pricing.prepare_pair', 'pricing.remaining_book'}
        assert all(row['sample_every'] == 127 and row['venue'] == row['outcome'] == '' for row in sampled)
