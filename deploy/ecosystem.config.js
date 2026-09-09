// pm2 process definitions for the read-only cross-venue spread watcher
// (src/spread_watch.py). No keys, no execution client, data clients only.
//
// Usage:
//   pm2 start deploy/ecosystem.config.js --only stocks
//   pm2 start deploy/ecosystem.config.js --only crypto
//   pm2 logs stocks
//   pm2 stop stocks
//   pm2 delete stocks
//
// Deadline: the script exits 0 on its own at --until / after --minutes, and
// pm2 must NOT relaunch it (autorestart: false, max_restarts: 0) -- the exit
// is expected, not a crash. Pass the deadline at start time via the
// WATCH_UNTIL env var, e.g.:
//   WATCH_UNTIL=2026-09-08T20:05:00Z pm2 start deploy/ecosystem.config.js --only stocks
// If WATCH_UNTIL is unset, the app falls back to `--minutes 30` (see the
// `args` functions below).

const path = require('path');

const REPO_ROOT = path.join(__dirname, '..');
const INTERPRETER = '.venv/bin/python';
const SCRIPT = 'src/spread_watch.py';
// LIGHTER_RH is Lighter's Robinhood Chain deployment (venue LIGHTER_ROBINHOOD,
// quote asset USDG, zero fees). Symbols it does not list -- HOOD, ASTER, DASH,
// PUMP, ARB as of 2026-09-07 -- simply skip that leg with an INFO line.
const VENUES = 'HL,LIGHTER,LIGHTER_RH,ASTER';
const OUT = 'reports/stage1';

function watchArgs(symbols) {
  const deadline = process.env.WATCH_UNTIL;
  const deadlineArgs = deadline ? ['--until', deadline] : ['--minutes', '30'];
  // WATCH_REFERENCE=FUTU adds the real US stock quote as a reference leg
  // (src/ref_feed.py). Needs FUTU_API_KEY / FUTU_PRIVATE_KEY in the repo's
  // .env on this box: spread_watch.py loads it itself, pm2 never sees the keys.
  const reference = process.env.WATCH_REFERENCE;
  const referenceArgs = reference ? ['--reference', reference] : [];
  return [
    SCRIPT,
    '--symbols', symbols.join(','),
    '--venues', VENUES,
    '--out', OUT,
    ...deadlineArgs,
    ...referenceArgs,
  ];
}

const STOCKS_SYMBOLS = ['NVDA', 'TSLA', 'HOOD', 'SNDK', 'MU', 'SPCX', 'GOLD1'];
const CRYPTO_SYMBOLS = ['SOL', 'HYPE', 'ZEC', 'PONS', 'LIT', 'ASTER', 'DASH', 'PUMP', 'ARB',
  // 2026-09-08 screen candidates (ANSEM has no HL leg)
  'ANSEM', 'XPL', 'MON', 'EIGEN', 'TIA',
  // 2026-09-09 quick screen (FF has no HL leg)
  'VVV', 'ETHFI', 'FF', 'AERO', 'ZRO', 'USELESS'];

function appDef(name, symbols) {
  return {
    name,
    script: SCRIPT,
    interpreter: INTERPRETER,
    cwd: REPO_ROOT,
    args: watchArgs(symbols).slice(1), // drop the leading script path; pm2 adds it back
    autorestart: false,
    max_restarts: 0,
    out_file: 'logs/pm2-' + name + '.out.log',
    error_file: 'logs/pm2-' + name + '.err.log',
    time: true,
    env: {
      PYTHONUNBUFFERED: '1',
    },
  };
}

// Live maker (src/maker_live.py). Mainnet orders: only after the user's explicit go.
//   MAKER_MINUTES=15 MAKER_SYMBOL=PONS pm2 start deploy/ecosystem.config.js --only maker
//   pm2 stop maker   -> SIGINT -> the strategy cancels its orders, hedges the residual, exits
const MAKER_SCRIPT = 'src/maker_live.py';
const makerApp = {
  name: 'maker',
  script: MAKER_SCRIPT,
  interpreter: INTERPRETER,
  cwd: REPO_ROOT,
  args: [
    '--live', '--env', process.env.MAKER_ENV || 'mainnet',
    '--symbol', process.env.MAKER_SYMBOL || 'PONS',
    '--confirm-mainnet',
    '--minutes', process.env.MAKER_MINUTES || '15',
    '--out', 'reports/live',
  ],
  autorestart: false,
  max_restarts: 0,
  kill_timeout: 45000, // pm2 stop: SIGINT, then up to 45 s for cancel-on-stop + residual hedge
  out_file: 'logs/pm2-maker.out.log',
  error_file: 'logs/pm2-maker.err.log',
  time: true,
  env: { PYTHONUNBUFFERED: '1' },
};

module.exports = {
  apps: [
    appDef('stocks', STOCKS_SYMBOLS),
    appDef('crypto', CRYPTO_SYMBOLS),
    makerApp,
  ],
};
