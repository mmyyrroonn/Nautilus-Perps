// Operator-started live trading. Credentials are loaded from the app's .env.
// Start: pm2 start deploy/opportunity-live.config.js --only persarb-live
// Stop:  pm2 stop persarb-live
const path = require('path');
const root = path.resolve(__dirname, '..');

module.exports = {
  apps: [{
    name: 'persarb-live',
    cwd: root,
    script: 'src/opportunity_trade.py',
    interpreter: path.join(root, '.venv/bin/python'),
    interpreter_args: '-B -u',
    args: [
      '--config', 'config/opportunity-trade.live.toml',
      '--execute-live',
      '--startup-balance-check',
    ],
    instances: 1,
    exec_mode: 'fork',
    watch: false,
    autorestart: false,
    max_restarts: 0,
    // Allow SIGINT handling to drain orders and complete native shutdown.
    kill_timeout: 120000,
    out_file: 'logs/pm2-persarb-live.out.log',
    error_file: 'logs/pm2-persarb-live.err.log',
    time: true,
    env: { PYTHONUNBUFFERED: '1' },
  }],
};
