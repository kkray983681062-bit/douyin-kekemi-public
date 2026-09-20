// 公屏礼物行要显示收礼给谁。
//
// 收礼人一直都有：服务端 SSE（web_listener.py:2072）和 /api/feed
// （web_listener.py:3561）两条路径都下发 recipient_name，前端
// normalizeFeedEvent 也一直在带，只是 feedItemHtml 从来没画出来。
// 线上实测：今日 1643 笔礼物 100% 都有收礼人，没有空值要处理。
//
// 送给大头的礼物在服务端会被填成厅名（web_listener.py:2050），
// 所以「空」只可能出现在老数据里，仍然要防一手。

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { JSDOM } = require('jsdom')

const APP_SOURCE = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'app.js'), 'utf8'
)

function createApp(t) {
  const dom = new JSDOM(`<!doctype html><body>
    <div id="events"></div><div id="roomSelector"></div>
    <div id="silencePanel"></div><div id="toastLayer"></div><div id="dot"></div>
    <div id="statusText"></div><div id="statsText"></div>
    <div id="dataPanelTitle"></div>
    <div id="feedPane"><div id="feedContainer"></div>
    <button id="feedNewMessageBtn"></button></div>
  </body>`, {url: 'http://127.0.0.1:5000/', runScripts: 'outside-only'})
  const { window } = dom
  window.KEKEMI_AUTH = {role: 'admin', enabled: false}
  window.fetch = async () => ({json: async () => ({success: true, alerts: []})})
  window.EventSource = class { close() {} }
  window.setInterval = () => 0
  window.clearInterval = () => {}
  window.anime = {animate: () => ({pause() {}}), stagger: () => 0, spring: () => 0}
  if (!window.CSS) window.CSS = {}
  if (!window.CSS.escape) window.CSS.escape = v => String(v)
  const context = dom.getInternalVMContext()
  new vm.Script(APP_SOURCE, {filename: 'static/app.js'}).runInContext(context)
  t.after(() => window.close())
  return {window, run(code) { return new vm.Script(code).runInContext(context) }}
}

test('礼物行显示送给了谁', t => {
  const app = createApp(t)
  const html = app.run(`feedItemHtml({type:'gift', room_id:'r1', display:'神秘人五阶',
    content:'比心', ticket_count:995, recipient_name:'Ry.水星', timestamp:1787000000})`)
  assert.match(html, /神秘人五阶/)
  assert.match(html, /Ry\.水星/)
  assert.match(html, /feed-recipient/, '收礼人要有自己的样式钩子，好跟送礼人区分')
})

test('聊天行不该冒出收礼人', t => {
  const app = createApp(t)
  const html = app.run(`feedItemHtml({type:'chat', room_id:'r1', display:'天晴了',
    content:'来啦', recipient_name:'Ry.水星', timestamp:1787000000})`)
  assert.doesNotMatch(html, /feed-recipient/)
  assert.doesNotMatch(html, /Ry\.水星/, '聊天没有收礼人这回事，脏数据也不许显示')
})

test('收礼人为空时整段不显示，不留一个孤零零的箭头', t => {
  const app = createApp(t)
  for (const value of ["''", 'undefined', 'null', "'   '"]) {
    const html = app.run(`feedItemHtml({type:'gift', room_id:'r1', display:'A',
      content:'小心心', ticket_count:3, recipient_name:${value}, timestamp:1787000000})`)
    assert.doesNotMatch(html, /feed-recipient/, String(value))
    assert.doesNotMatch(html, /→/, String(value))
  }
})

test('收礼人名字要转义，昵称里带尖括号不能变成标签', t => {
  const app = createApp(t)
  const html = app.run(`feedItemHtml({type:'gift', room_id:'r1', display:'A',
    content:'小心心', ticket_count:3, recipient_name:'<img src=x onerror=alert(1)>',
    timestamp:1787000000})`)
  assert.doesNotMatch(html, /<img/)
  assert.match(html, /&lt;img/)
})

test('两条下发路径都要把收礼人带进来', t => {
  // 这个功能真正的风险不在渲染，在「一条路径带了另一条没带」。
  // 本仓库栽过两次同类问题，所以两条都测。
  const app = createApp(t)
  const fromSse = app.run(`normalizeFeedEvent({type:'mystery_gift',
    data:{room_id:'r1', display:'A', gift_name:'比心', recipient_name:'Ry.水星',
    event_key:'k1', timestamp:1787000000}}, 'r1')`)
  const fromFeed = app.run(`normalizeFeedEvent({type:'gift', room_id:'r1',
    display:'A', content:'比心', recipient_name:'Ry.水星', combo_key:'k1',
    timestamp:1787000000}, 'r1')`)
  assert.equal(fromSse.recipient_name, 'Ry.水星', 'SSE 路径')
  assert.equal(fromFeed.recipient_name, 'Ry.水星', '/api/feed 路径')
})

test('端到端：灌一条礼物进去，公屏上能看到收礼人', t => {
  const app = createApp(t)
  app.run(`
    selectedRoomId = 'r1'
    currentView = 'feed'
    mergeRoomFeed('r1', [{type:'gift', room_id:'r1', display:'⁃̀⩊⁃́',
      content:'浪漫花火', ticket_count:599, recipient_name:'Ry.纸',
      combo_key:'g1', timestamp:1787000000}], true)
    renderFeed(feedByRoom['r1'], 'r1')
  `)
  const html = app.window.document.getElementById('feedContainer').innerHTML
  assert.match(html, /浪漫花火/)
  assert.match(html, /Ry\.纸/)
})
