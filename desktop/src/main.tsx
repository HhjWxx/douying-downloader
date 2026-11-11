import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { Item, Job, Options, Settings, Target } from './types';
import { TaskProgress } from './TaskProgress';
import './style.css';

const api = window.desktop;
const labels: Record<string, string> = { pending: '排队中', running: '处理中', waiting: '等待处理', interrupted: '已中断', cancelled: '已取消', success: '已完成', partial: '部分完成', failed: '失败', skipped: '已存在', disabled: '未启用', not_matched: '未发现目标水印', unsupported: '暂不支持' };
const targetLabels: Record<string, string> = { items: '选中作品', author: '作者作品', mix: '合集', collection: '我的收藏', likes: '我的喜欢', url: '链接下载' };
const defaults: Options = { limit: 20, date_from: null, date_to: null, media_type: 'all', quality: 'highest', cleanup: true };
const time = (value: number | string) => new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN', { hour12: false });

function Icon({ name }: { name: string }) {
  const shapes: Record<string, React.ReactNode> = {
    browse: <><circle cx="12" cy="12" r="9"/><path d="m16 8-3 5-5 3 3-5Z"/></>,
    content: <><path d="M4 6h16v14H4zM8 3h8"/><path d="m8 11 4 4 4-4"/></>,
    tasks: <><path d="M8 6h13M8 12h13M8 18h13"/><path d="m2 6 1 1 2-2m-3 7 1 1 2-2m-3 7 1 1 2-2"/></>,
    library: <><path d="M3 7h7l2-3h9v16H3Z"/><path d="m10 10 5 3-5 3Z"/></>,
    settings: <><circle cx="12" cy="12" r="4"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2"/></>,
  };
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{shapes[name]}</svg>;
}

