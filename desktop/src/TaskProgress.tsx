import { useEffect, useState } from 'react';
import type { Job } from './types';

const stopped = new Set(['success', 'partial', 'failed', 'cancelled', 'interrupted', 'waiting']);
const size = (value: number) => value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GB`
  : value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MB` : `${(value / 1024).toFixed(1)} KB`;
const duration = (seconds: number) => seconds < 60 ? `${Math.floor(seconds)} 秒`
  : `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;

function Phase({ kind, value, result, job, now }: {kind: 'download' | 'cleanup'; value: any; result: any; job: Job; now: number}) {
  const phase = value || {};
  const cleanStates = (result?.cleanup || []).map((item: any) => item.status);
  const fallback = kind === 'download'
    ? result ? result.status === 'failed' ? 'failed' : 'done' : 'pending'
    : cleanStates.includes('failed') ? 'failed' : cleanStates.some((x: string) => ['partial', 'unsupported'].includes(x)) ? 'partial'
      : cleanStates.some((x: string) => x !== 'disabled') ? 'done' : result?.status !== 'failed' && result ? 'skipped' : 'pending';
  const state = phase.state || fallback;
  const active = state === 'active' && !stopped.has(job.status);
  const finished = ['done', 'partial'].includes(state);
  const percent = finished ? 100 : Number.isFinite(phase.percent) ? Math.max(0, Math.min(100, phase.percent)) : undefined;
  const elapsed = phase.started_at ? Math.max(0, (phase.ended_at || now) - phase.started_at) : 0;
  let caption = kind === 'download' ? '等待下载' : '等待下载完成';
  let detail = '';
  if (finished) caption = kind === 'download' ? result?.status === 'skipped' ? '本地已存在' : '下载完成' : state === 'partial' ? '处理结束，请查看报告' : '处理完成';
  else if (state === 'failed') caption = kind === 'download' ? '下载失败' : '处理失败';
  else if (state === 'skipped') caption = job.options.cleanup ? '无需视频处理' : '未启用';
  else if (active) caption = kind === 'download' ? phase.stage || '下载中' : phase.stage || '去水印处理中';
  else if (state === 'paused' || (state === 'active' && stopped.has(job.status))) caption = job.status === 'waiting' ? '等待继续' : '已停止';
  else if (phase.stage) caption = phase.stage;
  if (kind === 'download' && phase.bytes_read !== undefined) {
    detail = `${size(phase.bytes_read)}${phase.bytes_total ? ` / ${size(phase.bytes_total)}` : ' · 总大小未知'}`;
    if (active) detail += now - (phase.updated_at || 0) > 10 ? ' · 等待接收数据' : ` · ${size(phase.speed || 0)}/s`;
  }
  if (kind === 'cleanup' && !finished && phase.unit) {
    const format = (n: number) => phase.unit === '秒' ? n.toFixed(1) : Math.floor(n).toLocaleString();
    detail = `已处理 ${format(phase.done || 0)}${phase.total ? ` / ${format(phase.total)}` : ''} ${phase.unit}`;
  }
  const status = finished ? '100%' : percent !== undefined ? `${Math.floor(percent)}%${kind === 'cleanup' ? '（当前阶段）' : ''}`
    : active ? '处理中' : state === 'pending' ? '等待中' : '';
  return <div className={`phase-progress ${kind} ${state}`}>
    <div className="phase-heading"><b>{kind === 'download' ? '下载' : '去水印'}</b><span>{caption}</span><strong>{status}</strong></div>
    <div className={`phase-track ${active && percent === undefined ? 'indeterminate' : ''}`} role="progressbar"
      aria-label={kind === 'download' ? '下载进度' : '去水印进度'} aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={percent} aria-valuetext={`${caption} ${status}`}>
      <span style={{ width: `${percent ?? (active ? 30 : 0)}%` }}/>
    </div>
    <div className="phase-detail"><span>{detail}</span><span>{elapsed > 0 ? `已耗时 ${duration(elapsed)}` : ''}
      {active && kind === 'cleanup' && phase.updated_at && now - phase.updated_at > 10 ? ' · 正在等待进程响应' : ''}</span></div>
  </div>;
}

export function TaskProgress({ job }: {job: Job}) {
  const [now, setNow] = useState(Date.now() / 1000);
  const active = ['pending', 'running'].includes(job.status);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [active]);
  const items = job.items.length ? job.items : job.target.ids || [];
  const visible = items.length === 1 ? items : items.filter(key => job.progress[key] && !job.results[key]);
  return <div className="task-phases">{visible.map(key => <div key={key} className="item-phases">
    {items.length > 1 && <small className="phase-item-name">{job.results[key]?.title || key}</small>}
    <Phase kind="download" value={job.progress[key]?.download} result={job.results[key]} job={job} now={now}/>
    <Phase kind="cleanup" value={job.progress[key]?.cleanup} result={job.results[key]} job={job} now={now}/>
  </div>)}</div>;
}
