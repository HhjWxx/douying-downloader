import { app, BrowserWindow, WebContentsView, ipcMain, dialog, shell, protocol, net, session } from 'electron';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { createInterface } from 'node:readline';
import { appendFile, mkdir, readFile, realpath } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { allowedPaths, apiAllowed, contextFromURL, douyinURL, pageScript } from './policy.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '../..');
const repoRoot = path.resolve(desktopRoot, '..');
const testMode = !app.isPackaged && process.env.DOUYIN_DESKTOP_TEST === '1';
if (process.env.DOUYIN_DESKTOP_DATA_DIR && !app.isPackaged) app.setPath('userData', process.env.DOUYIN_DESKTOP_DATA_DIR);
protocol.registerSchemesAsPrivileged([
  { scheme: 'app', privileges: { standard: true, secure: true, supportFetchAPI: true } },
  { scheme: 'local-media', privileges: { standard: true, secure: true, stream: true, supportFetchAPI: true } },
]);

let window: BrowserWindow;
let browser: WebContentsView;
let bridgeWindow: BrowserWindow;
let sidecar: ChildProcessWithoutNullStreams | undefined;
let socket: WebSocket | undefined;
let baseURL = '';
let token = '';
let quitting = false;
let browserVisible = false;
let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
let cookieTimer: ReturnType<typeof setTimeout> | undefined;
const partition = 'persist:douyin';
const mediaTokens = new Map<string, string>();
const knownPaths = new Set<string>();
let browserItems = new Map<string, any>();
let context = { current: '', author: '', mix: '' };
let captureGeneration = 0;
let contentPageKey = '';
let contextTimer: ReturnType<typeof setInterval> | undefined;
let bridgeChain = Promise.resolve();
let currentSettings = { path: '', proxy: '', thread: 2, cleanup: true };

function emit(message: unknown) {
  if (window && !window.isDestroyed()) window.webContents.send('desktop-event', message);
}

function trustSender(event: Electron.IpcMainInvokeEvent) {
  if (event.sender !== window.webContents || event.senderFrame?.url.split('/')[2] !== 'ui') throw Error('IPC 来源无效');
}

function recordPaths(value: any) {
  if (!value || typeof value !== 'object') return;
  if (Array.isArray(value)) { for (const item of value) recordPaths(item); return; }
  for (const [key, item] of Object.entries(value)) {
    if (['file_path', 'clean_path', 'report_path', 'source', 'output', 'report'].includes(key) && typeof item === 'string' && path.isAbsolute(item)) knownPaths.add(path.resolve(item));
    else if (key === 'output_dirs' && Array.isArray(item)) item.forEach(p => knownPaths.add(path.resolve(p)));
    else if (typeof item === 'object') recordPaths(item);
  }
}

async function api(method: string, route: string, body?: unknown) {
  if (!baseURL) throw Error('下载服务正在启动');
  const response = await fetch(baseURL + route, { method,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(120000) });
  const result = await response.json() as any;
  if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : '输入无效，请检查设置');
  recordPaths(result);
  return result;
}

async function syncCookies() {
  if (!baseURL || quitting) return;
  const values = await session.fromPartition(partition).cookies.get({ domain: '.douyin.com' });
  const cookies: Record<string, string> = {};
  for (const value of values) cookies[value.name] = value.value;
  const result = await api('POST', '/api/v1/session', { cookies });
  emit({ type: 'session', ...result });
}

