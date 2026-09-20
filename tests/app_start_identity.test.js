// 手动加厅时，解析拿到的身份要一路带到 /api/start。
//
// 2026-08-23：手动加的 demo.1 麦上主持正常，在线名单却一直「等待首次同步」。
// 解析那步明明返回了 sec_uid 和主播 uid，startListening 只发 room_id +
// nickname，转手全扔了，后端凑不齐主播身份就只能干等。
// douyin_id 也要带：它是「厅下播重开换房间号还能跟上」的依据。

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { JSDOM } = require('jsdom')

const APP_SOURCE = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'app.js'), 'utf8'
)

const RESOLVED = {
  success: true,
  room_id: '1000000000000000020',
  nickname: '示例点唱厅',
  live_status: 1,
  sec_uid: 'MS4wLjABAAAA_SYNTHETIC_0010_TEST_ONLY',
  anchor_id: '2769016234444315',
}

function createApp(t, input) {
  const dom = new JSDOM(`<!doctype html><body>
    <div id="headerArea"></div>
    <button id="modeMystery"></button><button id="modeAll"></button>
    <button id="modeFeed"></button><button id="modeDaily"></button>
    <button id="modeHostWeekly"></button><button id="modeVisitorWeekly"></button>
    <button id="modeGiftAssignments"></button><button id="modeGiftLibrary"></button>
    <input id="input" value="${input}"><button id="btn"></button>
    <div id="events"></div><div id="roomSelector"></div>
    <div id="historyDropdown"></div>
    <div id="statusText">已连接</div><div id="dot"></div>
    <div id="statsText"></div><div id="dataPanelTitle"></div>
    <div id="silencePanel"></div><div id="toastLayer"></div>
    <div id="feedPane"><div id="feedContainer"></div>
    <button id="feedNewMessageBtn"></button></div>
  </body>`, {url: 'http://127.0.0.1:5000/', runScripts: 'outside-only'})
  const { window } = dom
  const sent = []
  window.KEKEMI_AUTH = {role: 'admin', enabled: false}
  window.fetch = async (url, options) => {
    sent.push({url: String(url), options})
    if (String(url).includes('/api/resolve')) {
      return {json: async () => RESOLVED}
    }
    return {json: async () => ({success: true, alerts: [], rooms: {}, map: {}})}
  }
  window.EventSource = class { close() {} }
  window.setInterval = () => 0
  window.clearInterval = () => {}
  window.anime = {animate: () => ({pause() {}}), stagger: () => 0, spring: () => 0}
  if (!window.CSS) window.CSS = {}
  if (!window.CSS.escape) window.CSS.escape = v => String(v)
  const context = dom.getInternalVMContext()
  new vm.Script(APP_SOURCE, {filename: 'static/app.js'}).runInContext(context)
  t.after(() => window.close())
  return {
    window, sent,
    run(code) { return new vm.Script(code).runInContext(context) },
    startBody() {
      const hit = sent.find(s => s.url.includes('/api/start'))
      return hit ? JSON.parse(hit.options.body) : null
    },
    async settle() {
      for (let i = 0; i < 6; i += 1) await new Promise(r => setImmediate(r))
    },
  }
}

test('解析成功后，身份要原样带进 /api/start', async t => {
  const app = createApp(t, 'demo.1')
  app.run(`connect()`)
  await app.settle()
  const body = app.startBody()
  assert.ok(body, '应该调用 /api/start')
  assert.equal(body.room_id, '1000000000000000020')
  assert.equal(body.sec_uid, RESOLVED.sec_uid)
  assert.equal(body.anchor_id, '2769016234444315')
})

test('抖音号本身要当成 douyin_id 带上，否则厅重开就跟丢了', async t => {
  const app = createApp(t, 'demo.1')
  app.run(`connect()`)
  await app.settle()
  assert.equal(app.startBody().douyin_id, 'demo.1')
})

test('输入的是链接时不能把链接当抖音号', async t => {
  const app = createApp(t, 'https://live.douyin.com/123')
  app.run(`connect()`)
  await app.settle()
  assert.equal(app.startBody().douyin_id, '')
})
