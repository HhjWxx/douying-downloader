import test from 'node:test';
import assert from 'node:assert/strict';
import { apiAllowed, contextFromURL, douyinURL, pageScript } from '../out/main/policy.js';

test('页面导航与 IPC 路由边界', () => {
  assert.equal(douyinURL('https://www.douyin.com/user/MS4abc'), true);
  for (const url of ['javascript:alert(1)', 'https://douyin.com.evil.test/', 'https://user@www.douyin.com/']) assert.equal(douyinURL(url), false);
  assert.equal(apiAllowed('POST', '/api/v1/jobs/abcd/resume'), true);
  assert.equal(apiAllowed('POST', '/api/v1/capture'), false);
  assert.equal(apiAllowed('GET', '/api/v1/../../etc/passwd'), false);
  assert.equal(apiAllowed('POST', '/api/v1/session'), false);
});

test('当前作品和作者标识保持字符串精度', () => {
  assert.equal(contextFromURL('https://www.douyin.com/?modal_id=7697097927726009663').current, '7697097927726009663');
  assert.equal(contextFromURL('https://www.douyin.com/user/MS4abc').author, 'MS4abc');
  assert.equal(contextFromURL('https://www.douyin.com/?recommend=1').current, '');
});

test('桥接脚本将动态内容作为数据，保留表单 POST', async () => {
  let observed;
  const payload = `'); globalThis.compromised = true; //`;
  const script = pageScript({path:'/aweme/v1/web/aweme/listcollection/', params:{cursor:'7697097927726009663',q:payload}, method:'POST', data:{count:20,cursor:'0'}});
  const evaluate = new Function('window','navigator','screen', `return ${script}`);
  const result = await evaluate({fetch:async (url, options) => {
    observed = {url,options}; return {status:200,text:async ()=>'{}'};
  }}, {platform:'MacIntel',language:'zh-CN'}, {width:1920,height:1080});
  assert.equal(new URL(observed.url).searchParams.get('q'), payload);
  assert.equal(observed.options.body, 'count=20&cursor=0');
  assert.equal(globalThis.compromised, undefined);
  assert.equal(result.status,200);
});

test('当前作品只接受抖音页面的数字标识', () => {
  assert.equal(contextFromURL('https://www.douyin.com/?modal_id=undefined').current, '');
  assert.equal(contextFromURL('https://other.test/video/7697097927726009663').current, '');
});