function connectSocket() {
  if (!baseURL || quitting) return;
  const current = new WebSocket(baseURL.replace('http:', 'ws:') + '/api/v1/desktop/session', token);
  socket = current;
  current.onmessage = event => {
    const message = JSON.parse(String(event.data));
    if (message.type === 'fetch') {
      // 隐藏窗口请求串行，避免页面初始化及登录验证期间相互干扰。
      bridgeChain = bridgeChain.then(async () => {
        try {
          if (testMode) throw Error('测试模式未连接抖音');
          if (!bridgeWindow.webContents.getURL().startsWith('https://www.douyin.com')) await bridgeWindow.loadURL('https://www.douyin.com/');
          if (bridgeWindow.webContents.isLoading()) {
            await new Promise<void>((resolve, reject) => {
              const timer = setTimeout(() => reject(Error('登录页面尚未加载完成')), 20000);
              bridgeWindow.webContents.once('did-stop-loading', () => { clearTimeout(timer); resolve(); });
            });
          }
          const result = await bridgeWindow.webContents.executeJavaScript(pageScript(message));
          if (current.readyState === WebSocket.OPEN) current.send(JSON.stringify({ type: 'response', id: message.id, ...result }));
        } catch {
          if (current.readyState === WebSocket.OPEN) current.send(JSON.stringify({ type: 'response', id: message.id, code: 'PAGE_ERROR', error: '页面请求失败，请打开验证窗口检查登录和网络' }));
        }
      });
    } else {
      recordPaths(message);
      emit(message);
      if (message.type === 'connected') void syncCookies().catch(() => {});
    }
  };
  current.onclose = () => {
    if (socket !== current || quitting || !baseURL) return;
    emit({ type: 'connection', connected: false });
    reconnectTimer = setTimeout(connectSocket, 1500);
  };
  current.onerror = () => {};
}

async function startSidecar() {
  const dataDir = app.getPath('userData');
  await mkdir(dataDir, { recursive: true });
  const exe = app.isPackaged ? path.join(process.resourcesPath, 'sidecar', process.platform === 'win32' ? 'douyin-sidecar.exe' : 'douyin-sidecar')
    : process.env.DOUYIN_PYTHON || path.join(repoRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const args = app.isPackaged ? [] : ['-m', 'server.desktop_main'];
  args.push('--data-dir', dataDir, '--download-dir', path.join(app.getPath('downloads'), 'DouyinDownloader'));
  if (app.isPackaged) args.push('--model-dir', path.join(process.resourcesPath, 'ocr-v4'));
  token = randomBytes(32).toString('hex');
  const childEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(https?_proxy|all_proxy|no_proxy|DOUYIN_(COOKIE|PATH|PROXY|THREAD))$/i.test(key)));
  const child = spawn(exe, args, { cwd: dataDir, windowsHide: true,
    env: { ...childEnv, PYTHONPATH: app.isPackaged ? '' : repoRoot, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' } });
  sidecar = child;
  child.stdin.write(token + '\n');
  child.stderr.on('data', (chunk: Buffer) => {
    // 第三方模块可能输出完整媒体地址，只保留去敏后的诊断信息。
    const safe = chunk.toString().replace(/https?:\/\/\S+/g, '[URL]').replace(/(cookie|authorization|token)[=:][^\s]+/gi, '$1=[redacted]');
    void appendFile(path.join(dataDir, 'sidecar.log'), safe);
  });
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('下载服务启动超时')), 45000);
    const lines = createInterface({ input: child.stdout });
    lines.on('line', line => {
      try {
        const data = JSON.parse(line);
        if (typeof data.port === 'number' && data.port > 0 && data.port < 65536) {
          baseURL = `http://127.0.0.1:${data.port}`; clearTimeout(timer); lines.close(); resolve();
        }
      } catch { /* 第三方启动日志不属于握手。 */ }
    });
    child.once('error', error => { clearTimeout(timer); reject(error); });
    child.once('exit', () => { clearTimeout(timer); reject(Error('下载服务启动失败，请查看诊断日志')); });
  });
  child.on('exit', () => {
    if (sidecar !== child) return;
    baseURL = ''; socket?.close();
    if (!quitting) emit({ type: 'fatal', message: '下载服务已退出，未完成任务可在重启服务后继续' });
  });
  for (let attempt = 0; attempt < 100; attempt++) {
    try { await api('GET', '/api/v1/health'); break; }
    catch { if (attempt === 99) throw Error('下载服务未就绪'); await new Promise(r => setTimeout(r, 100)); }
  }
  currentSettings = await api('GET', '/api/v1/settings');
  await setProxy(currentSettings.proxy);
  knownPaths.add(path.resolve(currentSettings.path));
  connectSocket();
  emit({ type: 'ready' });
}

async function stopSidecar() {
  clearTimeout(reconnectTimer);
  const child = sidecar;
  sidecar = undefined; baseURL = '';
  socket?.close(); socket = undefined;
  if (!child || child.exitCode !== null) return;
  child.stdin.end();
  await new Promise<void>(resolve => {
    const timer = setTimeout(() => { child.kill(); resolve(); }, 10000);
    child.once('exit', () => { clearTimeout(timer); resolve(); });
  });
}

