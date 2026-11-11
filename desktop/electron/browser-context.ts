// 此函数整体在远程页面执行，不访问 Node 或应用 IPC。
function inspectPage(includePayloads = true) {
  const validID = (value: unknown): string => typeof value === 'string' && /^\d{5,30}$/.test(value) ? value : '';
  const linkID = (value: string): string => {
    try {
      const url = new URL(value, location.href);
      if (url.hostname !== 'www.douyin.com') return '';
      return validID(/\/(?:video|note|slides|gallery)\/(\d+)/.exec(url.pathname)?.[1] || url.searchParams.get('modal_id'));
    } catch { return ''; }
  };
  const visibleArea = (element: Element) => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) return 0;
    const width = Math.max(0, Math.min(rect.right, innerWidth) - Math.max(rect.left, 0));
    const height = Math.max(0, Math.min(rect.bottom, innerHeight) - Math.max(rect.top, 0));
    return width >= 80 && height >= 80 ? width * height : 0;
  };
  const videos = [...document.querySelectorAll('video')].filter(video => visibleArea(video) > 0);
  const playing = videos.filter(video => !video.paused && !video.ended);
  const candidates = playing.length ? playing : videos;
  // 同屏存在多个播放区域时不凭列表顺序、面积或网络响应顺序猜测。
  const video = candidates.length === 1 ? candidates[0] : undefined;
  const ids = new Set<string>();
  const links = new Set<string>();
  const items: Record<string, unknown>[] = [];
  if (video) {
    const anchor = video.closest<HTMLAnchorElement>('a[href]');
    const linkedID = anchor ? linkID(anchor.href) : '';
    if (linkedID) links.add(linkedID);
    let element: Element | null = video;
    for (let level = 0; element && level < 9; level++, element = element.parentElement) {
      if (element === document.body || element === document.documentElement || element.querySelectorAll('video').length > 1) break;
      for (const name of ['data-aweme-id', 'data-item-id']) {
        const id = validID(element.getAttribute(name));
        if (id) ids.add(id);
      }
      // 只读播放器所在分支的 props，不遍历 React 的子树、兄弟或全局状态。
      const seen = new WeakSet<object>();
      let budget = 600;
      const walk = (value: any, depth = 0) => {
        if (!value || typeof value !== 'object' || value instanceof Node || seen.has(value) || depth > 5 || --budget < 0) return;
        seen.add(value);
        const id = validID(value.aweme_id || value.awemeId);
        if (id && (value.video || value.images)) {
          ids.add(id);
          // 只传输下载缓存识别所需字段，避开 React 对象中的循环引用。
          if (value.aweme_id) {
            try {
              items.push(JSON.parse(JSON.stringify({aweme_id:id, desc:value.desc, author:value.author,
                video:value.video, images:value.images, create_time:value.create_time, aweme_type:value.aweme_type})));
            } catch { /* 循环结构只用于确定标识，不写入下载缓存。 */ }
          }
          return;
        }
        if (Array.isArray(value)) return;
        for (const key of Object.keys(value)) {
          if (['children', 'return', 'sibling', 'alternate', 'ref'].includes(key)) continue;
          walk(value[key], depth + 1);
        }
      };
      for (const key of Object.keys(element)) {
        if (key.startsWith('__reactProps$')) walk((element as any)[key]);
        if (key.startsWith('__reactFiber$')) {
          let fiber = (element as any)[key];
          for (let count = 0; fiber && count < 12; count++, fiber = fiber.return) {
            const host = fiber.stateNode;
            if (host instanceof Element && (host === document.body || host === document.documentElement || host.querySelectorAll('video').length > 1)) break;
            walk(fiber.memoizedProps);
            if (ids.size) break;
          }
        }
      }
      // 最近的播放器数据优先，避免上级容器同时包含推荐作品。
      if (ids.size) break;
    }
  }
  const payloads: string[] = [];
  let size = 0;
  for (const script of includePayloads ? document.querySelectorAll('script[type="application/json"], script#RENDER_DATA, script#__NEXT_DATA__') : []) {
    let value = script.textContent || '';
    if (script.id === 'RENDER_DATA') {
      try { value = decodeURIComponent(value); } catch { continue; }
    }
    if (!value || value.length + size > 10000000 || payloads.length >= 12) continue;
    size += value.length;
    payloads.push(value);
  }
  // currentSrc 在 load()/切换 src 后可能暂留上一条地址，以当前声明的资源为准。
  const sources = video ? (video.getAttribute('src') ? [video.src]
    : [...video.querySelectorAll<HTMLSourceElement>('source[src]')].map(source => source.src)) : [];
  return {url: location.href, ids: [...ids], links: [...links], sources, items, payloads};
}

export type PageSnapshot = ReturnType<typeof inspectPage>;
export const inspectPageScript = `(${inspectPage.toString()})()`;
export const currentPlayerScript = `(${inspectPage.toString()})(false)`;

// 只对完整媒体地址建立映射，不用域名或部分路径猜作品。
function mediaKey(value: string) {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol)) return '';
    // /play/ 等接口靠查询参数区分作品，不能删除这些参数。
    return url.origin + url.pathname + url.search;
  } catch { return ''; }
}

export class MediaIndex {
  private entries = new Map<string, Set<string>>();

  clear() { this.entries.clear(); }

  ingest(text: string) {
    let value: unknown;
    try { value = JSON.parse(text); } catch { return; }
    let budget = 20000;
    const walk = (node: any, depth = 0) => {
      if (!node || typeof node !== 'object' || depth > 12 || --budget < 0) return;
      if (typeof node.aweme_id === 'string' && /^\d{5,30}$/.test(node.aweme_id) && node.video) {
        const urls = new Set<string>();
        let mediaBudget = 1000;
        const scan = (part: any, level = 0) => {
          if (level > 8 || --mediaBudget < 0) return;
          if (typeof part === 'string') { const key = mediaKey(part); if (key) urls.add(key); }
          else if (Array.isArray(part)) part.slice(0, 100).forEach(x => scan(x, level + 1));
          else if (part && typeof part === 'object') Object.values(part).forEach(x => scan(x, level + 1));
        };
        scan(node.video);
        this.entries.set(node.aweme_id, urls);
        while (this.entries.size > 500) this.entries.delete(this.entries.keys().next().value!);
        return;
      }
      for (const child of (Array.isArray(node) ? node.slice(0, 500) : Object.values(node))) walk(child, depth + 1);
    };
    walk(value);
  }

  current(snapshot: PageSnapshot): string {
    if (snapshot.ids.length) return snapshot.ids.length === 1 ? snapshot.ids[0] : '';
    const matched = new Set<string>();
    for (const source of snapshot.sources) {
      const key = mediaKey(source);
      if (key) for (const [id, urls] of this.entries) if (urls.has(key)) matched.add(id);
    }
    if (matched.size) return matched.size === 1 ? [...matched][0] : '';
    return snapshot.links.length === 1 ? snapshot.links[0] : '';
  }
}