function App() {
  const [tab, setTab] = useState('browse');
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState('正在启动本地下载服务…');
  const [fatal, setFatal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [logged, setLogged] = useState(false);
  const [account, setAccount] = useState('');
  const [address, setAddress] = useState('https://www.douyin.com/');
  const [browserState, setBrowserState] = useState({ current: '', author: '', mix: '', items: [] as Item[] });
  const [selected, setSelected] = useState(new Set<string>());
  const [jobs, setJobs] = useState<Job[]>([]);
  const [settings, setSettings] = useState<Settings>({ path: '', proxy: '', thread: 2, cleanup: true });
  const [target, setTarget] = useState<Target | null>(null);
  const [options, setOptions] = useState<Options>(defaults);
  const [contentTarget, setContentTarget] = useState<Target>({ kind: 'collection' });
  const [contentItems, setContentItems] = useState<Item[]>([]);
  const [cursor, setCursor] = useState('0');
  const [hasMore, setHasMore] = useState(false);
  const [library, setLibrary] = useState<any>({ items: [], total: 0, page: 1 });
  const [search, setSearch] = useState('');
  const [author, setAuthor] = useState('');
  const [libraryFrom, setLibraryFrom] = useState('');
  const [libraryTo, setLibraryTo] = useState('');
  const [preview, setPreview] = useState<{url: string; image: boolean} | null>(null);
  const [report, setReport] = useState<any>(null);
  const [diagnostics, setDiagnostics] = useState<any[]>([]);
  const [expanded, setExpanded] = useState('');
  const [link, setLink] = useState('');
  const slot = useRef<HTMLDivElement>(null);
  const pageVersion = useRef(0);

  const attempt = useCallback(async (fn: () => Promise<unknown>) => {
    try { return await fn(); } catch (error) { setNotice(String(error).replace(/^Error: /, '')); }
  }, []);
  const refreshJobs = useCallback(async () => { const data = await api.api('GET', '/api/v1/jobs'); setJobs(data.jobs); }, []);
  const initialize = useCallback(async () => {
    const data = await api.api('GET', '/api/v1/settings');
    setSettings(data); setOptions(o => ({ ...o, cleanup: data.cleanup }));
    await refreshJobs(); setReady(true); setFatal(false); setNotice('');
    await api.browser('state');
  }, [refreshJobs]);

  useEffect(() => {
    const unsubscribe = api.onEvent(event => {
      if (event.type === 'ready' || event.type === 'connected') void attempt(initialize);
      if (event.type === 'fatal') { setFatal(true); setReady(false); setNotice(event.message); }
      if (event.type === 'warning') setNotice(event.message);
      if (event.type === 'session') setLogged(event.logged_in);
      if (event.type === 'browser') { setBrowserState(event); setAddress(event.url || 'https://www.douyin.com/'); }
      if (event.type === 'job') setJobs(old => [event.job, ...old.filter(x => x.job_id !== event.job.job_id)].sort((a, b) => b.created_at.localeCompare(a.created_at)));
      if (event.type === 'connection' && !event.connected) setNotice('正在恢复与下载服务的连接…');
    });
    void api.api('GET', '/api/v1/health').then(initialize).catch(() => {});
    return unsubscribe;
  }, [attempt, initialize]);

  useEffect(() => {
    const update = () => {
      const rect = slot.current?.getBoundingClientRect();
      void api.browser('layout', { visible: tab === 'browse' && !target && !preview && !report && ready,
        bounds: rect ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height } : undefined }).catch(() => {});
    };
    update(); window.addEventListener('resize', update);
    const observer = new ResizeObserver(update); if (slot.current) observer.observe(slot.current);
    return () => { window.removeEventListener('resize', update); observer.disconnect(); };
  }, [tab, target, preview, report, ready]);

  useEffect(() => { setSelected(new Set()); }, [tab]);
  useEffect(() => {
    if (tab === 'browse') {
      const visible = new Set(browserState.items.map(x => x.id));
      setSelected(old => new Set([...old].filter(id => visible.has(id))));
    }
  }, [browserState.items, tab]);

  const openTarget = (value: Target) => { setTarget(value); setOptions({ ...defaults, cleanup: settings.cleanup }); };
  const toggle = (id: string) => setSelected(old => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const submit = async () => {
    if (!target) return;
    setBusy(true);
    await attempt(async () => {
      await api.api('POST', '/api/v1/jobs', { target, options });
      setTarget(null); setTab('tasks'); setNotice('任务已加入队列'); await refreshJobs();
    }); setBusy(false);
  };
  const loadContent = async (value: Target, append = false) => {
    const version = ++pageVersion.current;
    setBusy(true); setContentTarget(value);
    if (!append) { setContentItems([]); setSelected(new Set()); setHasMore(false); setCursor('0'); }
    await attempt(async () => {
      const data = await api.api('POST', '/api/v1/content', { target: value, cursor: append ? cursor : '0' });
      if (version !== pageVersion.current) return;
      setContentItems(old => [...new Map((append ? [...old, ...data.items] : data.items).map((x: Item) => [x.id, x])).values()] as Item[]);
      setCursor(data.cursor); setHasMore(data.has_more);
    }); if (version === pageVersion.current) setBusy(false);
  };
  const loadLibrary = async (page = 1) => {
    const query = new URLSearchParams({ page: String(page), title: search, author });
    if (libraryFrom) query.set('date_from', String(new Date(libraryFrom + 'T00:00:00').getTime() / 1000));
    if (libraryTo) query.set('date_to', String(new Date(libraryTo + 'T23:59:59').getTime() / 1000));
    await attempt(async () => setLibrary(await api.api('GET', '/api/v1/library?' + query)));
  };
  const changeTab = (value: string) => { setTab(value); if (value === 'library') void loadLibrary(); if (value === 'tasks') void attempt(refreshJobs); };
  const previewFile = async (file: string) => attempt(async () => {
    setPreview({ url: await api.preview(file), image: /\.(jpe?g|png|webp)$/i.test(file) });
  });

  const cards = (items: Item[], folders = false) => <div className="cards">{items.map(item => <article key={item.id} className={'card ' + (selected.has(item.id) ? 'selected' : '')}>
    <button className="card-cover" onClick={() => folders ? void loadContent({ kind: 'collection', id: item.id }) : toggle(item.id)} aria-label={folders ? `打开${item.title}` : `选择${item.title}`}>
      {item.cover ? <img src={item.cover} alt="" loading="lazy" onError={e => { e.currentTarget.style.display = 'none'; }}/> : <span className="placeholder">{folders ? '收藏夹' : item.media_type === 'gallery' ? '图文' : '视频'}</span>}
      {!folders && <span className={'check ' + (selected.has(item.id) ? 'checked' : '')}>{selected.has(item.id) ? '✓' : ''}</span>}
      <span className="media-label">{folders ? '查看内容' : item.media_type === 'gallery' ? '图文' : '视频'}</span>
    </button><div className="card-body"><strong title={item.title}>{item.title}</strong><small>{item.author || ''}</small>
      {item.created ? <small>{new Date(item.created * 1000).toLocaleDateString()}</small> : null}
      {!folders && <button className="text-button" onClick={() => openTarget({ kind: 'items', ids: [item.id] })}>下载作品 ↗</button>}
    </div></article>)}</div>;

  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><div className="brand-icon">↓</div><div><b>Douyin</b><small>下载工作台</small></div></div>
      <div className="section-label">工作空间</div>
      <nav>{[['browse', '浏览抖音'], ['content', '我的内容'], ['tasks', '下载任务'], ['library', '本地作品']].map(([id, title]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => changeTab(id)}><Icon name={id}/>{title}{id === 'tasks' && jobs.some(x => x.status === 'running') && <i className="dot"/>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="account"><span className={'status-dot ' + (logged ? 'online' : '')}/><div><b>{account || (logged ? '抖音已登录' : '尚未登录抖音')}</b><small>会话保存在本机</small></div></div>
      <button className={tab === 'settings' ? 'active' : ''} onClick={() => changeTab('settings')}><Icon name="settings"/>设置与诊断</button><small className="version">DOUYIN DOWNLOADER · {import.meta.env.VITE_APP_VERSION}</small></div>
    </aside>
    <main><header className="topbar"><span>工作空间 <span className="slash">/</span> {{ browse: '浏览抖音', content: '我的内容', tasks: '下载任务', library: '本地作品', settings: '设置与诊断' }[tab]}</span><span className="connection"><i className={'status-dot ' + (ready ? 'online' : '')}/>{ready ? '本地服务已连接' : '服务未就绪'}</span></header>
    {notice && <div className={'notice ' + (fatal ? 'error' : '')}><span>{notice}</span>{fatal ? <button onClick={() => void attempt(async () => { setNotice('正在重启服务…'); await api.restart(); })}>重启服务</button> : <button aria-label="关闭提示" onClick={() => setNotice('')}>×</button>}</div>}
    <div className={'workspace ' + (tab === 'browse' ? 'browser-workspace' : '')}>
      {tab === 'browse' && <>
        <div className="page-heading"><div><span className="eyebrow">发现与下载</span><h1>在抖音里发现，在这里保存。</h1><p>正常浏览视频，选择作品或作者，即可加入下载队列。</p></div><button onClick={() => void attempt(() => api.browser('verify'))}>登录 / 验证窗口</button></div>
        <form className="addressbar" onSubmit={e => { e.preventDefault(); void attempt(() => api.browser('navigate', address)); }}><button type="button" aria-label="后退" onClick={() => void attempt(() => api.browser('back'))}>←</button><button type="button" aria-label="前进" onClick={() => void attempt(() => api.browser('forward'))}>→</button><button type="button" aria-label="刷新" onClick={() => void attempt(() => api.browser('reload'))}>↻</button><input aria-label="抖音网址" value={address} onChange={e => setAddress(e.target.value)}/><button type="submit">打开</button></form>
        <div className="browser-tools"><button className="primary" disabled={!ready} onClick={() => browserState.current ? openTarget({ kind: 'items', ids: [browserState.current] }) : setNotice('当前页面未提供唯一作品标识，请从右侧已识别列表选择作品')}>↓ 下载当前视频</button>
          <button disabled={!browserState.author || browserState.author === 'self'} onClick={() => openTarget({ kind: 'author', id: browserState.author })}>下载当前作者</button>
          {browserState.mix && <button onClick={() => openTarget({ kind: 'mix', id: browserState.mix })}>下载当前合集</button>}
          <span className="muted">原片保留 · 自动处理</span></div>
        <div className="browser-grid"><div className="browser-slot" ref={slot}><div className="empty"><Icon name="browse"/><p>{ready ? '正在加载抖音页面' : '等待本地服务启动'}</p></div></div>
          <aside className="capture-panel"><div className="panel-title"><b>页面中的作品</b><span>{browserState.items.length}</span></div><p className="muted">仅包含已加载内容</p>
            <button disabled={!selected.size} className="primary full" onClick={() => openTarget({ kind: 'items', ids: [...selected] })}>下载选中 · {selected.size}</button>
            <div className="capture-list">{browserState.items.map(item => <button key={item.id} className={'capture-item ' + (selected.has(item.id) ? 'selected' : '')} onClick={() => toggle(item.id)}><span className={'check ' + (selected.has(item.id) ? 'checked' : '')}>{selected.has(item.id) ? '✓' : ''}</span><span><b>{item.title}</b><small>{item.author}</small></span></button>)}{!browserState.items.length && <div className="empty compact"><p>浏览或刷新页面后<br/>识别到的作品会显示在这里</p></div>}</div>
          </aside></div>
      </>}
      {tab === 'content' && <>
        <div className="page-heading"><div><span className="eyebrow">我的收藏与喜欢</span><h1>值得留下的作品。</h1><p>读取当前登录账号可以访问的内容，先预览再选择。</p></div><button onClick={() => void attempt(async () => { const me = await api.api('GET', '/api/v1/me'); setAccount(me.nickname); setNotice(`当前账号：${me.nickname}`); })}>确认当前账号</button></div>
        <div className="toolbar"><div className="segmented">{[['collection', '全部收藏'], ['folders', '收藏夹'], ['likes', '喜欢']].map(([kind, title]) => <button disabled={busy} className={contentTarget.kind === kind && !contentTarget.id ? 'active' : ''} key={kind} onClick={() => void loadContent({ kind: kind as Target['kind'] })}>{title}</button>)}</div><button disabled={busy} onClick={() => void loadContent(contentTarget)}>刷新列表</button>
          {contentTarget.kind !== 'folders' && <button onClick={() => openTarget(contentTarget)}>批量下载此列表</button>}</div>
        {contentTarget.kind !== 'folders' && <div className="selection-toolbar"><button onClick={() => setSelected(new Set(contentItems.map(x => x.id)))}>选择已加载</button><button onClick={() => setSelected(new Set())}>清空选择</button><span>{contentItems.length} 条已加载</span><button className="primary" disabled={!selected.size} onClick={() => openTarget({ kind: 'items', ids: [...selected] })}>下载选中 · {selected.size}</button></div>}
        {cards(contentItems, contentTarget.kind === 'folders')}
        {!contentItems.length && <div className="empty"><Icon name="content"/><h3>{busy ? '正在读取内容…' : '选择上方列表开始读取'}</h3><p>首次使用请先在“浏览抖音”中完成登录。</p></div>}
        {hasMore && <button className="load-more" disabled={busy} onClick={() => void loadContent(contentTarget, true)}>{busy ? '读取中…' : '加载更多'}</button>}
      </>}
      {tab === 'tasks' && <>
        <div className="page-heading"><div><span className="eyebrow">下载与自动处理</span><h1>下载任务</h1><p>逐项记录下载结果。应用退出后，可以继续未完成的任务。</p></div><span className="count-pill">{jobs.length} 个任务</span></div>
        <form className="link-form" onSubmit={e => { e.preventDefault(); if (link.trim()) openTarget({ kind: 'url', url: link.trim() }); }}><input aria-label="下载链接" placeholder="也可以粘贴视频、作者或合集链接" value={link} onChange={e => setLink(e.target.value)}/><button className="primary" disabled={!ready}>添加链接任务</button></form>
        <div className="jobs">{jobs.map(job => {
          const completed = Object.keys(job.results).length; const active = ['pending', 'running'].includes(job.status);
          return <article className="job" key={job.job_id}><div className="job-top"><div><span className={'badge ' + job.status}>{labels[job.status]}</span><b title={job.title}>{job.title || targetLabels[job.target.kind] || job.target.kind}</b><small>{job.authors?.join("、")} {time(job.created_at)}</small></div><div className="job-actions">{active ? <button onClick={() => void attempt(async () => { await api.api('POST', `/api/v1/jobs/${job.job_id}/cancel`); await refreshJobs(); })}>取消</button> : !['success'].includes(job.status) && <button onClick={() => void attempt(async () => { await api.api('POST', `/api/v1/jobs/${job.job_id}/resume`); await refreshJobs(); })}>继续未完成项</button>}
            {job.output_dirs[0] && <button onClick={() => void attempt(() => api.openPath(job.output_dirs[0]))}>打开目录</button>}<button onClick={() => setExpanded(expanded === job.job_id ? '' : job.job_id)}>{expanded === job.job_id ? '收起' : '详情'}</button></div></div>
            <div className="job-progress"><div><span style={{ width: `${job.items.length ? completed / job.items.length * 100 : 0}%` }}/></div><small>{completed} / {job.items.length} 项 · {job.listing_complete ? '列表已取得' : '正在读取或等待继续读取列表'}</small></div>
            {job.error && <p className="job-error">{job.error}</p>}
            <TaskProgress job={job}/>
            {expanded === job.job_id && <div className="job-details">{Object.entries(job.results).map(([key, value]) => <div className="result-row" key={key}><b>{value.title || key}</b><span className={'badge ' + value.status}>{labels[value.status]}</span><span>{value.reason}</span>{value.cleanup?.map((clean: any, index: number) => <div className="cleanup-row" key={index}><span>自动处理：{labels[clean.status] || clean.status}</span>{clean.reason && <p>{clean.reason}</p>}<div>{clean.source && <button onClick={() => void attempt(() => api.openPath(clean.source))}>原片</button>}{clean.output && <button onClick={() => void previewFile(clean.output)}>处理副本</button>}{clean.report && <button onClick={() => void attempt(async () => setReport(await api.readReport(clean.report)))}>报告</button>}</div></div>)}</div>)}</div>}
          </article>;
        })}</div>{!jobs.length && <div className="empty"><Icon name="tasks"/><h3>还没有下载任务</h3><p>在“浏览抖音”中选择作品，或在这里添加链接。</p></div>}
      </>}
      {tab === 'library' && <>
        <div className="page-heading"><div><span className="eyebrow">保存在本机</span><h1>本地作品</h1><p>原片始终保留，处理结果和报告随时可查看。</p></div><span className="count-pill">{library.total} 件作品</span></div>
        <form className="library-filter" onSubmit={e => { e.preventDefault(); void loadLibrary(); }}><input placeholder="搜索标题" aria-label="搜索标题" value={search} onChange={e => setSearch(e.target.value)}/><input placeholder="作者" aria-label="作者" value={author} onChange={e => setAuthor(e.target.value)}/><input aria-label="开始日期" type="date" value={libraryFrom} onChange={e => setLibraryFrom(e.target.value)}/><input aria-label="结束日期" type="date" value={libraryTo} onChange={e => setLibraryTo(e.target.value)}/><button>筛选</button></form>
        <div className="library-list">{library.items.map((item: any) => <article className="library-row" key={item.aweme_id}><div className="library-thumbnail">{item.cover_urls?.[0] ? <img alt="" src={item.cover_urls[0]} loading="lazy"/> : <Icon name="library"/>}</div><div className="library-title"><b>{item.title}</b><small>{item.author_name} · {time(item.create_time)}</small><small>{item.file_path}</small><div className="file-actions">{item.assets?.map((asset: any) => <button key={asset.file_path} onClick={() => void previewFile(asset.file_path)}>{asset.kind === 'image' ? '图片' : '视频'}预览</button>)}</div></div><div className="library-actions"><button onClick={() => void attempt(() => api.openPath(item.file_path))}>打开目录</button>{item.clean_path && <button onClick={() => void previewFile(item.clean_path)}>处理副本</button>}{item.report_path && <button onClick={() => void attempt(async () => setReport(await api.readReport(item.report_path)))}>处理报告</button>}</div></article>)}</div>
        {!library.items.length && <div className="empty"><Icon name="library"/><h3>暂无本地作品</h3><p>下载完成后会自动归档，也可以在设置中导入已有 CLI 历史。</p></div>}
        {library.total > 40 && <div className="pagination"><button disabled={library.page <= 1} onClick={() => void loadLibrary(library.page - 1)}>上一页</button><span>第 {library.page} 页</span><button disabled={library.page * 40 >= library.total} onClick={() => void loadLibrary(library.page + 1)}>下一页</button></div>}
      </>}
      {tab === 'settings' && <>
        <div className="page-heading"><div><span className="eyebrow">偏好与连接</span><h1>设置与诊断</h1><p>设置保存在本机，修改后用于新建任务。</p></div></div>
        <section className="settings-card"><h3>下载设置</h3><label>保存目录<div className="input-group"><input value={settings.path} onChange={e => setSettings({ ...settings, path: e.target.value })}/><button onClick={() => void attempt(async () => { const value = await api.chooseDirectory(); if (value) setSettings({ ...settings, path: value }); })}>选择目录</button></div></label>
          <label>同时下载数量<select value={settings.thread} onChange={e => setSettings({ ...settings, thread: Number(e.target.value) })}>{[1, 2, 3, 4].map(n => <option key={n}>{n}</option>)}</select></label>
          <label className="toggle"><input type="checkbox" checked={settings.cleanup} onChange={e => setSettings({ ...settings, cleanup: e.target.checked })}/><span>下载后自动去水印<small>保留原片；可能存在修复痕迹或字幕误伤，部分结果会标记为“部分完成”。</small></span></label>
        </section>
        <section className="settings-card"><h3>网络设置</h3><label>HTTP / HTTPS 代理<input placeholder="留空直连，例如 http://127.0.0.1:7897" value={settings.proxy} onChange={e => setSettings({ ...settings, proxy: e.target.value })}/></label><p className="muted">浏览器和下载服务使用同一代理设置；系统 TUN 在直连模式下仍然生效。</p></section>
        <div className="toolbar"><button className="primary" onClick={() => void attempt(async () => { setSettings(await api.api('PUT', '/api/v1/settings', settings)); setNotice('设置已保存'); })}>保存设置</button><button onClick={() => void attempt(async () => setDiagnostics(await api.diagnostics()))}>运行网络诊断</button><button onClick={() => void attempt(async () => { const result = await api.importData(); if (result) { await initialize(); setNotice(`导入完成：${result.imported} 条历史`); } })}>导入 CLI 配置与历史</button><button onClick={() => void attempt(async () => { await api.browser('logout'); setLogged(false); setAccount(''); setNotice('已退出抖音登录'); })}>退出抖音账号</button></div>
        {diagnostics.length > 0 && <section className="settings-card"><h3>诊断结果</h3>{diagnostics.map(x => <div className="diagnostic" key={x.name}><b>{x.ok ? '✓' : '×'} {x.name}</b><span>{x.detail}</span></div>)}</section>}
      </>}
    </div></main>
    {target && <div className="modal-backdrop"><section className="modal"><div className="modal-heading"><div><span className="eyebrow">新建任务</span><h2>{targetLabels[target.kind]}</h2></div><button onClick={() => setTarget(null)} aria-label="关闭下载选项">×</button></div>
      <p className="muted">{target.kind === 'items' ? `已选择 ${target.ids?.length} 件作品` : '按所选范围读取作品，并保存到下载目录。'}</p>
      {target.kind !== 'items' && <><label>作品数量<select value={options.limit} onChange={e => setOptions({ ...options, limit: Number(e.target.value) })}>{[20, 50, 100, 0].map(n => <option key={n} value={n}>{n === 0 ? '全部作品' : `最近 ${n} 条`}</option>)}</select></label><div className="form-grid"><label>开始日期<input type="date" value={options.date_from || ''} onChange={e => setOptions({ ...options, date_from: e.target.value || null })}/></label><label>结束日期<input type="date" value={options.date_to || ''} onChange={e => setOptions({ ...options, date_to: e.target.value || null })}/></label></div><label>作品类型<select value={options.media_type} onChange={e => setOptions({ ...options, media_type: e.target.value })}><option value="all">视频和图文</option><option value="video">仅视频</option><option value="gallery">仅图文</option></select></label></>}
      <label>视频画质<select value={options.quality} onChange={e => setOptions({ ...options, quality: e.target.value })}><option value="highest">最高可用画质</option><option value="original">优先原画</option><option value="1080p">1080p</option><option value="720p">720p</option><option value="lowest">最低可用画质</option></select></label>
      <label className="toggle"><input type="checkbox" checked={options.cleanup} onChange={e => setOptions({ ...options, cleanup: e.target.checked })}/><span>下载后自动去水印<small>保留原片，并生成独立处理副本与报告。</small></span></label><div className="modal-footer"><button onClick={() => setTarget(null)}>取消</button><button className="primary" disabled={busy || !ready} onClick={() => void submit()}>{busy ? '正在创建…' : '开始下载'}</button></div>
    </section></div>}
    {preview && <div className="modal-backdrop" onClick={() => setPreview(null)}><section className="media-modal" onClick={e => e.stopPropagation()}><button className="close-preview" onClick={() => setPreview(null)}>关闭预览 ×</button>{preview.image ? <img src={preview.url} alt="作品预览"/> : <video src={preview.url} controls autoPlay onError={() => setNotice('内置播放器不支持此编码，可使用“打开目录”在系统播放器中播放')}/>}</section></div>}
    {report && <div className="modal-backdrop"><section className="modal report"><div className="modal-heading"><h2>处理报告</h2><button onClick={() => setReport(null)}>关闭 ×</button></div><span className={'badge ' + report.status}>{labels[report.status] || report.status}</span><p>{report.reason || '检测与校验结果如下'}</p><pre>{JSON.stringify(report, null, 2)}</pre></section></div>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App/>);
