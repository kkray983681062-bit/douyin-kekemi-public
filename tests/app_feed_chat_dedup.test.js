// 公屏聊天重复的回归测试。
//
// 重复的来源不是入库——库里靠 (room_id, event_key) 唯一索引早就干净了
// （线上近 1 小时 1393 条聊天，同(厅,名,内容)差 0 秒的重复 0 条）。
// 问题在下发：聊天事件不带 timestamp，前端只能拿 Date.now() 顶上
// （static/app.js 的 normalizeFeedEvent），而 /api/feed 返回的是服务器
// 接收秒。同一条消息两边算出的去重键必然不同，于是渲染两遍。
//
// 礼物早就不踩这个坑，因为它用 combo_key 当键、不看时间戳。聊天照做。

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

const KEY = 'chat-9a20d9b9cdea085f'

test('同一条聊天，SSE 与 /api/feed 的时间戳对不上时也要判成一条', t => {
  const app = createApp(t)
  // 服务器接收秒 1786979456，浏览器收到时已经是 1786979458——
  // 时钟偏移 + 网络延迟下这是常态，不是偶发。
  const fromFeed = `{type:'chat', room_id:'r1', display:'Ry.灰', content:'喵喵2',
                     combo_key:'${KEY}', timestamp:1786979456}`
  const fromSse = `{type:'chat', room_id:'r1', display:'Ry.灰', content:'喵喵2',
                    combo_key:'${KEY}', timestamp:1786979458}`
  const a = app.run(`feedEventKey(${fromFeed})`)
  const b = app.run(`feedEventKey(${fromSse})`)
  assert.equal(a, b, '带了 event_key 就该按它去重，不该再看时间戳')
})

test('抖音重推同一条，两次到达跨秒也要判成一条', t => {
  const app = createApp(t)
  const push1 = `{type:'chat', room_id:'r1', display:'天晴了', content:'@Ry.零肆 能不能排个下午档',
                  combo_key:'chat-72da996a9bc730ad', timestamp:1786979456}`
  const push2 = `{type:'chat', room_id:'r1', display:'天晴了', content:'@Ry.零肆 能不能排个下午档',
                  combo_key:'chat-72da996a9bc730ad', timestamp:1786979457}`
  assert.equal(app.run(`feedEventKey(${push1})`), app.run(`feedEventKey(${push2})`))
})

test('内容相同但是两条真实弹幕（键不同）不能被并掉', t => {
  const app = createApp(t)
  const first = `{type:'chat', room_id:'r1', display:'kk大王', content:'[打call]',
                  combo_key:'chat-aaaaaaaaaaaaaaaa', timestamp:1786971900}`
  const second = `{type:'chat', room_id:'r1', display:'kk大王', content:'[打call]',
                   combo_key:'chat-bbbbbbbbbbbbbbbb', timestamp:1786971955}`
  assert.notEqual(app.run(`feedEventKey(${first})`), app.run(`feedEventKey(${second})`))
})

test('同一个键在不同厅要分开，别把两个厅的公屏并成一条', t => {
  const app = createApp(t)
  const r1 = `{type:'chat', room_id:'r1', display:'A', content:'x', combo_key:'${KEY}', timestamp:1}`
  const r2 = `{type:'chat', room_id:'r2', display:'A', content:'x', combo_key:'${KEY}', timestamp:1}`
  assert.notEqual(app.run(`feedEventKey(${r1})`), app.run(`feedEventKey(${r2})`))
})

test('没有 event_key 的老事件仍走原来的兜底键，不能直接崩或全并成一条', t => {
  const app = createApp(t)
  const a = `{type:'chat', room_id:'r1', display:'A', content:'一', timestamp:100}`
  const b = `{type:'chat', room_id:'r1', display:'A', content:'二', timestamp:100}`
  const again = `{type:'chat', room_id:'r1', display:'A', content:'一', timestamp:100}`
  assert.equal(app.run(`feedEventKey(${a})`), app.run(`feedEventKey(${again})`))
  assert.notEqual(app.run(`feedEventKey(${a})`), app.run(`feedEventKey(${b})`))
})

test('端到端：先从 /api/feed 灌入，再来一条时间戳不同的同一条 SSE，公屏只出现一次', t => {
  const app = createApp(t)
  app.run(`
    selectedRoomId = 'r1'
    currentView = 'feed'
    mergeRoomFeed('r1', [{type:'chat', room_id:'r1', display:'Ry.灰',
      content:'喵喵2', combo_key:'${KEY}', timestamp:1786979456}], true)
    appendFeedItem({type:'mystery_chat', data:{room_id:'r1', display:'Ry.灰',
      content:'喵喵2', event_key:'${KEY}', timestamp:1786979458}}, 'r1')
  `)
  const n = app.run(`(feedByRoom['r1'] || []).filter(e => e.content === '喵喵2').length`)
  assert.equal(n, 1, '同一条消息在缓存里只该有一条')
})
