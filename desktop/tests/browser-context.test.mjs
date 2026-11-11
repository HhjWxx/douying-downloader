import test from 'node:test';
import assert from 'node:assert/strict';
import { MediaIndex } from '../out/main/browser-context.js';

const first = '7697097927726009663';
const second = '7704562259423826289';
const video = (id, url) => ({aweme_id:id,video:{play_addr:{url_list:[url]}}});
const snapshot = overrides => ({url:'https://www.douyin.com/',ids:[],links:[],sources:[],items:[],payloads:[],...overrides});

test('共享媒体端点依赖完整查询参数，同资源多作品不能误下载', () => {
  const index = new MediaIndex();
  const url = 'https://media.douyin.com/play/?id=first';
  const other = 'https://media.douyin.com/play/?id=second';
  index.ingest(JSON.stringify({aweme_list:[video(first,url),video(second,other)]}));
  assert.equal(index.current(snapshot({sources:[url]})),first);
  assert.equal(index.current(snapshot({sources:[other]})),second);
  assert.equal(index.current(snapshot({sources:['https://media.douyin.com/play/?id=unknown']})),'');
  index.ingest(JSON.stringify(video(second,url)));
  assert.equal(index.current(snapshot({sources:[url]})),'');
});

test('播放器绑定冲突时不退回媒体或链接猜测，清空后不能匹配旧资源', () => {
  const index = new MediaIndex();
  const url = 'https://media.douyin.com/first.mp4';
  index.ingest(JSON.stringify(video(first,url)));
  assert.equal(index.current(snapshot({ids:[first,second],sources:[url],links:[first]})),'');
  index.clear();
  assert.equal(index.current(snapshot({sources:[url]})),'');
});
