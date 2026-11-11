import { test, expect, _electron as electron } from '@playwright/test';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const firstID = '7697097927726009663';
const nextID = '7704562259423826289';
const prefetchedID = '7504599260294352180';
const item = (id: string, title: string) => ({aweme_id:id, desc:title,
  author:{nickname:'测试作者',sec_uid:'MS4test'}, video:{play_addr:{url_list:[`https://media.douyin.com/play/?id=${id}`]}}});

test('当前播放器识别、预加载列表和点击下载的完整链路', async () => {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), 'douyin-browser-test-'));
  const app = await electron.launch({ args: ['.'], env: {...process.env,
    DOUYIN_DESKTOP_TEST:'1', DOUYIN_DESKTOP_DATA_DIR:dataDir} });
  try {
    const page = await app.firstWindow();
    await expect(page.getByText('本地服务已连接')).toBeVisible({timeout:45000});
    // 本地 HTTPS fixture 保留真实 Electron 导航、CDP 捕获、IPC 和 Python 缓存路径。
    await app.evaluate(({session}, fixtures) => {
      session.fromPartition('persist:douyin').protocol.handle('https', request => {
        const url = new URL(request.url);
        if (url.pathname.startsWith('/aweme/')) return new Response(JSON.stringify({aweme_list:[fixtures.prefetched]}),
          {headers:{'Content-Type':'application/json'}});
        if (url.hostname === 'media.douyin.com') return new Response('',{status:404});
        const content = url.pathname === '/empty' ? '<div>尚未加载播放器</div>' : `
          <section id="player" data-aweme-id="${fixtures.first.aweme_id}"><video style="width:320px;height:200px"></video></section>
          <aside><a href="/video/${fixtures.prefetched.aweme_id}">其他推荐</a></aside>
          <script id="RENDER_DATA" type="application/json">${encodeURIComponent(JSON.stringify({aweme_list:[fixtures.first]}))}</script>`;
        return new Response(`<html><head><style>body{margin:0}a{display:block;width:200px;height:50px}</style></head><body>${content}</body></html>`,
          {headers:{'Content-Type':'text/html; charset=utf-8'}});
      });
    }, {first:item(firstID,'首页内嵌作品'),prefetched:item(prefetchedID,'后台预加载作品')});
    const runInBrowser = async (script: string) => app.evaluate(({BrowserWindow}, source) => {
      const win = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().startsWith('app://ui/'))!;
      const view = win.contentView.children[0] as Electron.WebContentsView;
      return view.webContents.executeJavaScript(source);
    }, script);
    const navigate = (url: string) => page.evaluate(url => window.desktop.browser('navigate',url),url);
    const current = () => page.evaluate(() => window.desktop.browser('current'));
    const active = page.locator('.capture-item[aria-current="true"]');

    await test.step('推荐页 URL 无 ID：从播放器及内嵌数据恢复当前作品', async () => {
      await navigate('https://www.douyin.com/?recommend=1');
      expect(await current()).toBe(firstID);
      await expect(active).toContainText('首页内嵌作品');
    });
    await test.step('后台响应补入列表，不覆盖正在播放的作品', async () => {
      await runInBrowser("fetch('/aweme/v1/web/aweme/feed/').then(r=>r.json())");
      await expect(page.locator('.capture-list')).toContainText('后台预加载作品');
      await expect(active).toContainText('首页内嵌作品');
    });
    await test.step('同 URL 切换、列表尚无此作品：点击时读取最新播放器', async () => {
      await runInBrowser(`document.querySelector('#player').setAttribute('data-aweme-id','${nextID}')`);
      await page.getByRole('button',{name:'↓ 下载当前视频',exact:true}).click();
      await expect(page.getByRole('heading',{name:'选中作品',exact:true})).toBeVisible();
      await page.getByRole('button',{name:'开始下载',exact:true}).click();
      await expect(page.locator('.job')).toHaveCount(1);
      const jobs = await page.evaluate(() => window.desktop.api('GET','/api/v1/jobs'));
      expect(jobs.jobs[0].target.ids).toEqual([nextID]);
      await page.getByRole('button',{name:'浏览抖音',exact:true}).click();
      await expect(active).toContainText('当前播放作品');
    });
    await test.step('作者页 SPA 打开预加载视频：不清空已经捕获的列表', async () => {
      await runInBrowser(`history.pushState({},'', '/user/MS4test?modal_id=${prefetchedID}')`);
      expect(await current()).toBe(prefetchedID);
      await expect(active).toContainText('后台预加载作品');
      await expect(page.locator('.capture-list')).toContainText('首页内嵌作品');
    });
    await test.step('播放器 React 分支的作品数据，不依赖整个页面的唯一链接', async () => {
      await runInBrowser(`history.pushState({},'', '/?recommend=1');
        document.querySelector('#player').removeAttribute('data-aweme-id');
        document.querySelector('video').__reactFiber$fixture = {memoizedProps:{}, return:{memoizedProps:{awemeInfo:${JSON.stringify(item(nextID,'播放器绑定作品'))}},return:null}};`);
      expect(await current()).toBe(nextID);
      await expect(active).toContainText('播放器绑定作品');
    });
    await test.step('没有绑定标识时，用正在播放的媒体完整地址匹配作品', async () => {
      await runInBrowser(`delete document.querySelector('video').__reactFiber$fixture;
        document.querySelector('video').src='https://media.douyin.com/play/?id=${firstID}'`);
      expect(await current()).toBe(firstID);
    });
    await test.step('多个可见播放器、未知媒体与侧栏链接不能选错作品', async () => {
      await runInBrowser(`document.querySelector('#player').insertAdjacentHTML('beforeend','<video style="width:320px;height:200px"></video>')`);
      expect(await current()).toBe('');
      await runInBrowser(`document.querySelectorAll('video')[1].remove(); document.querySelector('video').removeAttribute('src'); document.querySelector('video').load();`);
      expect(await current()).toBe('');
      await page.getByRole('button',{name:'↓ 下载当前视频',exact:true}).click();
      await expect(page.getByText('尚未识别到当前播放作品，请等待视频加载或打开视频详情后重试')).toBeVisible();
      await expect(page.locator('.modal')).toHaveCount(0);
    });
    await test.step('完整导航后不保留旧作品或旧列表', async () => {
      await navigate('https://www.douyin.com/empty');
      expect(await current()).toBe('');
      await expect(page.locator('.capture-item')).toHaveCount(0);
    });
  } finally {
    await app.close().catch(() => {});
    await rm(dataDir,{recursive:true,force:true});
  }
});
