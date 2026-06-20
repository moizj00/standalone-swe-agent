import express from 'express';
import path from 'path';
import { createServer as createViteServer } from 'vite';

/**
 * This server no longer talks to Gemini. It is a thin proxy in front of the
 * standalone SWE agent's Python HTTP/SSE bridge (swe_agent/server.py), which
 * runs the real tool-calling loop on a local Ollama model.
 *
 *   Browser ──▶ this Express server ──▶ AGENT_SERVER_URL (Python agent)
 *
 * Why proxy instead of calling the Python server directly from the browser:
 *   - same-origin (no CORS) for the React app, and
 *   - the bearer token stays server-side (SWE_AGENT_SERVER_TOKEN), never shipped
 *     to the client.
 *
 * Start the agent first, e.g.:
 *   python -m swe_agent.server --cwd /path/to/workspace --approval read-only \
 *          --token "$SWE_AGENT_SERVER_TOKEN"
 */
const AGENT_SERVER_URL = (process.env.AGENT_SERVER_URL || 'http://127.0.0.1:8765').replace(/\/$/, '');
const AGENT_TOKEN = process.env.SWE_AGENT_SERVER_TOKEN;

function agentHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const h: Record<string, string> = { ...extra };
  if (AGENT_TOKEN) h['Authorization'] = `Bearer ${AGENT_TOKEN}`;
  return h;
}

