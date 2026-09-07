# Linux deployment (read-only spread watcher)

No keys involved: `src/spread_watch.py` runs data clients only (HL / Lighter /
Aster), no execution client, no signing.

## Ship the tree

From Git Bash on Windows, in the repo root:

```bash
cd /e/Nautilus-Perps
git archive --format=tar HEAD | ssh vultr-worker 'mkdir -p ~/Nautilus-Perps && tar -x -C ~/Nautilus-Perps'
scp -r deploy vultr-worker:~/Nautilus-Perps/
```

`git archive` only ships committed files, so uncommitted `deploy/` files are
copied separately with `scp`. This never copies `.env` (there isn't one
needed), `.venv`, `logs/`, or `reports/stage1/*.csv`.

## Set up the remote box

```bash
ssh vultr-worker
cd ~/Nautilus-Perps
bash deploy/setup_linux.sh                       # no wheel yet
# once a Linux wheel exists:
bash deploy/setup_linux.sh /path/to/nautilus_trader-2.0.0rc4-*.whl
```

This installs uv, Python 3.12, creates `.venv`, installs `python-dotenv`
(+ the wheel if given), and installs pm2 (via `apt-get install nodejs npm`
then `npm install -g pm2`).

**The nautilus_trader wheel must be rebuilt on Linux** (cp312) -- the Windows
wheel does not run here. Build it in WSL Ubuntu 24.04 (`make build-wheel` in the fork; ~17 min on the
Windows box) and copy it over before starting a watcher. The 2026-09-07 build is
`nautilus_trader-2.0.0rc4-cp312-cp312-manylinux_2_39_x86_64.whl` (65 MB, needs
glibc >= 2.39).

## Run with pm2

```bash
# optional: cap the run length, otherwise it defaults to --minutes 30
export WATCH_UNTIL=2026-09-08T20:05:00Z

pm2 start deploy/ecosystem.config.js --only stocks
pm2 start deploy/ecosystem.config.js --only crypto
pm2 logs stocks
pm2 stop stocks
pm2 delete stocks        # or: pm2 delete all
```

Both apps have `autorestart: false` / `max_restarts: 0` -- the watcher exits
0 on purpose at its deadline, so pm2 must not relaunch it.

pm2 snapshots the environment at the first `pm2 start`. To run again with a
new `WATCH_UNTIL`, use `pm2 delete <app>` then `pm2 start ...` (or
`pm2 restart <app> --update-env`); a plain `pm2 restart` keeps the old deadline.

## Where things land

- CSVs: `reports/stage1/` (repo-relative, same layout as local runs)
- pm2 stdout/stderr: `logs/pm2-stocks.out.log` / `.err.log`,
  `logs/pm2-crypto.out.log` / `.err.log` (timestamped, `time: true`)

## Analyse a finished run

`src/analysis/opportunities.py` is offline and stdlib-only: copy the CSVs back
(or run it on the box) and point it at the run stamp.

```bash
.venv/Scripts/python.exe src/analysis/opportunities.py \
    --dir reports/stage1 --stamp 20260907T094928Z \
    [--symbols SOL,HYPE] [--gap-s 2] [--hold-s 30] [--min-usd 1000] \
    [--from 2026-09-07T13:30:00Z --to 2026-09-07T20:00:00Z] \
    [--md reports/stage1/opps.md]
```

Symbols are discovered from the file names of that stamp. Per symbol and per
direction it prints: the gross-bps distribution against the fee+reserve
threshold, hits merged into deduplicated episodes, top-of-book and depth-bucket
capacity per episode, a taker-in/taker-out round trip inside `--hold-s`, and the
hourly-normalised funding carry per venue pair. `--from` / `--to` restrict the
window (US RTH is 13:30-20:00 UTC). It streams each file once: ~9 s and ~150 MB
of RSS for 3.4 M rows (2 M `_all` + 1 M hits + 0.34 M depth).

Funding is reported raw AND normalised to bps/h (Aster / 8, HL and Lighter as
reported). That unit was never verified against a settlement, so implausible
magnitudes are flagged in the report instead of being trusted -- Lighter's
0.0012 reads as 12 bps/h (1051 %/yr), which is almost certainly not per hour.
