// Electron main process — desktop frame for the standalone SWE coding agent.
//
// CLOUD-ONLY build (no local Ollama). Orchestration (all loopback):
//
//   Electron main
//     ├─ generate a random bearer token (shared secret for this run)
//     ├─ spawn  : <venv python> -m swe_agent.server --provider <P> --api-key <…>
//     │            --port <AGENT_PORT> --token <tok> --cwd <workspace>
//     ├─ spawn  : web/ dashboard (npm run dev → Express+Vite on DASH_PORT)
//     │            with AGENT_SERVER_URL + SWE_AGENT_SERVER_TOKEN in its env, so the
//     │            Express proxy reaches Python and keeps the token server-side
//     ├─ wait   : poll /api/health (auth'd) then the dashboard, then load the window
//     └─ quit   : kill both child process trees
//
// Nothing here re-implements the agent: the real tool-calling loop lives in
// swe_agent/server.py, and the UI is the existing React dashboard in web/.

'use strict';

const { app, BrowserWindow, dialog, shell } = require('electron');
const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');

// ---------------------------------------------------------------- configuration

const REPO_ROOT = path.resolve(__dirname, '..');
const WEB_DIR = path.join(REPO_ROOT, 'web');

// Ports are configurable so two instances don't collide; defaults match web/server.ts
// (dashboard 3000) and the agent server (8765).
const AGENT_PORT = parseInt(process.env.SWE_AGENT_PORT || '8765', 10);
const DASH_PORT = parseInt(process.env.SWE_DASH_PORT || '3000', 10);
const AGENT_URL = `http://127.0.0.1:${AGENT_PORT}`;
const DASH_URL = `http://127.0.0.1:${DASH_PORT}`;

// Workspace the agent operates in. Defaults to the repo root; override with
// SWE_AGENT_WORKSPACE so the packaged app can point at the user's project.
const WORKSPACE = process.env.SWE_AGENT_WORKSPACE || REPO_ROOT;

// Approval posture over HTTP. Default read-only is the safe choice; the server
// itself refuses a mutating posture without a token unless --insecure is passed.
const APPROVAL = process.env.SWE_AGENT_APPROVAL || 'read-only';

// ---- Cloud LLM provider (this build is CLOUD-ONLY; no local Ollama) ---------
// The agent server is started with an explicit cloud --provider. Each provider
// reads its API key from a specific env var (see web/.env or your shell):
//   nemotron -> NVIDIA_API_KEY   openai -> OPENAI_API_KEY
//   minimax  -> MINIMAX_API_KEY  kimi   -> MOONSHOT_API_KEY (or KIMI_API_KEY)
const PROVIDER = (process.env.SWE_AGENT_PROVIDER || 'nemotron').toLowerCase();

const PROVIDER_KEY_ENVS = {
  nemotron: ['NVIDIA_API_KEY', 'NVIDIA_NIM_API_KEY', 'NGC_API_KEY'],
  openai: ['OPENAI_API_KEY'],
  minimax: ['MINIMAX_API_KEY'],
  kimi: ['MOONSHOT_API_KEY', 'KIMI_API_KEY'],
};

// Resolve the API key for the chosen provider from its env var(s). An explicit
// SWE_AGENT_API_KEY always wins. Returns '' when nothing is set.
function resolveApiKey() {
  if (process.env.SWE_AGENT_API_KEY) return process.env.SWE_AGENT_API_KEY;
  for (const name of PROVIDER_KEY_ENVS[PROVIDER] || []) {
    if (process.env[name]) return process.env[name];
  }
  return '';
}

const API_KEY = resolveApiKey();

// A fresh shared secret per launch — the dashboard proxy attaches it as a bearer
// token, and the agent server requires it, so nothing else on the machine can
// drive the tool loop.
const TOKEN = crypto.randomBytes(24).toString('hex');

