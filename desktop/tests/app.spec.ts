import { test, expect, _electron as electron } from '@playwright/test';
import { mkdtemp, realpath, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

test('真实 Electron 窗口、sidecar 与设置持久化', async () => {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), 'douyin-desktop-test-'));
  const app = await electron.launch({ args: ['.'], env: { ...process.env,
    DOUYIN_DESKTOP_TEST: '1', DOUYIN_DESKTOP_DATA_DIR: dataDir } });
  try {
    const page = await app.firstWindow();
    await expect(page.getByText('本地服务已连接')).toBeVisible({ timeout: 45000 });
    await page.getByRole('button', { name: '设置与诊断', exact: true }).click();
    await expect(page.getByRole('heading', { name: '设置与诊断' })).toBeVisible();
    await page.getByLabel('保存目录').fill(path.join(dataDir, '下载目录 中文'));
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await expect(page.getByText('设置已保存')).toBeVisible();
    await page.getByRole('button', { name: '下载任务', exact: true }).click();
    await page.getByLabel('下载链接').fill('https://www.douyin.com/video/7697097927726009663');
    await page.getByRole('button', { name: '添加链接任务' }).click();
    await expect(page.getByRole('heading', { name: '链接下载' })).toBeVisible();
    await page.getByRole('button', { name: '开始下载', exact: true }).click();
    await expect(page.locator('.job')).toHaveCount(1);
    await expect(page.locator('.job .badge').first()).toHaveText('等待处理', { timeout: 15000 });
    await page.screenshot({ path: 'test-results/tasks.png', fullPage: true });
    const demo: any = { job_id: 'progress-demo', title: '进度验证', status: 'running',
      target: {kind:'items',ids:['123456']}, options:{cleanup:true}, created_at:new Date().toISOString(),
      items:['123456'],results:{},output_dirs:[],error:'',attempt:1,listing_complete:true,
      progress:{'123456':{download:{state:'active',percent:45,bytes_read:45*1024*1024,
        bytes_total:100*1024*1024,speed:2*1024*1024,started_at:Date.now()/1000-20,updated_at:Date.now()/1000},
        cleanup:{state:'pending'}}} };
    const sendProgress = async () => app.evaluate(({BrowserWindow}, job) => {
      BrowserWindow.getAllWindows().find(win => win.webContents.getURL().startsWith('app://ui/'))!
        .webContents.send('desktop-event',{type:'job',job});
    }, demo);
    await sendProgress();
    const progressRow = page.locator('.job').filter({hasText:'进度验证'});
    await expect(progressRow.getByRole('progressbar',{name:'下载进度',exact:true})).toHaveAttribute('aria-valuenow','45');
    demo.progress['123456'].download.state='done';
    demo.progress['123456'].download.percent=100;
    demo.progress['123456'].download.bytes_read=100*1024*1024;
    demo.progress['123456'].download.ended_at=Date.now()/1000;
    demo.progress['123456'].cleanup={state:'active',stage:'修复画面',percent:37,done:370,total:1000,unit:'帧',started_at:Date.now()/1000-15,updated_at:Date.now()/1000};
    await sendProgress();
    await expect(progressRow.getByRole('progressbar',{name:'下载进度',exact:true})).toHaveAttribute('aria-valuenow','100');
    await expect(progressRow.getByRole('progressbar',{name:'去水印进度',exact:true})).toHaveAttribute('aria-valuenow','37');
    await expect(progressRow.getByText('37%（当前阶段）')).toBeVisible();
    await expect.poll(async () => progressRow.locator('.cleanup .phase-track').evaluate(track =>
      Math.round(track.firstElementChild!.getBoundingClientRect().width / track.getBoundingClientRect().width * 100))).toBe(37);
    await page.screenshot({path:'test-results/dual-progress.png',fullPage:true});
    demo.progress['123456'].cleanup={state:'active',stage:'加载模型',percent:null,updated_at:Date.now()/1000};
    await sendProgress();
    await expect(progressRow.locator('.indeterminate')).toHaveCount(1);
    await expect(progressRow.getByRole('progressbar',{name:'去水印进度',exact:true})).not.toHaveAttribute('aria-valuenow', /.+/);
    demo.status='cancelled';
    await sendProgress();
    await expect(progressRow.locator('.indeterminate')).toHaveCount(0);
    await page.getByRole('button', { name: '本地作品', exact: true }).click();
    await expect(page.getByText('暂无本地作品')).toBeVisible();
    await page.getByRole('button', { name: '浏览抖音', exact: true }).click();
    await expect(page.getByRole('heading', { name: '在抖音里发现，在这里保存。' })).toBeVisible();
    await page.screenshot({ path: 'test-results/browser.png', fullPage: true });
    await app.close();
    const restarted = await electron.launch({ args: ['.'], env: { ...process.env,
      DOUYIN_DESKTOP_TEST: '1', DOUYIN_DESKTOP_DATA_DIR: dataDir } });
    try {
      const second = await restarted.firstWindow();
      await expect(second.getByText('本地服务已连接')).toBeVisible({ timeout: 45000 });
      await second.getByRole('button', { name: '下载任务', exact: true }).click();
      await expect(second.locator('.job')).toHaveCount(1);
      await second.getByRole('button', { name: '设置与诊断', exact: true }).click();
      await expect(second.getByLabel('保存目录')).toHaveValue(await realpath(path.join(dataDir, '下载目录 中文')));
    } finally { await restarted.close(); }
  } finally {
    await app.close().catch(() => {});
    await rm(dataDir, { recursive: true, force: true });
  }
});
