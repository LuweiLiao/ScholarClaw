#!/usr/bin/env node
/**
 * ScholarLab production server.
 * Serves the pre-built frontend and proxies WebSocket to Python backends.
 *
 * Uses http.createServer instead of app.listen because Express 5
 * does not keep the event loop alive on its own.
 */

import express from 'express';
import { createServer } from 'node:http';
import { createProxyMiddleware } from 'http-proxy-middleware';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { existsSync } from 'node:fs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const DIST = existsSync(join(ROOT, 'dist', 'index.html'))
  ? join(ROOT, 'dist')
  : join(ROOT, 'frontend', 'dist');

function parseArgs() {
  const args = process.argv.slice(2);
  const opts = { port: 5903, bridgePort: 8906, resourcePort: 8905 };
  for (let i = 0; i < args.length; i += 2) {
    switch (args[i]) {
      case '--port':          opts.port = parseInt(args[i + 1], 10); break;
      case '--bridge-port':   opts.bridgePort = parseInt(args[i + 1], 10); break;
      case '--resource-port': opts.resourcePort = parseInt(args[i + 1], 10); break;
    }
  }
  return opts;
}

const { port, bridgePort, resourcePort } = parseArgs();

if (!existsSync(join(DIST, 'index.html'))) {
  console.error(`  [error] Frontend not built. Run: cd frontend && npm run build`);
  process.exit(1);
}

const app = express();

app.use(express.static(DIST));

app.get('/download/{*splat}', (req, res) => {
  const dlProxy = createProxyMiddleware({
    target: `http://localhost:${bridgePort}`,
    changeOrigin: true,
  });
  dlProxy(req, res);
});

app.get('{*splat}', (_req, res) => {
  res.sendFile(join(DIST, 'index.html'));
});

const agentProxy = createProxyMiddleware({
  target: `http://localhost:${bridgePort}`,
  ws: true,
  pathRewrite: { '^/ws/agents': '/' },
  changeOrigin: true,
});

const resourceProxy = createProxyMiddleware({
  target: `http://localhost:${resourcePort}`,
  ws: true,
  pathRewrite: { '^/ws/resources': '/' },
  changeOrigin: true,
});

const server = createServer(app);

server.on('upgrade', (req, socket, head) => {
  const url = req.url || '';
  if (url.startsWith('/ws/agents')) {
    agentProxy.upgrade(req, socket, head);
  } else if (url.startsWith('/ws/resources')) {
    resourceProxy.upgrade(req, socket, head);
  } else {
    socket.destroy();
  }
});

server.listen(port, '0.0.0.0', () => {
  console.log(`  ScholarLab frontend serving at http://localhost:${port}/`);
});
