#!/usr/bin/env node
// Serves index.html and proxies /v1/* to the local LLM server.
const http  = require('http');
const https = require('https');
const fs    = require('fs');
const path  = require('path');
const url   = require('url');

const LLM_HOST = '192.168.5.13';
const LLM_PORT = 1234;
const PORT     = 3000;

const CORS_HEADERS = {
  'Access-Control-Allow-Origin':  '*',
  'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type,Authorization',
};

const server = http.createServer((req, res) => {
  // CORS preflight
  if (req.method === 'OPTIONS') {
    res.writeHead(204, CORS_HEADERS);
    res.end();
    return;
  }

  const parsed = url.parse(req.url);

  // Proxy /v1/* to the LLM server
  if (parsed.pathname.startsWith('/v1/')) {
    const body = [];
    req.on('data', chunk => body.push(chunk));
    req.on('end', () => {
      const bodyBuf = Buffer.concat(body);
      const options = {
        hostname: LLM_HOST,
        port:     LLM_PORT,
        path:     req.url,
        method:   req.method,
        headers: {
          'Content-Type':   'application/json',
          'Authorization':  'Bearer dummy',
          'Content-Length': bodyBuf.length,
        },
      };

      const proxy = http.request(options, upstream => {
        res.writeHead(upstream.statusCode, {
          ...CORS_HEADERS,
          'Content-Type': upstream.headers['content-type'] || 'application/json',
          // pass through transfer-encoding for streaming
          ...(upstream.headers['transfer-encoding']
            ? { 'Transfer-Encoding': upstream.headers['transfer-encoding'] }
            : {}),
        });
        upstream.pipe(res);
      });

      proxy.on('error', err => {
        res.writeHead(502, CORS_HEADERS);
        res.end(JSON.stringify({ error: err.message }));
      });

      proxy.write(bodyBuf);
      proxy.end();
    });
    return;
  }

  // Serve index.html for everything else
  const file = path.join(__dirname, 'index.html');
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); res.end('Not found'); return; }
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(data);
  });
});

server.listen(PORT, () => {
  console.log(`Chat app running at http://localhost:${PORT}`);
  console.log(`Proxying /v1/* → http://${LLM_HOST}:${LLM_PORT}`);
});
