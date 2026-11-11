export const allowedPaths = new Set([
  'aweme/detail', 'aweme/post', 'aweme/favorite', 'aweme/listcollection',
  'collects/list', 'collects/video/list', 'mix/listcollection', 'mix/detail',
  'mix/aweme', 'mix/list', 'series/list', 'user/profile/self', 'user/profile/other',
].map(p => `/aweme/v1/web/${p}/`));

export function douyinURL(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password && !url.port &&
      (url.hostname === 'douyin.com' || url.hostname.endsWith('.douyin.com'));
  } catch { return false; }
}

export function contextFromURL(value: string) {
  try {
    if (!douyinURL(value)) return { current: '', author: '', mix: '' };
    const url = new URL(value);
    const candidate = /\/(?:video|note|slides|gallery)\/(\d+)/.exec(url.pathname)?.[1]
      || url.searchParams.get('modal_id') || '';
    const current = /^\d{5,30}$/.test(candidate) ? candidate : '';
    return { current, author: /\/user\/([\w-]+)/.exec(url.pathname)?.[1] || '',
      mix: /\/(?:collection|mix)\/(\d+)/.exec(url.pathname)?.[1] || '' };
  } catch { return { current: '', author: '', mix: '' }; }
}

export function apiAllowed(method: string, route: string): boolean {
  if (!route.startsWith('/api/v1/') || route.includes('..') || route.includes('://')) return false;
  const path = route.split('?')[0];
  const read = ['/api/v1/health', '/api/v1/settings', '/api/v1/jobs', '/api/v1/library', '/api/v1/me'];
  return (method === 'GET' && (read.includes(path) || /^\/api\/v1\/jobs\/[a-f0-9]+$/.test(path))) ||
    (method === 'PUT' && path === '/api/v1/settings') ||
    (method === 'POST' && (['/api/v1/jobs', '/api/v1/content', '/api/v1/download'].includes(path) ||
      /^\/api\/v1\/jobs\/[a-f0-9]+\/(cancel|resume|retry)$/.test(path)));
}

export function pageScript(request: {path: string; params: Record<string, unknown>; method: string; data?: Record<string, unknown>; content_type?: string}) {
  if (!allowedPaths.has(request.path) || !['GET', 'POST'].includes(request.method)) throw Error('页面请求被拒绝');
  // 所有动态值只作为 JSON 数据注入，禁止拼接执行网页提供的代码。
  return `(async () => {
    const r = ${JSON.stringify(request)};
    const url = new URL(r.path, 'https://www.douyin.com');
    for (const [key, value] of Object.entries(r.params || {})) url.searchParams.set(key, String(value));
    url.searchParams.set('browser_platform', navigator.platform);
    url.searchParams.set('screen_width', String(screen.width));
    url.searchParams.set('screen_height', String(screen.height));
    url.searchParams.set('browser_language', navigator.language);
    const abort = new AbortController(); const timer = setTimeout(() => abort.abort(), 35000);
    try {
      const response = await window.fetch(url.href, {method:r.method, credentials:'include', signal:abort.signal,
        headers:r.method === 'POST' ? {'Content-Type':'application/x-www-form-urlencoded'} : {},
        body:r.method === 'POST' ? new URLSearchParams(r.data || {}).toString() : undefined});
      const text = await response.text();
      if (text.length > 10000000) throw Error('页面响应过大');
      return {status:response.status,text};
    } finally {clearTimeout(timer);}
  })()`;
}
