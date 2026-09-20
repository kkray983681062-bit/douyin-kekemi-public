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

function createApp(t) {
  const dom = new JSDOM(`<!doctype html><html><head><title></title></head><body>
    <div id="headerArea"></div>
    <input id="input">
    <button id="btn"></button>
    <div id="historyDropdown"></div>
    <div id="statusText"></div>
    <div id="dot"></div>
    <div id="statsText"></div>
    <div id="anonymousBanner"><div id="anonymousMsg"></div></div>
    <button id="modeMystery"></button>
    <button id="modeAll"></button>
    <button id="modeFeed"></button>
    <button id="modeDaily"></button>
    <button id="modeHostWeekly"></button>
    <button id="modeVisitorWeekly"></button>
    <button id="modeGiftAssignments"></button>
    <button id="modeGiftLibrary"></button>
    <div id="roomSelector"></div>
    <div id="events"></div>
    <div id="feedContainer"></div>
  </body></html>`, {
    url: 'http://127.0.0.1:5000/',
    runScripts: 'outside-only'
  })

  const { window } = dom
  window.fetch = async () => ({
    json: async () => ({success: true, map: {}, count: 0, active: [], data: []})
  })
  window.EventSource = class FakeEventSource { close() {} }
  window.setInterval = () => 0
  window.clearInterval = () => {}
  window.anime = {animate: () => ({pause() {}}), stagger: () => 0, spring: () => 0}
  if (!window.CSS) window.CSS = {}
  if (!window.CSS.escape) window.CSS.escape = value => String(value)

  const context = dom.getInternalVMContext()
  new vm.Script(APP_SOURCE, {filename: 'static/app.js'}).runInContext(context)
  t.after(() => window.close())

  return {
    window,
    run(code) { return new vm.Script(code).runInContext(context) },
    feedText() {
      return window.document.getElementById('feedContainer').textContent
    },
    feedHtml() {
      return window.document.getElementById('feedContainer').innerHTML
    },
    eventsText() {
      return window.document.getElementById('events').textContent
    },
    eventsHtml() {
      return window.document.getElementById('events').innerHTML
    }
  }
}

const MATCHED = `{
  identity_id: 7, real_name: '示例用户甲', douyin_id: 'kekeray',
  sec_uid: 'MS4wLjABAAAA-kekeray', follower_count: 1234, aweme_count: 56,
  signature: '', profile_url: 'https://www.douyin.com/user/MS4wLjABAAAA-kekeray',
  matched_by: ['webcast_uid']
}`

test('公屏在 UID 命中时同时显示匿名编号和真实身份', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A厅'}
    selectedRoomId = 'a'
    currentView = 'feed'
    handleEvent({type: 'init', data: {feed: [
      {type: 'chat', display: 'dou8328160', real_name: 'dou8328160',
       content: '大家好', timestamp: 100, matched_identity: ${MATCHED}}
    ]}}, 'a')
    loadFeed()
  `)

  assert.match(app.feedText(), /dou8328160（真实：示例用户甲）/)
})

test('公屏在 UID 未命中时只显示原始匿名编号', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A厅'}
    selectedRoomId = 'a'
    currentView = 'feed'
    handleEvent({type: 'init', data: {feed: [
      {type: 'chat', display: 'dou8328160', real_name: 'dou8328160',
       content: '大家好', timestamp: 100, matched_identity: null}
    ]}}, 'a')
    loadFeed()
  `)

  const text = app.feedText()
  assert.match(text, /dou8328160/)
  assert.doesNotMatch(text, /真实：/)
})

test('已经解析出demo_004的事件行为不变', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A厅'}
    selectedRoomId = 'a'
    currentView = 'feed'
    handleEvent({type: 'init', data: {feed: [
      {type: 'chat', display: '神秘人339175', real_name: '某某',
       content: '大家好', timestamp: 100, matched_identity: null}
    ]}}, 'a')
    loadFeed()
  `)

  assert.match(app.feedText(), /神秘人339175（真实：某某）/)
})

test('SSE 实时事件的匹配结果直接进公屏', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A厅'}
    selectedRoomId = 'a'
    currentView = 'feed'
    handleEvent({type: 'mystery_chat', data: {
      display: '神秘人二阶', real_name: '神秘人二阶', content: '来了',
      timestamp: 200, matched_identity: ${MATCHED}
    }}, 'a')
  `)

  assert.match(app.feedText(), /神秘人二阶（真实：示例用户甲）/)
})

test('神秘人页面在命中时显示demo_004、抖音号、粉丝、作品和主页', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A厅'}
    renderOnlineAudienceData({
      room_id: 'a', room_nickname: 'A厅', updated_at: 100,
      records: [{
        user_key: 'webcast:anon', nickname: 'dou8328160', sec_uid: '',
        webcast_uid: '1000000000000000019', display_id: '',
        is_mystery: true, mystery_resolved: false, mystery_profile: null,
        matched_identity: ${MATCHED}
      }]
    }, true)
  `)

  const text = app.eventsText()
  assert.match(text, /dou8328160/)
  assert.match(text, /示例用户甲/)
  assert.match(text, /抖音号 kekeray/)
  assert.match(text, /粉丝 1,234/)
  assert.match(text, /作品 56/)
  assert.doesNotMatch(text, /身份待解析/)
  assert.match(
    app.eventsHtml(),
    /https:\/\/www\.douyin\.com\/user\/MS4wLjABAAAA-kekeray/
  )
})

test('神秘人页面在未命中时仍然显示身份待解析', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A厅'}
    renderOnlineAudienceData({
      room_id: 'a', room_nickname: 'A厅', updated_at: 100,
      records: [{
        user_key: 'webcast:anon', nickname: 'dou8328160', sec_uid: '',
        webcast_uid: '1000000000000000019', display_id: '',
        is_mystery: true, mystery_resolved: false, mystery_profile: null,
        matched_identity: null
      }]
    }, true)
  `)

  const text = app.eventsText()
  assert.match(text, /dou8328160/)
  assert.match(text, /身份待解析/)
  assert.doesNotMatch(text, /示例用户甲/)
})