async function startServer() {
  const app = express();
  const PORT = 3000;

  // Match the agent server's 16MB cap: a turn can ship many custom-tool schemas,
  // and the default ~100kb limit would 413 them before they reach Python.
  app.use(express.json({ limit: '16mb' }));

  // -- Tools ----------------------------------------------------------------
  // GET proxies the REAL tool registry from the Python agent (read-only).
  app.get('/api/tools', async (_req, res) => {
    try {
      const upstream = await fetch(`${AGENT_SERVER_URL}/api/tools`, { headers: agentHeaders() });
      const data = await upstream.json();
      // Pass the { tools, reserved } object through verbatim; both dashboard
      // consumers tolerate object-or-array, and `reserved` carries alias names.
      res.status(upstream.status).json(data);
    } catch (error: any) {
      res.status(502).json({ error: `Agent server unreachable at ${AGENT_SERVER_URL}: ${error.message}` });
    }
  });

  // Tools are defined in Python code, not editable at runtime. Accept the POST so
  // the builder UI doesn't error, but make clear the change is display-only.
  app.post('/api/tools', async (_req, res) => {
    res.json({
      success: false,
      readOnly: true,
      message: 'Tool definitions are managed by the Python SWE agent (code-registered). ' +
               'Edits here are not applied. Add a ToolSpec in swe_agent/tools/ to add a real tool.',
    });
  });

  // -- Conversations (database backend) -----------------------------------------------
  // POST /api/conversations - Create a new conversation
  app.post('/api/conversations', async (req, res) => {
    try {
      const { userId, title } = req.body;
      if (!userId || !title) {
        return res.status(400).json({ error: 'Missing userId or title' });
      }

      const { db } = await import('./src/lib/db');
      const { generateId } = await import('./src/lib/utils');
      const { conversation } = await import('./src/lib/db/schema');

      const id = generateId();
      const now = new Date();
      
      const result = await db.insert(conversation).values({
        id,
        userId,
        title,
        createdAt: now,
        updatedAt: now,
      }).returning();

      res.status(201).json(result[0]);
    } catch (error: any) {
      console.error('Error creating conversation:', error);
      res.status(500).json({ error: error.message });
    }
  });

  // GET /api/conversations - List user's conversations
  app.get('/api/conversations', async (req, res) => {
    try {
      const userId = req.query.userId as string;
      if (!userId) {
        return res.status(400).json({ error: 'Missing userId' });
      }

      const { db } = await import('./src/lib/db');
      const { conversation } = await import('./src/lib/db/schema');
      const { eq, desc } = await import('drizzle-orm');

      const results = await db
        .select()
        .from(conversation)
        .where(eq(conversation.userId, userId))
        .orderBy(desc(conversation.updatedAt));

      res.json(results);
    } catch (error: any) {
      console.error('Error fetching conversations:', error);
      res.status(500).json({ error: error.message });
    }
  });

  // POST /api/conversations/:conversationId/messages - Add message
  app.post('/api/conversations/:conversationId/messages', async (req, res) => {
    try {
      const { conversationId } = req.params;
      const { userId, role, content, toolCalls } = req.body;

      if (!userId || !role || !content) {
        return res.status(400).json({ error: 'Missing required fields' });
      }

      const { db } = await import('./src/lib/db');
      const { generateId } = await import('./src/lib/utils');
      const { message, conversation } = await import('./src/lib/db/schema');
      const { eq } = await import('drizzle-orm');

      // Verify user owns the conversation
      const conv = await db.select().from(conversation).where(eq(conversation.id, conversationId));
      if (!conv.length || conv[0].userId !== userId) {
        return res.status(403).json({ error: 'Unauthorized' });
      }

      const id = generateId();
      const now = new Date();

      const result = await db.insert(message).values({
        id,
        conversationId,
        userId,
        role,
        content,
        toolCalls: toolCalls ? JSON.stringify(toolCalls) : null,
        createdAt: now,
      }).returning();

      // Update conversation updatedAt
      await db.update(conversation)
        .set({ updatedAt: now })
        .where(eq(conversation.id, conversationId));

      res.status(201).json(result[0]);
    } catch (error: any) {
      console.error('Error adding message:', error);
      res.status(500).json({ error: error.message });
    }
  });

  // GET /api/conversations/:conversationId/messages - Get messages
  app.get('/api/conversations/:conversationId/messages', async (req, res) => {
    try {
      const { conversationId } = req.params;
      const userId = req.query.userId as string;

      if (!userId) {
        return res.status(400).json({ error: 'Missing userId' });
      }

      const { db } = await import('./src/lib/db');
      const { message, conversation } = await import('./src/lib/db/schema');
      const { eq } = await import('drizzle-orm');

      // Verify user owns the conversation
      const conv = await db.select().from(conversation).where(eq(conversation.id, conversationId));
      if (!conv.length || conv[0].userId !== userId) {
        return res.status(403).json({ error: 'Unauthorized' });
      }

      const results = await db
        .select()
        .from(message)
        .where(eq(message.conversationId, conversationId))
        .orderBy(message.createdAt);

      res.json(results);
    } catch (error: any) {
      console.error('Error fetching messages:', error);
      res.status(500).json({ error: error.message });
    }
  });

  // -- VS Code workspace integration (unchanged) ----------------------------
  const fs = await import('fs/promises');

  app.post('/api/vscode/init', async (_req, res) => {
    try {
      const vscodeDir = path.join(process.cwd(), '.vscode');
      await fs.mkdir(vscodeDir, { recursive: true });

      const settings = {
        "workbench.colorCustomizations": {
          "activityBar.background": "#1e1b4b",
          "titleBar.activeBackground": "#1e1b4b",
          "titleBar.activeForeground": "#f8fafc"
        },
        "editor.fontFamily": "'JetBrains Mono', 'Fira Code', Consolas, monospace",
        "editor.fontSize": 14,
        "editor.lineHeight": 22,
        "editor.tabSize": 2,
        "editor.insertSpaces": true,
        "editor.formatOnSave": true,
        "files.exclude": {
          "**/.git": true,
          "**/node_modules": true,
          "**/dist": true
        },
        "tailwindCSS.emmetCompletions": true
      };

      const tasks = {
        "version": "2.0.0",
        "tasks": [
          { "label": "AI Studio: Start Development Server", "type": "shell", "command": "npm run dev", "group": "active", "presentation": { "reveal": "always", "panel": "new" } },
          { "label": "AI Studio: Compile & Build Application", "type": "shell", "command": "npm run build", "group": { "kind": "build", "isDefault": true } },
          { "label": "AI Studio: Lint Workspace", "type": "shell", "command": "npm run lint", "problemMatcher": [] }
        ]
      };

      const extensions = {
        "recommendations": [
          "bradlc.vscode-tailwindcss",
          "dbaeumer.vscode-eslint",
          "esbenp.prettier-vscode",
          "ms-vscode.azure-repos"
        ]
      };

      await fs.writeFile(path.join(vscodeDir, 'settings.json'), JSON.stringify(settings, null, 2), 'utf-8');
      await fs.writeFile(path.join(vscodeDir, 'tasks.json'), JSON.stringify(tasks, null, 2), 'utf-8');
      await fs.writeFile(path.join(vscodeDir, 'extensions.json'), JSON.stringify(extensions, null, 2), 'utf-8');

      res.json({ success: true, message: 'VS Code integration workspace fully initialized.', files: { settings: true, tasks: true, extensions: true } });
    } catch (error: any) {
      res.status(500).json({ error: error.message });
    }
  });

  app.get('/api/vscode/status', async (_req, res) => {
    try {
      const vscodeDir = path.join(process.cwd(), '.vscode');
      let files = { settings: false, tasks: false, extensions: false };
      try { files.settings = (await fs.stat(path.join(vscodeDir, 'settings.json'))).isFile(); } catch {}
      try { files.tasks = (await fs.stat(path.join(vscodeDir, 'tasks.json'))).isFile(); } catch {}
      try { files.extensions = (await fs.stat(path.join(vscodeDir, 'extensions.json'))).isFile(); } catch {}
      const isInitialized = files.settings && files.tasks && files.extensions;
      res.json({
        isInitialized, files,
        workspaceUrl: `vscode://file${process.cwd()}`,
        env: { PORT, NODE_ENV: process.env.NODE_ENV || 'development', cwd: process.cwd(), os: process.platform },
      });
    } catch (error: any) {
      res.status(500).json({ error: error.message });
    }
  });

  // -- Chat: proxy to the Python SWE agent ----------------------------------

  // Non-streaming, drop-in: returns { text, session_id }.
  app.post('/api/chat', async (req, res) => {
    try {
      const upstream = await fetch(`${AGENT_SERVER_URL}/api/chat`, {
        method: 'POST',
        headers: agentHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(req.body ?? {}),
      });
      const data = await upstream.json();
      res.status(upstream.status).json(data);
    } catch (error: any) {
      res.status(502).json({ error: `Agent server unreachable at ${AGENT_SERVER_URL}: ${error.message}` });
    }
  });

  // Streaming: pipe the agent's Server-Sent Events straight through to the browser.
  app.post('/api/chat/stream', async (req, res) => {
    let upstream: Response;
    try {
      upstream = await fetch(`${AGENT_SERVER_URL}/api/chat/stream`, {
        method: 'POST',
        headers: agentHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(req.body ?? {}),
      });
    } catch (error: any) {
      res.status(502).json({ error: `Agent server unreachable at ${AGENT_SERVER_URL}: ${error.message}` });
      return;
    }
    if (!upstream.ok || !upstream.body) {
      const text = await upstream.text().catch(() => '');
      res.status(upstream.status || 502).json({ error: text || 'Agent stream failed.' });
      return;
    }
    res.status(200);
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.flushHeaders?.();

    const reader = upstream.body.getReader();
    const decoder = new TextDecoder();
    req.on('close', () => { reader.cancel().catch(() => {}); });
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        res.write(decoder.decode(value, { stream: true }));
      }
    } catch {
      // Upstream broke mid-stream (Python reset/died). Surface it as an SSE error
      // event so the client shows a failure instead of silently truncating.
      try { res.write('data: ' + JSON.stringify({ type: 'error', message: 'agent stream interrupted' }) + '\n\n'); } catch {}
    }
    res.end();
  });

  // -- Vite / static --------------------------------------------------------
  if (process.env.NODE_ENV !== 'production') {
    const vite = await createViteServer({ server: { middlewareMode: true }, appType: 'spa' });
    app.use(vite.middlewares);
  } else {
    const distPath = path.join(process.cwd(), 'dist');
    app.use(express.static(distPath));
    app.get('*', (_req, res) => { res.sendFile(path.join(distPath, 'index.html')); });
  }

  // Bind loopback by default (matches the agent). Opt into LAN exposure explicitly
  // with BIND_HOST=0.0.0.0, and set a token when you do.
  const BIND_HOST = process.env.BIND_HOST || '127.0.0.1';
  app.listen(PORT, BIND_HOST, () => {
    console.log(`Dashboard on http://${BIND_HOST}:${PORT}  (proxying agent at ${AGENT_SERVER_URL})`);
    if (BIND_HOST !== '127.0.0.1' && BIND_HOST !== 'localhost' && !AGENT_TOKEN) {
      console.warn('  ⚠ Non-loopback bind with no SWE_AGENT_SERVER_TOKEN — the agent tool loop is exposed to the LAN.');
    }
  });
}

startServer();