async function setProxy(proxy: string) {
  await session.fromPartition(partition).setProxy(proxy ? { mode: 'fixed_servers', proxyRules: proxy } : { mode: 'direct' });
  await session.fromPartition(partition).closeAllConnections();
}

function showBrowser(data: { visible: boolean; bounds?: Electron.Rectangle }) {
  browserVisible = data.visible;
  browser.setVisible(data.visible);
  if (data.bounds) {
    const [width, height] = window.getContentSize();
    browser.setBounds({ x: Math.max(0, Math.round(data.bounds.x)), y: Math.max(0, Math.round(data.bounds.y)),
      width: Math.max(1, Math.min(width, Math.round(data.bounds.width))),
      height: Math.max(1, Math.min(height, Math.round(data.bounds.height))) });
  }
}

function browserState() {
  const url = browser.webContents.getURL();
  context = contextFromURL(url);
  if (!context.author && context.current) context.author = browserItems.get(context.current)?.sec_uid || '';
  emit({ type: 'browser', url, ...context, items: [...browserItems.values()] });
}

async function detectVisibleContext() {
  if (!browserVisible || !baseURL || browser.webContents.isDestroyed()) return;
  if (contextFromURL(browser.webContents.getURL()).current) return;
  try {
    const ids: string[] = await browser.webContents.executeJavaScript(`(() => {
      const ids = new Set();
      for (const anchor of document.querySelectorAll('a[href]')) {
        const box = anchor.getBoundingClientRect();
        if (box.width < 20 || box.height < 20 || box.bottom < 0 || box.right < 0 || box.top > innerHeight || box.left > innerWidth) continue;
        const url = new URL(anchor.href, location.href);
        if (url.hostname !== 'www.douyin.com') continue;
        const id = /\\/(?:video|note)\\/(\\d+)/.exec(url.pathname)?.[1] || url.searchParams.get('modal_id');
        if (id) ids.add(id);
      }
      return [...ids];
    })()`);
    const current = ids.length === 1 && browserItems.has(ids[0]) ? ids[0] : '';
    context = { ...contextFromURL(browser.webContents.getURL()), current };
    if (current && !context.author) context.author = browserItems.get(current)?.sec_uid || '';
    emit({ type: 'browser', url: browser.webContents.getURL(), ...context, items: [...browserItems.values()] });
  } catch { /* 页面导航时上下文会销毁，下一次重新识别。 */ }
}

