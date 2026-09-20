// 降低 Egress 的两项前端改动。
//
// 背景：公屏改成定时轮询之后，/api/feed 每次返回全量（实测 15.7 KB），
// 每 3 秒一次就是 452 MB/天/人——比它替换掉的弹幕推送（12~23 MB/天）
// 还贵 20~35 倍。所以增量拉取不是可选优化，是那个改动能成立的前提。
//
// 另一项：页面切到后台时停掉轮询。挂着不看的标签页现在照样每 3 秒发一次请求。

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { JSDOM } = require('jsdom')

const APP_SOURCE = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'app.js'), 'utf8'
)

function createApp(t, {hidden = false} = {}) {
  const dom = new JSDOM(`<!doctype html><body>
    <div id="headerArea"></div>
    <button id="modeMystery"></button><button id="modeAll"></button>
    <button id="modeFeed"></button><button id="modeDaily"></button>
    <button id="modeHostWeekly"></button><button id="modeVisitorWeekly"></button>
    <button id="modeGiftAssignments"></button><button id="modeGiftLibrary"></button>
    <input id="input"><button id="btn"></button>
    <div id="events"></div><div id="roomSelector"></div>
    <div id="historyDropdown"></div>
    <div id="statusText"></div><div id="dot"></div><div id="statsText"></div>
    <div id="dataPanelTitle"></div><div id="silencePanel"></div>
    <div id="toastLayer"></div>
    <div id="feedPane"><div id="feedContainer"></div>
    <button id="feedNewMessageBtn"></button></div>
  </body>`, {url: 'http://127.0.0.1:5000/', runScripts: 'outside-only'})
  const { window } = dom
  Object.defineProperty(window.document, 'hidden', {
    configurable: true, get: () => hidden,
  })
  Object.defineProperty(window.document, 'visibilityState', {
    configurable: true, get: () => (hidden ? 'hidden' : 'visible'),
  })
  const urls = []
  window.KEKEMI_AUTH = {role: 'admin', enabled: false}
  window.fetch = async url => {
    urls.push(String(url))
    return {json: async () => ({success: true, events: [], rooms: {}, alerts: [],
      map: {}, count: 0, active: [], data: []})}
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
    window, urls,
    run(code) { return new vm.Script(code).runInContext(context) },
    feedUrls() { return urls.filter(u => u.includes('/api/feed/')) },
    async settle() {
      for (let i = 0; i < 5; i += 1) await new Promise(r => setImmediate(r))
    },
  }
}

test('缓存为空时拉全量，不带 since', async t => {
  const app = createApp(t)
  app.run(`selectedRoomId = 'r1'; currentRooms['r1'] = {nickname:'A厅'}
           currentView = 'feed'; feedByRoom['r1'] = []; loadFeed()`)
  await app.settle()
  const u = app.feedUrls()[0]
  assert.ok(u, '应该发起请求')
  assert.ok(!u.includes('since='), `首次不该带 since：${u}`)
})

test('缓存里已有内容时只拉增量', async t => {
  const app = createApp(t)
  app.run(`
    selectedRoomId = 'r1'; currentRooms['r1'] = {nickname:'A厅'}
    currentView = 'feed'
    feedByRoom['r1'] = [
      {type:'chat', room_id:'r1', display:'A', content:'旧', combo_key:'k1', timestamp:1787000000},
      {type:'chat', room_id:'r1', display:'B', content:'新', combo_key:'k2', timestamp:1787000500}
    ]
    loadFeed()
  `)
  await app.settle()
  const u = app.feedUrls()[0]
  assert.match(u, /since=1787000500/, `应带最新那条的时间戳：${u}`)
})

test('切厅时对新厅是全量，不会把上一个厅的时间戳带过去', async t => {
  const app = createApp(t)
  app.run(`
    currentRooms['r1'] = {nickname:'A厅'}; currentRooms['r2'] = {nickname:'B厅'}
    currentView = 'feed'
    feedByRoom['r1'] = [{type:'chat', room_id:'r1', display:'A', content:'x',
                         combo_key:'k1', timestamp:1787000500}]
    feedByRoom['r2'] = []
    selectedRoomId = 'r2'
    loadFeed()
  `)
  await app.settle()
  const u = app.feedUrls()[0]
  assert.ok(!u.includes('since='), `新厅缓存为空，应拉全量：${u}`)
})

test('页面切到后台时，公屏轮询不发请求', async t => {
  const app = createApp(t, {hidden: true})
  app.run(`
    selectedRoomId = 'r1'; currentRooms['r1'] = {nickname:'A厅'}
    currentView = 'feed'; feedByRoom['r1'] = []
    refreshFeedPage()
  `)
  await app.settle()
  assert.equal(app.feedUrls().length, 0, '后台标签页不该发请求')
})

test('页面在前台时，公屏轮询照常发请求', async t => {
  const app = createApp(t, {hidden: false})
  app.run(`
    selectedRoomId = 'r1'; currentRooms['r1'] = {nickname:'A厅'}
    currentView = 'feed'; feedByRoom['r1'] = []
    refreshFeedPage()
  `)
  await app.settle()
  assert.equal(app.feedUrls().length, 1)
})

test('从后台切回前台要立刻刷一次，别让人对着过期画面', async t => {
  const app = createApp(t, {hidden: false})
  app.run(`
    selectedRoomId = 'r1'; currentRooms['r1'] = {nickname:'A厅'}
    currentView = 'feed'; feedByRoom['r1'] = []
  `)
  app.urls.length = 0
  app.window.document.dispatchEvent(new app.window.Event('visibilitychange'))
  await app.settle()
  assert.ok(app.feedUrls().length >= 1, '切回前台应立刻拉一次')
})
