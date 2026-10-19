"""Offline deterministic replay and alternating entry-pricing benchmarks.

The baseline directory must contain the previous Python sources. No credentials,
native clients, sockets, or gateways are constructed. Synthetic replay is not a
substitute for an uninterrupted live observation window.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000_000_000_000
VENUES = ('HL', 'LIGHTER', 'ASTER', 'ONDO', 'BACKPACK')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def load_source(source):
    sys.path.insert(0, str(ROOT / 'src'))
    sys.path.insert(0, str(source))
    global core, paper, scan, trade, live, preflight, timing
    import opportunity_core as core
    import opportunity_paper as paper
    import opportunity_scan as scan
    import opportunity_trade as trade
    import opportunity_live as live
    import opportunity_preflight as preflight
    import opportunity_timing as timing


def plan():
    markets = tuple(scan.MarketPlan('BTC', v, 'BTC.' + v, D(1), 'synthetic parity') for v in VENUES)
    return scan.ScanPlan(markets, core.ScanSettings(reserve_bps=D(1)), None, evaluation_interval_ms=100)


def frames():
    result = []
    cases = ('flat', 'small_spread', 'profitable', 'shallow', 'quantity_only', 'deep_price_only',
             'bad_sort', 'zero_size', 'invalid', 'stale', 'receive_stale', 'future', 'skew',
             'missing_fee', 'fee_change', 'step_change', 'multiplier_change', 'min_quantity',
             'min_notional', 'metadata_identity', 'account_stale', 'account_disconnected',
             'account_external_order', 'consumed', 'received_only', 'negative_threshold', 'zero_threshold',
             'edge_below', 'edge_equal', 'edge_above')
    for repeat in range(3):
        for case in cases:
            stamp = NOW + repeat * 1_000_000
            books, metadata = [], []
            for index, venue in enumerate(VENUES):
                depth = 5 if venue == 'HL' else 20
                ask = D(100) + (D(index) * D('.4') if case not in {'flat', 'small_spread'} else
                               D(index) * D('.005'))
                if case.startswith('edge_'):
                    # Exact net edge after a one-bp reserve, with zero fees/slip.
                    ask = D('100') if index == 0 else D('100.32')
                    if index == 1:
                        ask += {'edge_below': D('-0.000000000000000001'),
                                'edge_equal': D(0), 'edge_above': D('0.000000000000000001')}[case]
                size = D('.001') if case == 'shallow' else D(10)
                book = dict(venue=venue, symbol='BTC',
                    bids=[(str(ask - D('.01') - D(n) / 100), str(size)) for n in range(depth)],
                    asks=[(str(ask + D(n) / 100), str(size)) for n in range(depth)],
                    ts_event_ns=stamp, ts_received_ns=stamp, valid=True, coverage_limit=depth)
                meta = dict(instrument_id='BTC.' + venue, size_increment='.01', multiplier='1',
                            taker_fee_bps='0', fee_source='synthetic', min_quantity=None, min_notional=None)
                if index == 0:
                    if case == 'quantity_only':
                        book['asks'][0] = (book['asks'][0][0], '.001')
                    elif case == 'deep_price_only':
                        book['asks'] = [(str(ask + D(n)), '.1') for n in range(depth)]
                    elif case == 'bad_sort':
                        book['asks'] = list(reversed(book['asks']))
                    elif case == 'zero_size':
                        book['asks'][0] = (book['asks'][0][0], '0')
                    elif case == 'invalid':
                        book['valid'] = False
                    elif case == 'stale':
                        book['ts_event_ns'] -= 3_000_000_000
                    elif case == 'receive_stale':
                        book['ts_event_ns'] -= 3_000_000_000
                        book['ts_received_ns'] -= 3_000_000_000
                    elif case == 'future':
                        book['ts_received_ns'] += 1
                    elif case == 'skew':
                        book['ts_event_ns'] -= 600_000_000
                    elif case == 'missing_fee':
                        meta['taker_fee_bps'] = None
                    elif case == 'fee_change':
                        meta['taker_fee_bps'] = '100'
                    elif case == 'step_change':
                        meta['size_increment'] = '3'
                    elif case == 'multiplier_change':
                        meta['multiplier'] = '2'
                    elif case == 'min_quantity':
                        meta['min_quantity'] = '10'
                    elif case == 'min_notional':
                        meta['min_notional'] = '101'
                    elif case == 'metadata_identity':
                        meta['instrument_id'] = 'WRONG.HL'
                books.append(book)
                metadata.append(meta)
            result.append(dict(case=case, now_ns=stamp, books=books, metadata=metadata))
    return result


def install(state, frame):
    for row, raw_meta in zip(frame['books'], frame['metadata']):
        values = {**row, **{side: tuple(tuple(D(x) for x in level) for level in row[side])
                           for side in ('bids', 'asks')}}
        meta = {key: D(value) if value is not None and key not in {'instrument_id', 'fee_source'} else value
                for key, value in raw_meta.items()}
        instrument_id = 'BTC.' + row['venue']
        state.update_metadata(instrument_id, core.MarketMetadata(**meta), frame['now_ns'], evaluate=False)
        state.update_book(instrument_id, core.BookSnapshot(**values), frame['now_ns'], evaluate=False)


def replay(dataset):
    rows = []
    for frame in dataset:
        case, now = frame['case'], frame['now_ns']
        policy = trade.TradePolicy()
        state = paper.PaperTradeState(plan(), policy)
        install(state, frame)
        if case in {'consumed', 'received_only'}:
            state._consume('BTC.HL', 'asks', D('10.25'))
            if case == 'received_only':
                state.books['BTC.HL'] = replace(state.books['BTC.HL'], ts_received_ns=now + 1)
                now += 1
        decisions = [state.entry('BTC.' + buy, 'BTC.' + sell, now)
                     for buy in VENUES for sell in VENUES if buy != sell]
        settings = replace(state.plan.settings, min_entry_edge_bps=D('-1') if case == 'negative_threshold'
                           else D(0) if case == 'zero_threshold' else D(30))
        scans = [core.evaluate_opportunity(state.books['BTC.' + buy], state.metadata['BTC.' + buy],
                 state.books['BTC.' + sell], state.metadata['BTC.' + sell], settings, now)
                 for buy in VENUES for sell in VENUES if buy != sell]
        actual = live.LiveTradeState(plan(), policy, max_pairs=2, max_gross_notional_usd=D(500))
        actual.session_id = 'synthetic'
        install(actual, frame)
        for venue in VENUES:
            instrument_id = 'BTC.' + venue
            actual.register_instrument(instrument_id, D('.01'), D(1), 'USD')
            bad = venue == 'HL'
            actual.on_account(preflight.AccountSnapshot(venue, instrument_id, D(100), D('100.01'),
                now - 6_000_000_000 if bad and case == 'account_stale' else now,
                D(0), ('external',) if bad and case == 'account_external_order' else (),
                connected=not (bad and case == 'account_disconnected'), authoritative=True), now)
        actual.activate(now)
        actual.drain_intents()
        actual.evaluate_symbol('BTC', now)
        intents = [asdict(i) for i in actual.drain_intents()]
        rows.append(dict(case=case, decisions=digest(decisions), scans=digest(scans), live=digest(actual.snapshot()),
                         intents=digest(intents), accepted=sum(d is not None for d in decisions),
                         submits=sum(i['action'] == 'submit' for i in intents)))
    return rows


def benchmark(dataset, enabled, rounds):
    timing._active = timing.TimingRecorder() if enabled else None
    result = {}
    for case in ('flat', 'small_spread', 'profitable'):
        frame = next(f for f in dataset if f['case'] == case)
        state = paper.PaperTradeState(plan(), trade.TradePolicy())
        pairs = [('BTC.' + buy, 'BTC.' + sell) for buy in VENUES for sell in VENUES if buy != sell]
        def run():
            # Include new snapshot construction and its first validation in each round.
            install(state, frame)
            for buy, sell in pairs:
                state.entry(buy, sell, frame['now_ns'])
        for _ in range(10):
            run()
        start = time.perf_counter_ns()
        for _ in range(rounds):
            run()
        result[case] = (time.perf_counter_ns() - start) / rounds / 1e6
    timing._active = None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--timing', action='store_true')
    parser.add_argument('--replay', action='store_true')
    parser.add_argument('--samples', type=int, default=7)
    parser.add_argument('--rounds', type=int, default=100)
    args = parser.parse_args()
    if args.worker:
        load_source(args.worker)
        dataset = json.loads((args.output / 'inputs.json').read_text(encoding='utf-8'))
        result = replay(dataset) if args.replay else benchmark(dataset, args.timing, args.rounds)
        print(json.dumps(result, default=str))
        return
    if args.baseline_dir is None:
        parser.error('--baseline-dir is required')
    args.output.mkdir(parents=True, exist_ok=True)
    dataset = frames()
    (args.output / 'inputs.json').write_text(json.dumps(dataset, indent=2) + '\n', encoding='utf-8')
    sources = dict(baseline=args.baseline_dir.resolve(), candidate=ROOT / 'src')
    def worker(name, *flags):
        command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker', str(sources[name]),
                   '--output', str(args.output), '--rounds', str(args.rounds), *flags]
        return json.loads(subprocess.check_output(command, text=True))
    replayed = {name: worker(name, '--replay') for name in sources}
    (args.output / 'replay.json').write_text(json.dumps(replayed, indent=2) + '\n', encoding='utf-8')
    if replayed['baseline'] != replayed['candidate']:
        raise SystemExit('Replay mismatch; inspect replay.json before benchmarking')
    samples = {mode: {name: [] for name in sources} for mode in ('disabled', 'enabled')}
    for mode in samples:
        for index in range(args.samples):
            for name in (('baseline', 'candidate') if index % 2 == 0 else ('candidate', 'baseline')):
                samples[mode][name].append(worker(name, *(['--timing'] if mode == 'enabled' else [])))
    medians = {mode: {name: {case: statistics.median(s[case] for s in values)
                            for case in values[0]} for name, values in versions.items()}
               for mode, versions in samples.items()}
    result = dict(python=sys.version, inputs_sha256=digest(dataset), replay_frames=len(dataset),
                  replay_equal=True, pricing_decisions=len(dataset) * 20, scan_decisions=len(dataset) * 20,
                  accepted=sum(r['accepted'] for r in replayed['candidate']),
                  submits=sum(r['submits'] for r in replayed['candidate']),
                  medians_ms_per_20_directions_including_publication=medians, samples=samples,
                  sources={name: {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                            for f in path.glob('opportunity_*.py')} for name, path in sources.items()})
    (args.output / 'comparison.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key not in {'sources', 'samples'}}, indent=2))


if __name__ == '__main__':
    main()
