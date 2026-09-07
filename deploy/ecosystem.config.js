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
const VENUES = 'HL,LIGHTER,ASTER';
const OUT = 'reports/stage1';

function watchArgs(symbols) {
  const deadline = process.env.WATCH_UNTIL;
  const deadlineArgs = deadline ? ['--until', deadline] : ['--minutes', '30'];
  return [
    SCRIPT,
    '--symbols', symbols.join(','),
    '--venues', VENUES,
    '--out', OUT,
    ...deadlineArgs,
  ];
}

const STOCKS_SYMBOLS = ['NVDA', 'TSLA', 'HOOD', 'SNDK', 'MU', 'SPCX', 'GOLD1'];
const CRYPTO_SYMBOLS = ['SOL', 'HYPE', 'ZEC', 'PONS', 'LIT', 'ASTER', 'DASH', 'PUMP', 'ARB'];

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

module.exports = {
  apps: [
    appDef('stocks', STOCKS_SYMBOLS),
    appDef('crypto', CRYPTO_SYMBOLS),
  ],
};
