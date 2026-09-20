const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { JSDOM } = require('jsdom')

const APP_SOURCE = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'app.js'),
  'utf8'
)

function createApp(t, {role = 'super_admin', fetchImpl} = {}) {
  const dom = new JSDOM(`<!doctype html><html><head><title></title></head><body>
    <div id="headerArea"></div>
    <input id="input">
    <button id="btn"></button>
    <div id="historyDropdown"></div>
    <div id="statusText"></div>
    <div id="dot"></div>
    <div id="statsText"></div>
    <button id="modeMystery"></button>
    <button id="modeAll"></button>
    <button id="modeFeed"></button>
    <button id="modeDaily"></button>
    <button id="modeHostWeekly"></button>
    <button id="modeVisitorWeekly"></button>
    <button id="modeGiftAssignments"></button>
    <button id="modeGiftLibrary"></button>
    <div id="roomSelector"></div>
    <section id="dataPanel" class="data-panel">
      <div class="data-panel-toggle-row">
        <span id="dataPanelTitle"></span>
        <button id="dataPanelToggle" type="button" aria-expanded="true">收起 ↑</button>
      </div>
      <div id="dataPanelBody" class="data-panel-body">
        <div id="events"></div>
        <div id="feedPane" class="feed-pane" style="display:none">
          <div id="feedContainer" class="feed-container"></div>
          <button id="feedNewMessageBtn" class="feed-new-btn" style="display:none"></button>
        </div>
      </div>
    </section>
    </div>
  </body></html>`, {url: 'http://127.0.0.1:5000/', runScripts: 'outside-only'})
  const { window } = dom
  window.fetch = fetchImpl || (async () => ({json: async () => ({success: true})}))
  window.EventSource = class { close() {} }
  window.setInterval = () => 0
  window.clearInterval = () => {}
  window.anime = {animate: () => ({pause() {}}), stagger: () => 0, spring: () => 0}
  window.KEKEMI_AUTH = {role}
  if (!window.CSS) window.CSS = {}
  if (!window.CSS.escape) window.CSS.escape = value => String(value)
  const context = dom.getInternalVMContext()
  new vm.Script(APP_SOURCE, {filename: 'static/app.js'}).runInContext(context)
  t.after(() => window.close())
  return {
    window,
    run(code) { return new vm.Script(code).runInContext(context) },
    selectorHtml() {
      return window.document.getElementById('roomSelector').innerHTML
    },
    async flush() {
      await new Promise(resolve => setImmediate(resolve))
      await new Promise(resolve => setImmediate(resolve))
    },
  }
}

// 一个默认厅 + 一个临时厅
const SEED = `
Object.keys(currentRooms).forEach(k => delete currentRooms[k])
Object.assign(currentRooms, {
  'room-default': {nickname: 'demo_003', is_temporary: false},
  'room-temp': {nickname: '临时观察厅', is_temporary: true}
})
selectedRoomId = 'room-default'
updateRoomSelector()
bindDelegatedControls()
`

test('临时厅显示「临时」标记，默认厅不显示', async t => {
  const app = createApp(t)
  app.run(SEED)
  const html = app.selectorHtml()
  const tempCard = html.split('room-temp')[1] || ''
  await new Promise(r => setImmediate(r))
  assert.ok(html.includes('临时'), '临时厅要有标记')
  const defaultPart = html.slice(0, html.indexOf('room-temp'))
  assert.ok(!defaultPart.includes('room-temp-tag'), '默认厅不该带临时标记')
})

test('默认厅的 × 走「收起」，不发停止请求', async t => {
  const app = createApp(t)
  app.run(SEED)
  const calls = app.run(`
    window.__stopped = []
    window.fetch = (url) => { window.__stopped.push(String(url)); return new Promise(() => {}) }
    document.querySelector('[data-hide-rid="room-default"]').click()
    window.__stopped.filter(u => String(u).includes('/api/stop'))
  `)
  assert.equal(calls.length, 0, '默认厅不能触发停止监听')
})

test('临时厅的 × 弹确认；取消则什么都不做', async t => {
  const app = createApp(t)
  app.run(SEED)
  const calls = app.run(`
    window.__stopped = []
    window.confirm = () => false
    window.fetch = (url) => { window.__stopped.push(String(url)); return new Promise(() => {}) }
    document.querySelector('[data-stop-rid="room-temp"]').click()
    window.__stopped.filter(u => String(u).includes('/api/stop'))
  `)
  assert.equal(calls.length, 0, '取消确认后不得发出任何停止请求')
})

test('临时厅的 × 确认后调停止监听', async t => {
  const app = createApp(t)
  app.run(SEED)
  const calls = app.run(`
    window.__stopped = []
    window.confirm = () => true
    window.fetch = (url) => { window.__stopped.push(String(url)); return new Promise(() => {}) }
    document.querySelector('[data-stop-rid="room-temp"]').click()
    window.__stopped.filter(u => u.includes('/api/stop'))
  `)
  assert.equal(calls.length, 1, '确认后必须调 /api/stop')
})

test('确认文案要说明记录补不回来', async t => {
  const app = createApp(t)
  app.run(SEED)
  const text = app.run(`
    window.__msg = ''
    window.confirm = (m) => { window.__msg = m; return false }
    document.querySelector('[data-stop-rid="room-temp"]').click()
    window.__msg
  `)
  assert.ok(text.includes('临时观察厅'), '要点名是哪个厅')
  assert.ok(text.includes('补不回来'), '要说明后果')
})

