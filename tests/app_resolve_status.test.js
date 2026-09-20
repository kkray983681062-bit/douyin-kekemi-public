// 解析失败时不能一直挂着「解析中...」。
//
// 2026-08-23：Ray 反馈 demo.1「一直卡在解析中」。后端其实很快返回了失败，
// 但前端每条失败分支只做了 btn.disabled=false + resetBtnText()，
// 开头那句 setStatus('解析中...') 从来没被复位。再加上 showToast 只在
// 神秘人列表为空时才往 #events 写，错误提示大概率也看不见——
// 于是用户既看不到失败原因，又一直看着「解析中」。

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { JSDOM } = require('jsdom')

const APP_SOURCE = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'app.js'), 'utf8'
)

function createApp(t, resolvePayload) {
  const dom = new JSDOM(`<!doctype html><body>
    <input id="input" value="demo.1"><button id="btn"></button>
    <div id="events"></div><div id="roomSelector"></div>
    <div id="historyDropdown"></div>
    <div id="statusText">已连接</div><div id="dot"></div>
    <div id="statsText"></div><div id="dataPanelTitle"></div>
    <div id="silencePanel"></div><div id="toastLayer"></div>
    <div id="feedPane"><div id="feedContainer"></div>
    <button id="feedNewMessageBtn"></button></div>
  </body>`, {url: 'http://127.0.0.1:5000/', runScripts: 'outside-only'})
  const { window } = dom
  window.KEKEMI_AUTH = {role: 'admin', enabled: false}
  window.fetch = async url => ({
    json: async () => (String(url).includes('/api/resolve')
      ? resolvePayload
      : {success: true, alerts: [], rooms: {}})
  })
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
    window,
    run(code) { return new vm.Script(code).runInContext(context) },
    status() { return window.document.getElementById('statusText').textContent },
    async settle() {
      for (let i = 0; i < 5; i += 1) await new Promise(r => setImmediate(r))
    },
  }
}

test('解析失败后状态栏不能还停在「解析中」', async t => {
  const app = createApp(t, {success: false, error: '查询失败，请检查抖音号是否存在'})
  app.run(`connect()`)
  await app.settle()
  assert.ok(!app.status().includes('解析中'),
    `状态栏卡住了：${app.status()}`)
})

test('解析失败的原因要显示在状态栏上，别只靠可能看不见的浮层', async t => {
  const app = createApp(t, {success: false, error: '查询失败，请检查抖音号是否存在'})
  app.run(`connect()`)
  await app.settle()
  assert.match(app.status(), /查询失败/)
})

test('主播未开播也要复位，不能挂着解析中', async t => {
  const app = createApp(t, {success: true, room_id: '123', live_status: 0})
  app.run(`connect()`)
  await app.settle()
  assert.ok(!app.status().includes('解析中'), app.status())
  assert.match(app.status(), /未在直播/)
})

test('返回成功但拿不到直播间信息，同样要复位', async t => {
  const app = createApp(t, {success: true})
  app.run(`connect()`)
  await app.settle()
  assert.ok(!app.status().includes('解析中'), app.status())
})

test('按钮在失败后要恢复可点', async t => {
  const app = createApp(t, {success: false, error: '查询失败'})
  app.run(`connect()`)
  await app.settle()
  assert.equal(false, app.window.document.getElementById('btn').disabled)
})
