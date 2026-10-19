"""Immutable pricing snapshots preserve paper consumption and live admission."""
from dataclasses import asdict, replace
from decimal import Decimal as D, localcontext
from fractions import Fraction as F
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from opportunity_core import BookSnapshot
import opportunity_core as core
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
        # The rejected edge never reaches pair preparation after the early bound.
        assert {row['name'] for row in sampled} == {'pricing.entry'}
        assert all(row['sample_every'] == 127 and row['venue'] == row['outcome'] == '' for row in sampled)


def test_static_validation_is_object_local_and_never_caches_freshness(monkeypatch):
    calls = []
    validate = core._validate_book_structure
    def observed(book):
        calls.append(id(book))
        return validate(book)
    monkeypatch.setattr(core, '_validate_book_structure', observed)
    book, settings = depth_book(), core.ScanSettings()
    evidence = asdict(book)
    for _ in range(10):
        assert core._book_valid(book, settings, NOW)
    assert calls == [id(book)] and asdict(book) == evidence
    assert not core._book_valid(book, settings, NOW + 3 * SECOND)
    assert not core._book_valid(book, replace(settings, max_receive_age_ms=0), NOW + 1)
    assert not core._book_valid(book, settings, NOW - 1)
    assert calls == [id(book)]
    invalid = replace(book, asks=((D(100), D(-1)),))
    assert not core._book_valid(invalid, settings, NOW)
    assert calls == [id(book), id(invalid)]
    assert not core._book_valid(replace(book, valid=False), settings, NOW)


@pytest.mark.parametrize('change', [
    {'bids': ((D(99), D(1)), (D(100), D(1)))},
    {'asks': ((D(100), D(1)), (D(100), D(1)))},
    {'asks': ((D(100), D('NaN')),)},
    {'asks': ((D(100),),)},
    {'coverage_limit': 1}, {'coverage_limit': True},
    {'ts_event_ns': True}, {'bids': ()},
])
def test_replacement_revalidates_every_structural_constraint(change):
    book = depth_book()
    assert core._book_valid(book, core.ScanSettings(), NOW)
    changed = replace(book, **change)
    for _ in range(3):
        assert not core._book_valid(changed, core.ScanSettings(), NOW)


def test_metadata_validation_reuses_only_the_same_immutable_version(monkeypatch):
    calls = []
    validate = core._validate_execution_metadata
    def observed(metadata):
        calls.append(id(metadata))
        return validate(metadata)
    monkeypatch.setattr(core, '_validate_execution_metadata', observed)
    paper, _ = make_state()
    meta = paper.metadata['BTC.HL']
    evidence = asdict(meta)
    for _ in range(10):
        assert core._metadata_valid(meta)
    assert calls == [id(meta)] and asdict(meta) == evidence
    for changes in ({'size_increment': D(0)}, {'multiplier': D(-1)},
                    {'min_quantity': D(-1)}, {'min_notional': D('NaN')},
                    {'taker_fee_bps': None}, {'fee_source': 'unknown'}):
        assert not core._metadata_valid(replace(meta, **changes))


def test_early_bound_skips_preparation_without_resetting_paper_liquidity(monkeypatch):
    paper, _ = make_state()
    paper.books['BTC.HL'] = depth_book()
    paper.books['BTC.ASTER'] = depth_book('ASTER')
    paper._consume('BTC.HL', 'asks', F(1))
    def forbidden(*args):
        raise AssertionError('impossible spread reached full preparation')
    monkeypatch.setattr(paper, '_pair', forbidden)
    assert paper.entry('BTC.HL', 'BTC.ASTER', NOW) is None
    assert paper._remaining_book('BTC.HL').asks[0][1] == D(1)


def test_full_pricing_path_still_has_all_sampled_stages(monkeypatch):
    paper, _ = make_state()
    set_book(paper, 'HL', '99.99', '100')
    set_book(paper, 'ASTER', '100.5', '100.51')
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    for _ in range(254):
        assert paper.entry('BTC.HL', 'BTC.ASTER', NOW) is not None
    sampled = [r for r in recorder.document()['operations'] if r['category'] == 'sampled_span']
    assert {r['name'] for r in sampled} == {
        'pricing.entry', 'pricing.prepare_pair', 'pricing.remaining_book'}
