#!/usr/bin/env node
/**
 * ScholarLab CLI — start/stop/status the academic research platform.
 *
 * Usage:
 *   scholarlab start   — launch backend + frontend, open browser
 *   scholarlab stop    — kill running services
 *   scholarlab status  — show service health
 *   scholarlab setup   — (re)install Python dependencies
 */

import { spawn, execSync } from 'node:child_process';
import {
  existsSync, mkdirSync, readFileSync, writeFileSync,
  unlinkSync, openSync,
} from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import net from 'node:net';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = dirname(__filename);
const ROOT       = join(__dirname, '..');
const LOG_DIR    = join(ROOT, 'logs');
const PID_DIR    = join(ROOT, '.pids');
const FRONTEND   = join(ROOT, 'frontend');

const DEFAULT_PORTS = { resource: 8905, bridge: 8906, frontend: 5903 };

function ensureDirs() {
  for (const d of [LOG_DIR, PID_DIR]) {
    mkdirSync(d, { recursive: true });
  }
}

function findPython() {
  const venvPaths = [
    join(ROOT, '.venv', 'Scripts', 'python.exe'),
    join(ROOT, '.venv', 'bin', 'python'),
    join(ROOT, 'claw-ai-env', 'Scripts', 'python.exe'),
    join(ROOT, 'claw-ai-env', 'bin', 'python'),
  ];
  for (const p of venvPaths) {
    if (existsSync(p) && pythonHasDeps(p)) return p;
  }
  for (const cmd of ['python3', 'python']) {
    try {
      execSync(`${cmd} --version`, { stdio: 'ignore' });
      if (pythonHasDeps(cmd)) return cmd;
    } catch { /* skip */ }
  }
  return null;
}

function pythonHasDeps(py) {
  try {
    execSync(`"${py}" -c "import websockets, psutil"`, { stdio: 'ignore', timeout: 5000 });
    return true;
  } catch {
    return false;
  }
}

function isPortInUse(port) {
  return new Promise((resolve) => {
    const c = net.createConnection({ port, host: '127.0.0.1' });
    c.once('connect', () => { c.end(); resolve(true); });
    c.once('error', () => resolve(false));
    c.setTimeout(1500, () => { c.destroy(); resolve(false); });
  });
}

function readPid(name) {
  const f = join(PID_DIR, `${name}.pid`);
  if (!existsSync(f)) return null;
  const pid = parseInt(readFileSync(f, 'utf8').trim(), 10);
  return isNaN(pid) ? null : pid;
}

function writePid(name, pid) {
  writeFileSync(join(PID_DIR, `${name}.pid`), String(pid), 'utf8');
}

function removePid(name) {
  const f = join(PID_DIR, `${name}.pid`);
  try { unlinkSync(f); } catch { /* ignore */ }
}

function killPid(pid) {
  try {
    process.kill(pid, 'SIGTERM');
    return true;
  } catch {
    return false;
  }
}

function launchProcess(name, cmd, args, opts = {}) {
  const outLog = join(LOG_DIR, `${name}.log`);
  const errLog = join(LOG_DIR, `${name}.err`);

  const out = openSync(outLog, 'w');
  const err = openSync(errLog, 'w');

  const mergedEnv = { ...process.env, PYTHONUTF8: '1' };
  if (opts.env) Object.assign(mergedEnv, opts.env);

  const proc = spawn(cmd, args, {
    cwd: opts.cwd || ROOT,
    stdio: ['ignore', out, err],
    detached: true,
    env: mergedEnv,
    windowsHide: true,
  });
  proc.unref();
  writePid(name, proc.pid);
  return proc.pid;
}