/* ═══ 以下用例只喂后端 JSON，不手工往 currentRooms 里塞 is_temporary ═══
   上面那批用例是先把标记写好再渲染，所以「前端压根没把后端给的
   is_temporary 存下来」这种 bug 它们一条都抓不到——临时厅在页面上会
   长得跟常驻厅一模一样，叉掉只是收起，监听其实还在跑。 */

// /api/status 给房间列表，其余接口一律给空壳，免得用例互相干扰。
function backendFetch(routes) {
  return async url => {
    const target = String(url)
    const hit = Object.keys(routes).find(prefix => target.includes(prefix))
    const payload = hit ? routes[hit]() : {}
    return {
      json: async () => Object.assign(
        {success: true, count: 0, max: 3, active: [], map: {}, data: [], records: []},
        payload
      )
    }
  }
}

function statusPayload(active) {
  return {count: active.length, max: 3, active}
}

const DEFAULT_ROOM = {room_id: 'room-default', nickname: 'demo_003', is_temporary: false}
const TEMP_ROOM = {room_id: 'room-temp', nickname: '临时观察厅', is_temporary: true}

test('页面加载重连时，把 /api/status 的 is_temporary 存进 currentRooms', async t => {
  const app = createApp(t, {fetchImpl: backendFetch({
    '/api/status': () => statusPayload([DEFAULT_ROOM, TEMP_ROOM])
  })})
  await app.flush()
  assert.equal(app.run(`currentRooms['room-temp'].is_temporary`), true, '临时厅要标成临时')
  assert.equal(app.run(`currentRooms['room-default'].is_temporary`), false, '常驻厅要标成非临时')
})

test('页面加载重连后，临时厅直接渲染出停止叉号和「临时」标记', async t => {
  const app = createApp(t, {fetchImpl: backendFetch({
    '/api/status': () => statusPayload([DEFAULT_ROOM, TEMP_ROOM])
  })})
  await app.flush()
  const html = app.selectorHtml()
  assert.ok(html.includes('data-stop-rid="room-temp"'), '临时厅的叉号必须是「停止监听」')
  assert.ok(html.includes('room-temp-tag'), '临时厅要带「临时」标记')
  assert.ok(html.includes('data-hide-rid="room-default"'), '常驻厅的叉号仍然只是收起')
  assert.ok(!html.includes('data-stop-rid="room-default"'), '常驻厅不该出现停止叉号')
})

test('手动开厅：/api/start 说是临时厅，就按临时厅渲染', async t => {
  const app = createApp(t, {fetchImpl: backendFetch({
    '/api/start': () => ({room_id: 'room-temp', is_temporary: true})
  })})
  await app.flush()
  app.run(`startListening('room-temp', '临时观察厅')`)
  await app.flush()
  assert.equal(app.run(`currentRooms['room-temp'].is_temporary`), true, '开厅响应里的标记要存下来')
  assert.ok(app.selectorHtml().includes('data-stop-rid="room-temp"'),
    '刚开的临时厅就该显示停止叉号，不能等下一次刷新')
})

test('手动开厅：响应没带 is_temporary 时按常驻处理，且不是 undefined', async t => {
  const app = createApp(t, {fetchImpl: backendFetch({
    '/api/start': () => ({room_id: 'room-default'})
  })})
  await app.flush()
  app.run(`startListening('room-default', 'demo_003')`)
  await app.flush()
  assert.equal(app.run(`currentRooms['room-default'].is_temporary`), false, '缺字段要落成 false')
})

test('refreshRooms：厅从常驻变临时时，标记跟着变且页面重画', async t => {
  let temporary = false
  const app = createApp(t, {fetchImpl: backendFetch({
    '/api/status': () => statusPayload([
      {room_id: 'room-x', nickname: '厅X', is_temporary: temporary}
    ])
  })})
  await app.flush()
  assert.equal(app.run(`currentRooms['room-x'].is_temporary`), false, '前置条件：先是常驻厅')

  temporary = true
  await app.run(`refreshRooms()`)
  await app.flush()
  assert.equal(app.run(`currentRooms['room-x'].is_temporary`), true, '刷新要把新标记同步回来')
  assert.ok(app.selectorHtml().includes('data-stop-rid="room-x"'), '标记变了页面必须跟着变')
})

test('refreshRooms：厅从临时变回常驻时，停止叉号要收回去', async t => {
  let temporary = true
  const app = createApp(t, {fetchImpl: backendFetch({
    '/api/status': () => statusPayload([
      {room_id: 'room-x', nickname: '厅X', is_temporary: temporary}
    ])
  })})
  await app.flush()
  assert.equal(app.run(`currentRooms['room-x'].is_temporary`), true, '前置条件：先是临时厅')

  temporary = false
  await app.run(`refreshRooms()`)
  await app.flush()
  assert.equal(app.run(`currentRooms['room-x'].is_temporary`), false, '刷新要把新标记同步回来')
  assert.ok(!app.selectorHtml().includes('data-stop-rid="room-x"'), '常驻厅不该还留着停止叉号')
  assert.ok(app.selectorHtml().includes('data-hide-rid="room-x"'), '常驻厅的叉号是收起')
})

test('refreshRooms：不给页面上没有的厅凭空建条目', async t => {
  let active = []
  const app = createApp(t, {fetchImpl: backendFetch({
    '/api/status': () => statusPayload(active)
  })})
  await app.flush()
  assert.equal(app.run(`Object.keys(currentRooms).length`), 0, '前置条件：页面上没有厅')

  active = [TEMP_ROOM]
  await app.run(`refreshRooms()`)
  await app.flush()
  assert.equal(app.run(`currentRooms['room-temp']`), undefined,
    '刷新只负责对齐标记，开厅是 startListening / 自动重连的事')
})