function attachCapture() {
  const contents = browser.webContents;
  const requests = new Map<string, number>();
  try { contents.debugger.attach('1.3'); } catch { return; }
  void contents.debugger.sendCommand('Network.enable');
  contents.debugger.on('message', async (_event, method, data) => {
    if (method === 'Network.responseReceived') {
      try {
        const url = new URL(data.response.url);
        if (url.hostname === 'www.douyin.com' && (allowedPaths.has(url.pathname) || /^\/aweme\/v\d+\/web\//.test(url.pathname))) {
          requests.set(data.requestId, captureGeneration);
          if (requests.size > 500) requests.delete(requests.keys().next().value!);
        }
      } catch { /* 忽略非页面数据。 */ }
    }
    if (method === 'Network.loadingFailed') requests.delete(data.requestId);
    if (method !== 'Network.loadingFinished' || !requests.has(data.requestId)) return;
    const generation = requests.get(data.requestId); requests.delete(data.requestId);
    try {
      const payload = await contents.debugger.sendCommand('Network.getResponseBody', { requestId: data.requestId });
      const text = payload.base64Encoded ? Buffer.from(payload.body, 'base64').toString() : payload.body;
      if (text.length > 10000000 || generation !== captureGeneration) return;
      const result = await api('POST', '/api/v1/capture', { text });
      if (generation !== captureGeneration) return;
      for (const item of result.items) browserItems.set(item.id, item);
      while (browserItems.size > 500) browserItems.delete(browserItems.keys().next().value!);
      browserState();
    } catch { /* 单条响应过期不清空已经识别的作品。 */ }
  });
  contents.debugger.on('detach', () => {
    if (!quitting && !contents.isDestroyed()) emit({ type: 'warning', message: '页面识别连接已断开，刷新页面后重试' });
  });
}

function protect(contents: Electron.WebContents) {
  contents.setWindowOpenHandler(({ url }) => {
    if (douyinURL(url)) void browser.webContents.loadURL(url);
    return { action: 'deny' };
  });
  contents.on('will-navigate', (event, url) => { if (!douyinURL(url)) event.preventDefault(); });
}

async function permittedPath(value: string) {
  if (typeof value !== 'string' || !knownPaths.has(path.resolve(value))) throw Error('只能打开任务或作品记录中的文件');
  return realpath(value);
}

function registerIPC() {
  ipcMain.handle('api', async (event, method: string, route: string, body?: unknown) => {
    trustSender(event);
    if (!apiAllowed(method, route)) throw Error('不支持此操作');
    if (method === 'POST' && route === '/api/v1/jobs') await syncCookies();
    const result = await api(method, route, body);
    if (method === 'PUT' && route === '/api/v1/settings') {
      await setProxy(result.proxy); currentSettings = result; knownPaths.add(path.resolve(result.path));
    }
    return result;
  });
  ipcMain.handle('browser', async (event, action: string, data: any) => {
    trustSender(event);
    const contents = browser.webContents;
    if (action === 'layout') return showBrowser(data);
    if (action === 'state') { browserState(); return; }
    if (action === 'navigate') {
      if (!douyinURL(data)) throw Error('仅支持抖音网页地址');
      await contents.loadURL(data); return;
    }
    if (action === 'back' && contents.navigationHistory.canGoBack()) contents.navigationHistory.goBack();
    else if (action === 'forward' && contents.navigationHistory.canGoForward()) contents.navigationHistory.goForward();
    else if (action === 'reload') { if (!contents.debugger.isAttached()) attachCapture(); contents.reload(); }
    else if (action === 'verify') { bridgeWindow.show(); bridgeWindow.focus(); }
    else if (action === 'logout') {
      await api('POST', '/api/v1/session', { cookies: {} });
      await session.fromPartition(partition).clearStorageData();
      browserItems.clear(); await contents.loadURL('https://www.douyin.com/');
      await bridgeWindow.loadURL('https://www.douyin.com/');
    }
  });
  ipcMain.handle('choose-directory', async event => {
    trustSender(event);
    const result = await dialog.showOpenDialog(window, { properties: ['openDirectory', 'createDirectory'] });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle('open-path', async (event, value: string) => {
    trustSender(event); const result = await shell.openPath(await permittedPath(value));
    if (result) throw Error(result);
  });
  ipcMain.handle('preview', async (event, value: string) => {
    trustSender(event); const actual = await permittedPath(value);
    const key = randomBytes(24).toString('hex'); mediaTokens.set(key, actual);
    if (mediaTokens.size > 100) mediaTokens.delete(mediaTokens.keys().next().value!);
    return `local-media://file/${key}`;
  });
  ipcMain.handle('read-report', async (event, value: string) => {
    trustSender(event); const actual = await permittedPath(value);
    if (!actual.endsWith('.json')) throw Error('仅支持读取处理报告');
    const text = await readFile(actual, 'utf8');
    return JSON.parse(text);
  });
  ipcMain.handle('restart', async event => {
    trustSender(event); await stopSidecar(); await startSidecar();
  });
  ipcMain.handle('diagnostics', async event => {
    trustSender(event);
    const checks = [{ name: '下载服务', ok: !!baseURL, detail: baseURL ? '已连接' : '未连接' },
      { name: '页面桥接', ok: socket?.readyState === WebSocket.OPEN, detail: bridgeWindow.webContents.getURL() },
      { name: '网络模式', ok: true, detail: currentSettings.proxy || '直连（含系统 TUN）' }];
    try {
      const response = await session.fromPartition(partition).fetch('https://www.douyin.com/', { signal: AbortSignal.timeout(10000) });
      checks.push({ name: '抖音网页', ok: response.ok, detail: `HTTP ${response.status}` });
      await response.body?.cancel();
    } catch { checks.push({ name: '抖音网页', ok: false, detail: '连接失败，请检查代理设置' }); }
    try { checks.push(...await api('GET', '/api/v1/diagnostics')); }
    catch { checks.push({ name: 'API / CDN', ok: false, detail: '下载服务无法完成检测' }); }
    return checks;
  });
  ipcMain.handle('import-data', async event => {
    trustSender(event);
    const result = await dialog.showOpenDialog(window, { properties: ['openFile'], filters: [{ name: 'CLI 配置', extensions: ['yml', 'yaml'] }] });
    if (result.canceled) return null;
    return api('POST', '/api/v1/import', { path: result.filePaths[0] });
  });
}

app.on('window-all-closed', () => app.quit());
app.on('before-quit', event => {
  if (quitting) return;
  event.preventDefault(); quitting = true;
  clearTimeout(cookieTimer);
  clearInterval(contextTimer);
  void stopSidecar().finally(() => app.quit());
});
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (window) { window.show(); window.focus(); } });
  void app.whenReady().then(async () => {
    protocol.handle('app', request => {
      const url = new URL(request.url);
      const root = path.join(desktopRoot, 'out/renderer');
      const target = path.resolve(root, '.' + decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname));
      if (url.hostname !== 'ui' || !target.startsWith(root + path.sep)) return new Response('', { status: 403 });
      return net.fetch(pathToFileURL(target).href);
    });
    protocol.handle('local-media', request => {
      const target = mediaTokens.get(new URL(request.url).pathname.slice(1));
      if (!target) return new Response('', { status: 404 });
      return net.fetch(pathToFileURL(target).href, { headers: request.headers });
    });
    window = new BrowserWindow({ width: 1400, height: 900, minWidth: 1080, minHeight: 720,
      backgroundColor: '#11151b', title: 'Douyin Downloader',
      webPreferences: { preload: path.join(desktopRoot, 'electron/preload.cjs'), nodeIntegration: false, contextIsolation: true, sandbox: true } });
    window.on('closed', () => { if (!quitting) app.quit(); });
    browser = new WebContentsView({ webPreferences: { partition, nodeIntegration: false, contextIsolation: true, sandbox: true } });
    window.contentView.addChildView(browser); browser.setVisible(false);
    bridgeWindow = new BrowserWindow({ width: 1100, height: 800, show: false, title: '抖音登录与验证',
      webPreferences: { partition, nodeIntegration: false, contextIsolation: true, sandbox: true } });
    bridgeWindow.on('close', event => { if (!quitting) { event.preventDefault(); bridgeWindow.hide(); } });
    protect(browser.webContents); protect(bridgeWindow.webContents);
    session.fromPartition(partition).setPermissionRequestHandler((_wc, permission, callback) => callback(permission === 'fullscreen'));
    session.fromPartition(partition).cookies.on('changed', () => {
      clearTimeout(cookieTimer); cookieTimer = setTimeout(() => { void syncCookies().catch(() => {}); }, 500);
    });
    browser.webContents.on('did-start-navigation', (_e, _url, isInPlace, mainFrame) => {
      if (mainFrame && !isInPlace) { captureGeneration++; browserItems.clear(); }
    });
    browser.webContents.on('did-navigate', browserState);
    browser.webContents.on('did-navigate-in-page', (_event, url, mainFrame) => {
      if (mainFrame) {
        const value = new URL(url); value.searchParams.delete('modal_id');
        const key = value.pathname + value.search;
        if (contentPageKey && key !== contentPageKey) { browserItems.clear(); captureGeneration++; }
        contentPageKey = key;
        browserState();
      }
    });
    browser.webContents.on('did-finish-load', browserState);
    registerIPC();
    await window.loadURL('app://ui/');
    try {
      await startSidecar();
      attachCapture();
      contextTimer = setInterval(() => { void detectVisibleContext(); }, 2000);
      if (!testMode) {
        void browser.webContents.loadURL('https://www.douyin.com/').catch(() => emit({ type: 'warning', message: '抖音页面加载失败，请检查网络设置' }));
        void bridgeWindow.loadURL('https://www.douyin.com/').catch(() => {});
      }
    } catch (error) { emit({ type: 'fatal', message: String(error) }); }
  });
}