// Resolve the project venv's Python (created during setup). Fall back to PATH.
function resolvePython() {
  const candidates =
    process.platform === 'win32'
      ? [path.join(REPO_ROOT, '.venv', 'Scripts', 'python.exe')]
      : [path.join(REPO_ROOT, '.venv', 'bin', 'python')];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

const PYTHON = process.env.SWE_AGENT_PYTHON || resolvePython();

// On Windows, npm is a .cmd shim and must be spawned via the shell.
const NPM = process.platform === 'win32' ? 'npm.cmd' : 'npm';

/** @type {import('child_process').ChildProcess[]} */
const children = [];
let mainWindow = null;
let shuttingDown = false;

// ---------------------------------------------------------------- child spawns

function logPrefix(name, data) {
  const text = data.toString();
  for (const line of text.split(/\r?\n/)) {
    if (line.trim()) console.log(`[${name}] ${line}`);
  }
}

function spawnAgent() {
  const args = [
    '-m', 'swe_agent.server',
    '--host', '127.0.0.1',
    '--port', String(AGENT_PORT),
    '--cwd', WORKSPACE,
    '--approval', APPROVAL,
    '--token', TOKEN,
    '--provider', PROVIDER,
  ];
  // Pass the resolved cloud key explicitly when we have one; otherwise let the
  // server resolve it from the provider's own env var (and fail preflight with a
  // clear message if none is set).
  if (API_KEY) {
    args.push('--api-key', API_KEY);
  }
  // Redact the key in the console echo.
  const echo = args.map((a) => (a === API_KEY ? '<api-key>' : a)).join(' ');
  console.log(`[main] starting agent (provider=${PROVIDER}): ${PYTHON} ${echo}`);
  const child = spawn(PYTHON, args, {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (d) => logPrefix('agent', d));
  child.stderr.on('data', (d) => logPrefix('agent', d));
  child.on('exit', (code) => onChildExit('agent server', code));
  children.push(child);
  return child;
}

function spawnDashboard() {
  console.log(`[main] starting dashboard: ${NPM} run dev (cwd=${WEB_DIR})`);
  const child = spawn(NPM, ['run', 'dev'], {
    cwd: WEB_DIR,
    env: {
      ...process.env,
      AGENT_SERVER_URL: AGENT_URL,
      SWE_AGENT_SERVER_TOKEN: TOKEN,
      // Bind loopback only; the proxy already keeps the token server-side.
      BIND_HOST: '127.0.0.1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    shell: process.platform === 'win32', // resolve npm.cmd on Windows
  });
  child.stdout.on('data', (d) => logPrefix('dashboard', d));
  child.stderr.on('data', (d) => logPrefix('dashboard', d));
  child.on('exit', (code) => onChildExit('dashboard', code));
  children.push(child);
  return child;
}

function onChildExit(name, code) {
  if (shuttingDown) return;
  console.error(`[main] ${name} exited unexpectedly (code ${code}).`);
  if (mainWindow && !mainWindow.isDestroyed()) {
    dialog.showErrorBox(
      'SWE Agent — backend stopped',
      `The ${name} process exited (code ${code}). Check the terminal logs.\n` +
        `The window will stay open but may be non-functional.`
    );
  }
}

// ---------------------------------------------------------------- readiness

// Poll an HTTP endpoint until it answers (any status) or we time out.
function waitForHttp(url, { headers = {}, timeoutMs = 60000, intervalMs = 400 } = {}) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(url, { headers }, (res) => {
        res.resume();
        resolve(res.statusCode);
      });
      req.on('error', () => {
        if (Date.now() > deadline) reject(new Error(`timed out waiting for ${url}`));
        else setTimeout(tick, intervalMs);
      });
      req.setTimeout(2000, () => req.destroy());
    };
    tick();
  });
}

// ---------------------------------------------------------------- window

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    backgroundColor: '#0b0b12',
    title: 'SWE Agent',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
  });

  // Open external links in the OS browser, not inside the app window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url) && !url.startsWith(DASH_URL)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('closed', () => { mainWindow = null; });
  return mainWindow;
}

function showLoading(win) {
  const html =
    'data:text/html,' +
    encodeURIComponent(`<!doctype html><html><head><meta charset="utf-8">
      <style>
        html,body{height:100%;margin:0;background:#0b0b12;color:#e5e7eb;
          font-family:Segoe UI,Roboto,system-ui,sans-serif;display:flex;
          align-items:center;justify-content:center}
        .box{text-align:center}
        .spin{width:38px;height:38px;border:3px solid #2a2a3a;border-top-color:#7c8cff;
          border-radius:50%;margin:0 auto 18px;animation:s 0.9s linear infinite}
        @keyframes s{to{transform:rotate(360deg)}}
        .muted{color:#8b8ba0;font-size:13px;margin-top:6px}
      </style></head><body><div class="box">
        <div class="spin"></div>
        <div>Starting SWE Agent…</div>
        <div class="muted">connecting to cloud model &amp; dashboard</div>
      </div></body></html>`);
  win.loadURL(html);
}

// ---------------------------------------------------------------- lifecycle

async function boot() {
  const win = createWindow();
  showLoading(win);

  // Cloud-only build: refuse to start without a key for the chosen provider, so
  // the user gets an actionable message instead of an opaque preflight failure.
  if (!API_KEY) {
    const envList = (PROVIDER_KEY_ENVS[PROVIDER] || ['SWE_AGENT_API_KEY']).join(' or ');
    dialog.showErrorBox(
      'SWE Agent — no cloud API key',
      `No API key found for provider "${PROVIDER}".\n\n` +
        `Set one of these environment variables and relaunch:\n  ${envList}\n\n` +
        `Or choose a different provider with SWE_AGENT_PROVIDER ` +
        `(nemotron, openai, minimax, kimi).`
    );
    app.quit();
    return;
  }

  spawnAgent();
  spawnDashboard();

  try {
    // The agent's /api/health requires the bearer token.
    await waitForHttp(`${AGENT_URL}/api/health`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
      timeoutMs: 60000, // cloud preflight + first request
    });
    console.log('[main] agent server is up.');

    await waitForHttp(DASH_URL, { timeoutMs: 90000 });
    console.log('[main] dashboard is up; loading UI.');

    if (win && !win.isDestroyed()) await win.loadURL(DASH_URL);
  } catch (err) {
    console.error('[main] startup failed:', err);
    dialog.showErrorBox(
      'SWE Agent — failed to start',
      `Could not reach the local servers.\n\n${err.message}\n\n` +
        `Checklist:\n` +
        `  • python venv at .venv with the agent installed\n` +
        `  • cd web && npm install\n` +
        `  • a valid cloud API key for provider "${PROVIDER}"`
    );
  }
}

function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log('[main] shutting down child processes…');
  for (const child of children) {
    if (child.exitCode !== null) continue;
    try {
      if (process.platform === 'win32') {
        // Kill the whole process tree (npm → vite/tsx, python).
        spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
      } else {
        child.kill('SIGTERM');
      }
    } catch (e) {
      console.error('[main] error killing child:', e.message);
    }
  }
}

app.whenReady().then(boot);

app.on('window-all-closed', () => {
  shutdown();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', shutdown);
process.on('exit', shutdown);
process.on('SIGINT', () => { shutdown(); process.exit(0); });
process.on('SIGTERM', () => { shutdown(); process.exit(0); });
