const fs = require('node:fs');
const fsp = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const puppeteer = require('puppeteer-core');

const rootDir = path.resolve(__dirname, '..');
const webDir = path.join(rootDir, 'web');
const outDir = path.join(rootDir, '.panel-preview');
const args = process.argv.slice(2);

function getArgValue(flag, fallback) {
  const direct = args.find(item => item.startsWith(`${flag}=`));
  if (direct) return direct.slice(flag.length + 1);
  const index = args.indexOf(flag);
  if (index >= 0 && args[index + 1]) return args[index + 1];
  return fallback;
}

function hasArg(flag) {
  return args.includes(flag);
}

function getPositionalArgs() {
  return args.filter(item => item && !item.startsWith('--'));
}

function resolveBrowserExecutable() {
  const envPath = process.env.PUPPETEER_EXECUTABLE_PATH;
  if (envPath && fs.existsSync(envPath)) {
    return envPath;
  }

  const candidates = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  ];

  const found = candidates.find(filePath => fs.existsSync(filePath));
  if (!found) {
    throw new Error('No supported browser found. Set PUPPETEER_EXECUTABLE_PATH to Chrome or Edge.');
  }
  return found;
}

function contentType(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  return {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.json': 'application/json; charset=utf-8',
  }[ext] || 'application/octet-stream';
}

function safeResolve(urlPathname) {
  const pathname = decodeURIComponent(urlPathname.split('?')[0]);
  const normalized = pathname === '/' ? '/index.html' : pathname;
  const resolved = path.normalize(path.join(webDir, normalized));
  if (!resolved.startsWith(webDir)) {
    return null;
  }
  return resolved;
}

async function serveFile(filePath, res) {
  try {
    const stat = await fsp.stat(filePath);
    if (stat.isDirectory()) {
      return serveFile(path.join(filePath, 'index.html'), res);
    }
    res.writeHead(200, { 'Content-Type': contentType(filePath) });
    fs.createReadStream(filePath).pipe(res);
  } catch {
    const fallback = path.join(webDir, '404.html');
    res.writeHead(404, { 'Content-Type': contentType(fallback) });
    fs.createReadStream(fallback).pipe(res);
  }
}

async function startServer() {
  const server = http.createServer(async (req, res) => {
    const target = safeResolve(req.url || '/');
    if (!target) {
      res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('Forbidden');
      return;
    }
    await serveFile(target, res);
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });

  const address = server.address();
  if (!address || typeof address === 'string') {
    throw new Error('Unable to determine preview server address');
  }

  return {
    server,
    url: `http://127.0.0.1:${address.port}`,
  };
}

async function capturePage(browser, baseUrl, pageName) {
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200, deviceScaleFactor: 1 });
  const url = `${baseUrl}/?preview=1&page=${encodeURIComponent(pageName)}`;
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector(pageName === 'overview' ? '.stats-grid' : '.page-area > *', { timeout: 10000 });
  await page.addStyleTag({ content: 'body { overflow: auto !important; }' });
  const outputPath = path.join(outDir, `panel-${pageName}.png`);
  await page.screenshot({ path: outputPath, fullPage: true });
  await page.close();
  return outputPath;
}

async function main() {
  const requestedPage = getArgValue('--page', getPositionalArgs()[0] || 'all');
  const headful = hasArg('--headful');
  const pages = requestedPage === 'all'
    ? ['overview', 'recovery', 'thresholds', 'developer']
    : [requestedPage];
  const executablePath = resolveBrowserExecutable();

  await fsp.mkdir(outDir, { recursive: true });
  const { server, url } = await startServer();
  const browser = await puppeteer.launch({
    executablePath,
    headless: headful ? false : true,
  });

  try {
    const outputs = [];
    for (const pageName of pages) {
      outputs.push(await capturePage(browser, url, pageName));
    }
    console.log(`Browser: ${executablePath}`);
    console.log(`Preview server: ${url}`);
    outputs.forEach(file => console.log(`Captured: ${file}`));
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(err => {
  console.error(err);
  process.exitCode = 1;
});