async function startServices() {
  ensureDirs();
  let py = findPython();
  if (!py) {
    console.log('  Python environment not found. Running setup...\n');
    try {
      const { setupPython } = await import('./setup-python.js');
      await setupPython();
      py = findPython();
    } catch { /* ignore */ }
    if (!py) {
      console.error('  Python 3.10+ is required but not found.');
      console.error('  Install from: https://www.python.org/downloads/');
      console.error('  Then run: scholarlab setup\n');
      process.exit(1);
    }
  }

  const ports = { ...DEFAULT_PORTS };
  console.log('\n  ScholarLab starting...\n');

  // 1. Resource Monitor
  if (await isPortInUse(ports.resource)) {
    console.log(`  [skip] resource_monitor already on port ${ports.resource}`);
  } else {
    const pid = launchProcess('resource_monitor', py, [
      '-u', join(ROOT, 'backend', 'services', 'resource_monitor.py'),
      '--port', String(ports.resource),
    ]);
    console.log(`  [ok]   resource_monitor  PID=${pid}`);
  }

  // 2. Agent Bridge
  if (await isPortInUse(ports.bridge)) {
    console.log(`  [skip] agent_bridge already on port ${ports.bridge}`);
  } else {
    const pid = launchProcess('agent_bridge', py, [
      '-u', join(ROOT, 'backend', 'services', 'agent_bridge.py'),
      '--port', String(ports.bridge),
      '--python', py,
      '--agent-dir', join(ROOT, 'backend', 'agent'),
      '--runs-dir', join(ROOT, 'backend', 'runs'),
      '--pool-idea', '1', '--pool-exp', '1', '--pool-code', '1',
      '--pool-exec', '1', '--pool-write', '1',
      '--total-gpus', '8', '--gpus-per-project', '1',
      '--discussion-mode', '--discussion-rounds', '2',
    ]);
    console.log(`  [ok]   agent_bridge     PID=${pid}`);
  }

  // 3. Frontend
  if (await isPortInUse(ports.frontend)) {
    console.log(`  [skip] frontend already on port ${ports.frontend}`);
  } else {
    const distDir = existsSync(join(ROOT, 'dist', 'index.html'))
      ? join(ROOT, 'dist')
      : join(FRONTEND, 'dist');
    if (existsSync(join(distDir, 'index.html'))) {
      const pid = launchProcess('frontend', process.execPath, [
        join(ROOT, 'server', 'serve.mjs'),
        '--port', String(ports.frontend),
        '--bridge-port', String(ports.bridge),
        '--resource-port', String(ports.resource),
      ]);
      console.log(`  [ok]   frontend (prod)  PID=${pid}`);
    } else {
      const npx = process.platform === 'win32' ? 'npx.cmd' : 'npx';
      const pid = launchProcess('frontend', npx, [
        'vite', '--host', '0.0.0.0', '--port', String(ports.frontend),
      ], {
        cwd: FRONTEND,
        env: {
          RESOURCE_MONITOR_PORT: String(ports.resource),
          AGENT_BRIDGE_PORT: String(ports.bridge),
        },
      });
      console.log(`  [ok]   frontend (dev)   PID=${pid}`);
    }
  }

  console.log(`\n  Frontend:     http://localhost:${ports.frontend}/`);
  console.log(`  WS Bridge:    ws://localhost:${ports.bridge}`);
  console.log(`  WS Monitor:   ws://localhost:${ports.resource}\n`);

  try {
    const open = (await import('open')).default;
    await open(`http://localhost:${ports.frontend}/`);
  } catch { /* ok if open fails */ }
}

function stopServices() {
  console.log('\n  Stopping ScholarLab...\n');
  for (const name of ['frontend', 'agent_bridge', 'resource_monitor']) {
    const pid = readPid(name);
    if (pid) {
      killPid(pid);
      console.log(`  [stopped] ${name} PID=${pid}`);
      removePid(name);
    }
  }

  if (process.platform === 'win32') {
    for (const port of Object.values(DEFAULT_PORTS)) {
      try {
        execSync(
          `for /f "tokens=5" %a in ('netstat -aon ^| findstr ":${port}.*LISTENING"') do taskkill /F /PID %a`,
          { stdio: 'ignore', shell: true },
        );
      } catch { /* ignore */ }
    }
  }
  console.log();
}

async function showStatus() {
  console.log('\n  ScholarLab Service Status:\n');
  const names = [
    { name: 'resource_monitor', port: DEFAULT_PORTS.resource },
    { name: 'agent_bridge',     port: DEFAULT_PORTS.bridge },
    { name: 'frontend',         port: DEFAULT_PORTS.frontend },
  ];
  for (const { name, port } of names) {
    const up = await isPortInUse(port);
    const tag = up ? '\x1b[32m[UP]\x1b[0m  ' : '\x1b[31m[DOWN]\x1b[0m';
    console.log(`  ${tag} ${name} :${port}`);
  }
  console.log();
}

// ── Main ──
const [,, command = 'start'] = process.argv;

switch (command) {
  case 'start':
    await startServices();
    break;
  case 'stop':
    stopServices();
    break;
  case 'restart':
    stopServices();
    await new Promise(r => setTimeout(r, 1500));
    await startServices();
    break;
  case 'status':
    await showStatus();
    break;
  case 'setup': {
    const { setupPython } = await import('./setup-python.js');
    await setupPython();
    break;
  }
  default:
    console.log('\n  Usage: scholarlab <start|stop|restart|status|setup>\n');
}
