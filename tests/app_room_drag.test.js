// 房间栏按住左键拖动横向滚动。
//
// 滚轮已经能滚了，但横向条上最自然的手势是按住拖。难点在于：厅按钮
// 本身是可点的（切厅、铃铛、收起），拖完必须把那一下点击吃掉，
// 否则拖一下就切了厅或者开了铃铛。

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { JSDOM } = require('jsdom')

const APP_SOURCE = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'app.js'), 'utf8'
)

function createApp(t, {overflow = true} = {}) {
  const dom = new JSDOM(`<!doctype html><body>
    <div id="events"></div><div id="roomSelector"></div>
    <div id="silencePanel"></div><div id="toastLayer"></div>
    <div id="dot"></div><div id="statusText"></div><div id="statsText"></div>
    <div id="dataPanelTitle"></div>
    <div id="feedPane"><div id="feedContainer"></div>
    <button id="feedNewMessageBtn"></button></div>
  </body>`, {url: 'http://127.0.0.1:5000/', runScripts: 'outside-only'})
  const { window } = dom
  window.KEKEMI_AUTH = {role: 'admin', enabled: false}
  window.fetch = async () => ({json: async () => ({success: true, alerts: [],
    unread_by_hall: {}, douyin_ids: [], records: [], active: [], count: 0})})
  window.EventSource = class { close() {} }
  window.setInterval = () => 0
  window.clearInterval = () => {}
  window.setTimeout = () => 0
  window.anime = {animate: () => ({pause() {}}), stagger: () => 0, spring: () => 0}
  if (!window.CSS) window.CSS = {}
  if (!window.CSS.escape) window.CSS.escape = v => String(v)

  const selector = window.document.getElementById('roomSelector')
  // jsdom 不做布局，scrollWidth/clientWidth 恒为 0，「没溢出就别抢」那条
  // 守卫会把拖动整个挡掉。这里把溢出状态造出来。
  Object.defineProperty(selector, 'scrollWidth',
    {configurable: true, get: () => (overflow ? 900 : 300)})
  Object.defineProperty(selector, 'clientWidth',
    {configurable: true, get: () => 300})
  let scroll = 0
  Object.defineProperty(selector, 'scrollLeft', {
    configurable: true,
    get: () => scroll,
    set: v => { scroll = Math.max(0, Math.min(600, v)) },
  })

  const context = dom.getInternalVMContext()
  new vm.Script(APP_SOURCE, {filename: 'static/app.js'}).runInContext(context)
  t.after(() => window.close())

  function mouse(type, x, target) {
    const ev = new window.MouseEvent(type, {
      bubbles: true, cancelable: true, clientX: x, button: 0,
    })
    ;(target || selector).dispatchEvent(ev)
    return ev
  }

  return {
    window, selector, mouse,
    run(code) { return new vm.Script(code).runInContext(context) },
    // 点击切厅会发起 fetch。它的 .then 必须在 t.after 关掉窗口之前跑完，
    // 否则 document 已经没了，冒出一堆未处理拒绝把整个文件判失败。
    async flush() {
      await new Promise(r => setImmediate(r))
      await new Promise(r => setImmediate(r))
      await new Promise(r => setImmediate(r))
    },
    twoRooms() {
      this.run(`
        currentRooms['room-a'] = {nickname: 'A厅', douyin_id: 'aaa'}
        currentRooms['room-b'] = {nickname: 'B厅', douyin_id: 'bbb'}
        selectedRoomId = 'room-a'
        updateRoomSelector()
      `)
    },
  }
}

test('按住左键横向拖动会滚动房间栏', t => {
  const app = createApp(t)
  app.twoRooms()
  app.mouse('mousedown', 400)
  app.mouse('mousemove', 320)     // 往左拖 80px
  app.mouse('mouseup', 320)
  assert.equal(app.selector.scrollLeft, 80, '往左拖，内容往右走')
})

test('松手之后再动鼠标不会继续滚', t => {
  const app = createApp(t)
  app.twoRooms()
  app.mouse('mousedown', 400)
  app.mouse('mousemove', 350)
  app.mouse('mouseup', 350)
  const after = app.selector.scrollLeft
  app.mouse('mousemove', 100)
  assert.equal(app.selector.scrollLeft, after)
})

test('鼠标移出房间栏也要停止拖动，不然回来时会跳一大截', t => {
  const app = createApp(t)
  app.twoRooms()
  app.mouse('mousedown', 400)
  app.mouse('mousemove', 380)
  app.mouse('mouseleave', 380)
  const after = app.selector.scrollLeft
  app.mouse('mousemove', 100)
  assert.equal(app.selector.scrollLeft, after)
})

test('拖动之后那一下点击要被吃掉，不能误切厅', async t => {
  const app = createApp(t)
  app.twoRooms()
  const btn = app.window.document.querySelector('[data-rid="room-b"]')
  app.mouse('mousedown', 400, btn)
  app.mouse('mousemove', 320, btn)      // 拖了 80px
  app.mouse('mouseup', 320, btn)
  app.mouse('click', 320, btn)
  assert.equal(app.run(`selectedRoomId`), 'room-a', '拖动不该切厅')
  await app.flush()
})

test('拖动之后那一下点击也不能误开铃铛', t => {
  const app = createApp(t)
  app.twoRooms()
  const bell = app.window.document.querySelector('[data-silence-id="bbb"]')
  app.mouse('mousedown', 400, bell)
  app.mouse('mousemove', 320, bell)
  app.mouse('mouseup', 320, bell)
  app.mouse('click', 320, bell)
  assert.equal(app.window.document.getElementById('silencePanel').innerHTML, '',
    '拖动不该打开面板')
})

test('只是点一下（没拖动），点击照常生效', async t => {
  const app = createApp(t)
  app.twoRooms()
  const btn = app.window.document.querySelector('[data-rid="room-b"]')
  app.mouse('mousedown', 400, btn)
  app.mouse('mouseup', 400, btn)
  app.mouse('click', 400, btn)
  assert.equal(app.run(`selectedRoomId`), 'room-b', '普通点击不能被误伤')
  await app.flush()
})

test('手抖几个像素仍算点击，不算拖动', async t => {
  const app = createApp(t)
  app.twoRooms()
  const btn = app.window.document.querySelector('[data-rid="room-b"]')
  app.mouse('mousedown', 400, btn)
  app.mouse('mousemove', 398, btn)      // 抖了 2px
  app.mouse('mouseup', 398, btn)
  app.mouse('click', 398, btn)
  assert.equal(app.run(`selectedRoomId`), 'room-b', '2px 抖动不该被当成拖动')
  await app.flush()
})

test('没有溢出时不拦截拖动，别把普通的文字选中抢掉', t => {
  const app = createApp(t, {overflow: false})
  app.twoRooms()
  const down = app.mouse('mousedown', 400)
  assert.equal(down.defaultPrevented, false, '没溢出就不该 preventDefault')
  app.mouse('mousemove', 320)
  assert.equal(app.selector.scrollLeft, 0)
})

test('右键按下不触发拖动', t => {
  const app = createApp(t)
  app.twoRooms()
  const ev = new app.window.MouseEvent('mousedown', {
    bubbles: true, cancelable: true, clientX: 400, button: 2})
  app.selector.dispatchEvent(ev)
  app.mouse('mousemove', 320)
  assert.equal(app.selector.scrollLeft, 0, '只认左键')
})
