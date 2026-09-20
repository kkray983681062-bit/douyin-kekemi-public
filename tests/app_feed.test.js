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

function createApp(t, {fetchImpl} = {}) {
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
  </body></html>`, {
    url: 'http://127.0.0.1:5000/',
    runScripts: 'outside-only'
  })

  const { window } = dom
  window.fetch = fetchImpl || (async () => ({
    json: async () => ({success: true, map: {}, count: 0, active: [], data: []})
  }))
  window.EventSource = class FakeEventSource {
    close() {}
  }
  window.setInterval = () => 0
  window.clearInterval = () => {}
  window.anime = {
    animate: () => ({pause() {}}),
    stagger: () => 0,
    spring: () => 0
  }
  if (!window.CSS) window.CSS = {}
  if (!window.CSS.escape) window.CSS.escape = value => String(value)

  const context = dom.getInternalVMContext()
  new vm.Script(APP_SOURCE, {filename: 'static/app.js'}).runInContext(context)
  t.after(() => window.close())

  return {
    window,
    run(code) {
      return new vm.Script(code).runInContext(context)
    },
    feedText() {
      return window.document.getElementById('feedContainer').textContent
    },
    eventsText() {
      return window.document.getElementById('events').textContent
    },
    async flush() {
      await new Promise(resolve => setImmediate(resolve))
      await new Promise(resolve => setImmediate(resolve))
    }
  }
}

function jsonResponse(data) {
  return {json: async () => data}
}

// 公屏冻结相关测试的页内辅助函数：jsdom 没有布局，需要自己伪造滚动尺寸并手动派发 scroll。
const FEED_HELPERS = `
function patchFeedLayout() {
  const feed = document.getElementById('feedContainer')
  if (feed.layoutPatched) return feed
  feed.layoutPatched = true
  Object.defineProperty(feed, 'clientHeight', {value: 200, configurable: true})
  Object.defineProperty(feed, 'scrollHeight', {
    configurable: true,
    get() { return this.children.length * 20 }
  })
  return feed
}
function seedFeed(roomId, count, prefix, startTs) {
  feedByRoom[roomId] = Array.from({length: count}, (_, index) => ({
    type: 'chat', room_id: roomId, display: '游客' + index,
    content: prefix + index, timestamp: startTs + index
  }))
  return feedByRoom[roomId]
}
function chatEvent(roomId, content, timestamp) {
  return {type: 'mystery_chat', data: {
    room_id: roomId, display: '新游客', content: content, timestamp: timestamp
  }}
}
function scrollFeedTo(top) {
  const feed = document.getElementById('feedContainer')
  feed.scrollTop = top
  feed.dispatchEvent(new Event('scroll'))
  return feed
}
function newMessageButtonText() {
  const btn = document.getElementById('feedNewMessageBtn')
  return btn.style.display === 'none' ? '' : btn.textContent
}
function feedScrollTop() { return document.getElementById('feedContainer').scrollTop }
function feedScrollHeight() { return document.getElementById('feedContainer').scrollHeight }
`

function createFeedApp(t, options) {
  const app = createApp(t, options)
  app.run(FEED_HELPERS)
  return app
}

// 打开某个房间的公屏：写入 size 条缓存并渲染到底部。
function openFeedRoom(app, roomId, size = app.run(`FEED_MAX_ITEMS`), prefix = '旧消息', startTs = 1000) {
  app.run(`
    currentRooms['${roomId}'] = {nickname: '${roomId}厅'}
    currentView = 'feed'
    selectedRoomId = '${roomId}'
    patchFeedLayout()
    seedFeed('${roomId}', ${size}, '${prefix}', ${startTs})
    renderFeed(feedByRoom['${roomId}'], '${roomId}')
  `)
}

function roomIdFromFeedUrl(url) {
  const match = String(url).match(/^\/api\/feed\/([^?]+)/)
  return match ? decodeURIComponent(match[1]) : null
}

test('switching rooms renders only the selected room feed', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentRooms.b = {nickname: 'B'}
    handleEvent({type: 'init', data: {feed: [
      {type: 'chat', display: '甲', content: 'A消息', timestamp: 100}
    ]}}, 'a')
    handleEvent({type: 'init', data: {feed: [
      {type: 'chat', display: '乙', content: 'B消息', timestamp: 101}
    ]}}, 'b')
    currentView = 'feed'
    selectRoom('a')
  `)

  assert.match(app.feedText(), /A消息/)
  assert.doesNotMatch(app.feedText(), /B消息/)

  app.run(`selectRoom('b')`)
  assert.match(app.feedText(), /B消息/)
  assert.doesNotMatch(app.feedText(), /A消息/)
})

test('unselected room realtime chat is cached until that room is selected', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentRooms.b = {nickname: 'B'}
    currentView = 'feed'
    selectRoom('a')
    handleEvent({type: 'mystery_chat', data: {
      room_id: 'b', display: '乙', content: '后台消息', timestamp: 102
    }}, 'b')
  `)

  assert.doesNotMatch(app.feedText(), /后台消息/)
  app.run(`selectRoom('b')`)
  assert.match(app.feedText(), /后台消息/)
})

test('the SSE connection room is the isolation boundary for realtime events', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentRooms.b = {nickname: 'B'}
    currentView = 'feed'
    selectRoom('a')
    handleEvent({type: 'mystery_chat', data: {
      room_id: 'b', display: '甲', content: '只属于A厅', timestamp: 102.5
    }}, 'a')
  `)

  assert.match(app.feedText(), /只属于A厅/)
  app.run(`selectRoom('b')`)
  assert.doesNotMatch(app.feedText(), /只属于A厅/)
})

test('an anonymous chat delivered twice with different sec_uid shows once', t => {
  // 匿名房同一个人的 sec_uid 每次进出都变，抖音又会把同一条弹幕推两次。
  // 去重键若掺 sec_uid，两次就判成两条，公屏出现重复。
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentView = 'feed'
    selectRoom('a')
    handleEvent({type: 'mystery_chat', data: {
      room_id: 'a', sec_uid: 'MS4wLjAAsecone', display: 'dou7101055',
      content: 'test', timestamp: 200
    }}, 'a')
    handleEvent({type: 'mystery_chat', data: {
      room_id: 'a', sec_uid: 'MS4wLjBBsectwo', display: 'dou7101055',
      content: 'test', timestamp: 200
    }}, 'a')
  `)

  assert.equal(app.run(`feedByRoom.a.filter(e => e.content === 'test').length`), 1)
})

test('the feed never shows room-enter notices', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentView = 'feed'
    selectRoom('a')
    handleEvent({type: 'mystery_enter', data: {
      room_id: 'a', sec_uid: 'u1', display: '进场的人', timestamp: 100
    }}, 'a')
    handleEvent({type: 'mystery_chat', data: {
      room_id: 'a', sec_uid: 'u2', display: '发言的人', content: '你好', timestamp: 101
    }}, 'a')
  `)

  assert.doesNotMatch(app.feedText(), /进入了直播间/)
  assert.doesNotMatch(app.feedText(), /进场的人/)
  assert.match(app.feedText(), /发言的人/)
  assert.equal(app.run(`feedByRoom.a.length`), 1)
})

test('enter events from SSE history and the API are dropped before caching', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentView = 'feed'
    handleEvent({type: 'init', data: {feed: [
      {type: 'enter', room_id: 'a', display: '历史进场', timestamp: 90},
      {type: 'chat', room_id: 'a', display: '历史发言', content: '在么', timestamp: 91}
    ]}}, 'a')
    selectRoom('a')
  `)

  assert.equal(app.run(`feedByRoom.a.length`), 1)
  assert.doesNotMatch(app.feedText(), /历史进场/)
  assert.match(app.feedText(), /历史发言/)
})

test('resolved anonymous name keeps both the anonymous id and real name', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentView = 'feed'
    selectRoom('a')
    handleEvent({type: 'mystery_chat', data: {
      room_id: 'a', display: 'dou7101055', real_name: '示例用户甲',
      content: '测试', timestamp: 103
    }}, 'a')
  `)

  assert.match(app.feedText(), /dou7101055（真实：示例用户甲）/)
})

test('full feed refresh keeps the reading position when user scrolled up', t => {
  const app = createApp(t)

  const scrollTop = app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'feed'
    const feed = document.getElementById('feedContainer')
    Object.defineProperty(feed, 'clientHeight', {value: 200, configurable: true})
    Object.defineProperty(feed, 'scrollHeight', {
      configurable: true,
      get() { return this.children.length * 20 }
    })
    feedByRoom.a = Array.from({length: 500}, (_, index) => ({
      type: 'chat', room_id: 'a', display: '游客' + index,
      content: '旧消息' + index, timestamp: 1000 + index
    }))
    renderFeed(feedByRoom.a, 'a')
    feed.scrollTop = 2400
    appendFeedItem({type: 'mystery_chat', data: {
      room_id: 'a', display: '新游客', content: '最新消息', timestamp: 2000
    }}, 'a')
    feed.scrollTop
  `)

  assert.equal(scrollTop, 2400)
})

test('selecting a room actively loads and shows only that room feed', async t => {
  const feeds = {
    a: [{type: 'chat', room_id: 'a', display: '甲', content: 'A接口消息', timestamp: 201}],
    b: [{type: 'chat', room_id: 'b', display: '乙', content: 'B接口消息', timestamp: 202}]
  }
  const app = createApp(t, {
    fetchImpl: async url => {
      const roomId = roomIdFromFeedUrl(url)
      if (roomId) return jsonResponse({success: true, events: feeds[roomId] || []})
      return jsonResponse({success: true, map: {}, count: 0, active: [], data: []})
    }
  })

  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentRooms.b = {nickname: 'B'}
    currentView = 'feed'
    selectRoom('a')
  `)
  await app.flush()
  assert.match(app.feedText(), /A接口消息/)
  assert.doesNotMatch(app.feedText(), /B接口消息/)

  app.run(`selectRoom('b')`)
  await app.flush()
  assert.match(app.feedText(), /B接口消息/)
  assert.doesNotMatch(app.feedText(), /A接口消息/)
})

test('a slow previous-room response cannot overwrite the newly selected room', async t => {
  const pending = {}
  const app = createApp(t, {
    fetchImpl: url => {
      const roomId = roomIdFromFeedUrl(url)
      if (!roomId) {
        return Promise.resolve(jsonResponse({success: true, map: {}, count: 0, active: [], data: []}))
      }
      return new Promise(resolve => {
        pending[roomId] = events => resolve(jsonResponse({success: true, events}))
      })
    }
  })

  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentRooms.b = {nickname: 'B'}
    currentView = 'feed'
    selectRoom('a')
    selectRoom('b')
  `)
  assert.equal(typeof pending.a, 'function')
  assert.equal(typeof pending.b, 'function')

  pending.b([{type: 'chat', room_id: 'b', display: '乙', content: 'B最新消息', timestamp: 302}])
  await app.flush()
  assert.match(app.feedText(), /B最新消息/)

  pending.a([{type: 'chat', room_id: 'a', display: '甲', content: 'A迟到消息', timestamp: 301}])
  await app.flush()
  assert.match(app.feedText(), /B最新消息/)
  assert.doesNotMatch(app.feedText(), /A迟到消息/)
})

test('gift value text shows diamonds only when price is verified', t => {
  const app = createApp(t)

  const known = app.run(`giftValueText({
    gift_quantity: 5,
    unit_diamonds: 99,
    total_diamonds: 495,
    price_known: true,
    quantity_verified: true
  })`)
  const unknown = app.run(`giftValueText({
    gift_quantity: 5,
    price_known: false,
    quantity_verified: true
  })`)

  assert.equal(known, '×5｜单价 99 钻石｜合计 495 钻石')
  assert.equal(unknown, '×5｜价格未知')
  assert.doesNotMatch(known + unknown, /人民币|RMB|¥|￥/)
})

test('page timestamps are rendered in Beijing time', t => {
  const app = createApp(t)
  assert.match(app.run(`formatTime(1)`), /08:00:01/)
})

test('all-user gift summary separates gift events from gift quantity', t => {
  const app = createApp(t)

  const summary = app.run(`giftSummaryText({
    gift_event_count: 1,
    gift_quantity_total: 5
  })`)

  assert.equal(summary, '🎁1次/5个')
})

test('mystery page is the current-online subset and never loads history cards', async t => {
  const calls = []
  const app = createApp(t, {
    fetchImpl: async url => {
      calls.push(String(url))
      if (url === '/api/online/a') {
        return jsonResponse({success: true, room_id: 'a', count: 3,
          updated_at: 100, stale: false, records: [
            {user_key: 'sec:mystery', sec_uid: 'mystery', nickname: 'demo_004',
             display_id: 'real-id', is_mic: false, known_diamonds: 99,
             is_mystery: true, mystery_profile: {
               display: '神秘人123', real_name: 'demo_004',
               unique_id: 'real-id', follower_count: 20, aweme_count: 3
             }},
            {user_key: 'sec:ordinary', sec_uid: 'ordinary', nickname: '普通观众',
              display_id: 'ordinary-id', is_mic: false, known_diamonds: 500,
              is_mystery: false},
            {user_key: 'webcast:anonymous', sec_uid: '', webcast_uid: 'anonymous',
              nickname: 'dou7101055', display_id: '', is_mic: false,
              known_diamonds: 0, is_mystery: true, mystery_profile: null}
          ]})
      }
      return jsonResponse({success: true, map: {}, count: 0, active: [], data: []})
    }
  })

  app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'mystery'
    renderMysteries()
  `)
  await app.flush()

  assert.match(app.eventsText(), /神秘人123/)
  assert.match(app.eventsText(), /demo_004/)
  assert.match(app.eventsText(), /dou7101055/)
  assert.match(app.eventsText(), /身份待解析/)
  assert.doesNotMatch(app.eventsText(), /普通观众/)
  assert.ok(calls.includes('/api/online/a'))
  assert.ok(!calls.some(url => url.startsWith('/api/history/')))
})

test('anonymous-room events keep working without rendering a banner', t => {
  const app = createApp(t)

  app.run(`handleEvent({
    type: 'room_anonymous',
    data: {message: '当前为匿名模式直播间，仅能通过礼物获取用户真实身份。'}
  }, 'a')`)

  assert.equal(app.window.document.querySelector('.anonymous-banner'), null)
  assert.equal(app.run(`currentView`), 'mystery')
})

test('collapsing the data panel keeps live feed DOM and scroll state intact', t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a', 20)

  const before = app.run(`({
    html: document.getElementById('feedContainer').innerHTML,
    top: feedScrollTop()
  })`)
  const collapsed = app.run(`
    toggleDataPanel()
    ;({
      collapsed: document.getElementById('dataPanel').classList.contains('is-collapsed'),
      expanded: document.getElementById('dataPanelToggle').getAttribute('aria-expanded'),
      label: document.getElementById('dataPanelToggle').textContent,
      html: document.getElementById('feedContainer').innerHTML,
      top: feedScrollTop()
    })
  `)

  assert.equal(collapsed.collapsed, true)
  assert.equal(collapsed.expanded, 'false')
  assert.equal(collapsed.label, '展开 ↓')
  assert.equal(collapsed.html, before.html)
  assert.equal(collapsed.top, before.top)

  app.run(`appendFeedItem(chatEvent('a', '折叠期间的新消息', 3000), 'a')`)
  assert.match(app.feedText(), /折叠期间的新消息/)
  assert.equal(app.run(`document.getElementById('dataPanel').classList.contains('is-collapsed')`), true)

  const expanded = app.run(`
    toggleDataPanel()
    ;({
      collapsed: document.getElementById('dataPanel').classList.contains('is-collapsed'),
      expanded: document.getElementById('dataPanelToggle').getAttribute('aria-expanded'),
      label: document.getElementById('dataPanelToggle').textContent
    })
  `)
  assert.equal(expanded.collapsed, false)
  assert.equal(expanded.expanded, 'true')
  assert.equal(expanded.label, '收起 ↑')
})

test('the compact data bar follows the selected room and current mode', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: '示例·RUYUE'}
    selectedRoomId = 'a'
    switchMode('feed')
  `)

  assert.equal(
    app.window.document.getElementById('dataPanelTitle').textContent,
    '🖥 公屏 · 示例·RUYUE'
  )
})

// ========== 外部文本不得进入 JS 执行上下文 ==========

const EVIL_TEXT = "夜猫'); alert(1); ('"

test('the addable-room dropdown never puts room data into inline JS', t => {
  const app = createApp(t)
  const result = app.run(`
    localStorage.clear()
    currentRooms[${JSON.stringify(EVIL_TEXT)}] = {nickname: "坏昵称'"}
    currentRooms.b = {nickname: 'B厅'}
    hideRoom(${JSON.stringify(EVIL_TEXT)})
    const dd = document.getElementById('historyDropdown')
    globalThis.shown = null
    showRoom = function(roomId) { globalThis.shown = roomId }
    dd.querySelector('.item').click()
    ;({
      inlineHandlers: dd.innerHTML.includes('onclick'),
      shown: globalThis.shown
    })
  `)

  assert.equal(result.inlineHandlers, false)
  assert.equal(result.shown, EVIL_TEXT)
})

test('the room bar hide button never puts room ids into inline JS', t => {
  const app = createApp(t)
  const result = app.run(`
    localStorage.clear()
    currentRooms[${JSON.stringify(EVIL_TEXT)}] = {nickname: '怪名字厅'}
    selectedRoomId = ${JSON.stringify(EVIL_TEXT)}
    updateRoomSelector()
    const selector = document.getElementById('roomSelector')
    globalThis.hidden = null
    hideRoom = function(roomId) { globalThis.hidden = roomId }
    selector.querySelector('.room-hide-btn').click()
    ;({
      inlineHandlers: selector.innerHTML.includes('onclick'),
      hidden: globalThis.hidden
    })
  `)

  assert.equal(result.inlineHandlers, false)
  assert.equal(result.hidden, EVIL_TEXT)
})

test('room filter buttons select rooms without inline JS', t => {
  const app = createApp(t)
  const selected = app.run(`
    currentRooms[${JSON.stringify(EVIL_TEXT)}] = {nickname: '怪名字厅'}
    currentRooms.b = {nickname: 'B厅'}
    selectedRoomId = 'b'
    updateRoomSelector()
    globalThis.chosen = null
    selectRoom = function(roomId) { globalThis.chosen = roomId }
    const selector = document.getElementById('roomSelector')
    ;({
      inlineHandlers: selector.innerHTML.includes('onclick'),
      chosen: (selector.querySelector('.room-filter-btn').click(), globalThis.chosen)
    })
  `)

  assert.equal(selected.inlineHandlers, false)
  assert.equal(selected.chosen, EVIL_TEXT)
})

test('the stop dialog stops the right room without inline JS', t => {
  const app = createApp(t)
  const result = app.run(`
    currentRooms[${JSON.stringify(EVIL_TEXT)}] = {nickname: '怪名字厅'}
    currentRooms.b = {nickname: 'B厅'}
    showStopDialog()
    globalThis.stopped = null
    stopRoomAndClose = function(roomId) { globalThis.stopped = roomId }
    const overlay = document.getElementById('stopOverlay')
    const markup = overlay.innerHTML
    overlay.querySelector('.stop-item').click()
    ;({
      inlineStopItem: markup.includes('stopRoomAndClose('),
      stopped: globalThis.stopped
    })
  `)

  assert.equal(result.inlineStopItem, false)
  assert.equal(result.stopped, EVIL_TEXT)
})

// ========== 在线名单定时刷新不打断阅读 ==========

function onlineRoster(count, tag) {
  return {
    success: true, room_id: 'a', count: count, updated_at: 100, stale: false,
    records: Array.from({length: count}, (_, index) => ({
      user_key: 'sec:u' + index, sec_uid: 'u' + index,
      nickname: tag + index, display_id: 'id' + index,
      is_mic: false, known_diamonds: 1000 - index, rank: index + 1,
      is_mystery: true, mystery_profile: {
        display: tag + index, real_name: tag + index,
        unique_id: 'id' + index, follower_count: 1, aweme_count: 1
      }
    }))
  }
}

function createRosterApp(t, getPayload) {
  return createApp(t, {
    fetchImpl: async url => {
      if (String(url).startsWith('/api/online/')) return jsonResponse(getPayload())
      return jsonResponse({success: true, map: {}, count: 0, active: [], data: []})
    }
  })
}

// 插入哨兵后再渲染：哨兵还在 = 没有整块重绘。
const SENTINEL = `
function plantSentinel() {
  document.getElementById('events')
    .insertAdjacentHTML('beforeend', '<div id="repaintSentinel"></div>')
}
function sentinelSurvived() {
  return Boolean(document.getElementById('repaintSentinel'))
}
`

test('an unchanged online roster is not repainted on the 10s refresh', async t => {
  let payload = onlineRoster(8, '观众')
  const app = createRosterApp(t, () => payload)
  app.run(SENTINEL)

  app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'mystery'
    renderOnlineAudience(true)
  `)
  await app.flush()
  assert.match(app.eventsText(), /观众0/)

  app.run(`plantSentinel()`)
  app.run(`renderOnlineAudience(true)`)
  await app.flush()

  assert.equal(app.run(`sentinelSurvived()`), true)
})

test('a changed online roster is repainted', async t => {
  let payload = onlineRoster(8, '观众')
  const app = createRosterApp(t, () => payload)
  app.run(SENTINEL)

  app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'mystery'
    renderOnlineAudience(true)
  `)
  await app.flush()

  app.run(`plantSentinel()`)
  payload = onlineRoster(9, '观众')
  app.run(`renderOnlineAudience(true)`)
  await app.flush()

  assert.equal(app.run(`sentinelSurvived()`), false)
  assert.match(app.eventsText(), /观众8/)
})

test('repainting the online roster keeps the reading position', async t => {
  let payload = onlineRoster(40, '观众')
  const app = createRosterApp(t, () => payload)

  app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'mystery'
    const events = document.getElementById('events')
    Object.defineProperty(events, 'clientHeight', {value: 200, configurable: true})
    Object.defineProperty(events, 'scrollHeight', {
      configurable: true,
      get() { return this.children.length * 40 }
    })
    // jsdom 没有布局，不会像浏览器那样在内容被整块换掉时把 scrollTop 夹回 0。
    // 这里补上这个行为，否则「保留阅读位置」的断言没有鉴别力。
    const innerHTMLDesc = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML')
    Object.defineProperty(events, 'innerHTML', {
      configurable: true,
      get() { return innerHTMLDesc.get.call(this) },
      set(value) { innerHTMLDesc.set.call(this, value); this.scrollTop = 0 }
    })
    renderOnlineAudience(true)
  `)
  await app.flush()

  app.run(`document.getElementById('events').scrollTop = 500`)
  payload = onlineRoster(41, '观众')
  app.run(`renderOnlineAudience(true)`)
  await app.flush()

  assert.equal(app.run(`document.getElementById('events').scrollTop`), 500)
})

test('returning from another view repaints even when the roster is unchanged', async t => {
  const payload = onlineRoster(8, '观众')
  const app = createRosterApp(t, () => payload)

  app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'mystery'
    renderOnlineAudience(true)
  `)
  await app.flush()

  // 别的页面占用了同一个 events 容器
  app.run(`document.getElementById('events').innerHTML = '<div>别的页面</div>'`)
  app.run(`renderOnlineAudience(true)`)
  await app.flush()

  assert.doesNotMatch(app.eventsText(), /别的页面/)
  assert.match(app.eventsText(), /观众0/)
})

test('template does not expose the history mode', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '..', 'templates', 'index.html'), 'utf8'
  )
  assert.doesNotMatch(source, /id="modeHistory"/)
  assert.doesNotMatch(source, />📜历史</)
  assert.doesNotMatch(source, /id="modeHostGifts"/)
  assert.doesNotMatch(source, />🎁收礼</)
  assert.match(source, /id="modeDaily"/)
  assert.match(source, /id="modeHostWeekly"/)
  assert.match(source, /id="modeVisitorWeekly"/)
  assert.match(source, /id="modeGiftAssignments"/)
})

test('all page shows only the selected room current online roster', async t => {
  const calls = []
  const app = createApp(t, {
    fetchImpl: async url => {
      calls.push(String(url))
      if (url === '/api/online/a') {
        return jsonResponse({success: true, room_id: 'a', count: 2,
          updated_at: 100, stale: false, records: [
            {user_key: 'sec:viewer-a', sec_uid: 'viewer-a', nickname: '游客A',
             display_id: 'a-viewer', is_mic: false, known_diamonds: 500, rank: 1},
            {user_key: 'sec:host-a', sec_uid: 'host-a', nickname: '主持A',
             display_id: 'a-host', is_mic: true, mic_slot: 1,
             received_tickets: 16401, known_diamonds: 1, rank: 2}
          ]})
      }
      return jsonResponse({success: true, map: {}, count: 0, active: [], data: []})
    }
  })

  app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'all'
    renderAllRecords()
  `)
  await app.flush()

  assert.match(app.eventsText(), /主持A/)
  assert.match(app.eventsText(), /游客A/)
  assert.ok(app.eventsText().indexOf('主持A') < app.eventsText().indexOf('游客A'))
  assert.match(app.eventsText(), /16,401 票/)
  assert.doesNotMatch(app.eventsText(), /a-host1 票/)
  assert.match(app.eventsText(), /500 票/)
  assert.match(app.eventsText(), /北京时间今日0点至现在/)
  assert.doesNotMatch(app.eventsText(), /近3天/)
  assert.ok(calls.includes('/api/online/a'))
  assert.ok(!calls.some(url => url.startsWith('/api/all_records/')))
})

test('a late online response from the previous room cannot overwrite the selected room', async t => {
  const pending = {}
  const app = createApp(t, {
    fetchImpl: url => {
      if (!String(url).startsWith('/api/online/')) {
        return Promise.resolve(jsonResponse({success: true, map: {}, count: 0, active: []}))
      }
      const roomId = decodeURIComponent(String(url).split('/').pop())
      return new Promise(resolve => {
        pending[roomId] = records => resolve(jsonResponse({
          success: true, room_id: roomId, count: records.length,
          updated_at: 100, stale: false, records
        }))
      })
    }
  })

  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentRooms.b = {nickname: 'B'}
    currentView = 'all'
    selectRoom('a')
    selectRoom('b')
  `)
  assert.equal(typeof pending.a, 'function')
  assert.equal(typeof pending.b, 'function')

  pending.b([{user_key: 'sec:b', nickname: 'B厅在线', is_mic: false,
              known_diamonds: 0, rank: 1}])
  await app.flush()
  assert.match(app.eventsText(), /B厅在线/)

  pending.a([{user_key: 'sec:a', nickname: 'A厅迟到', is_mic: false,
              known_diamonds: 0, rank: 1}])
  await app.flush()
  assert.match(app.eventsText(), /B厅在线/)
  assert.doesNotMatch(app.eventsText(), /A厅迟到/)
})

test('gift library saves one global manual price with reason', async t => {
  const calls = []
  const app = createApp(t, {
    fetchImpl: async (url, options = {}) => {
      calls.push({url: String(url), options})
      if (url === '/api/admin/gifts') {
        return jsonResponse({success: true, pending_count: 1, records: [{
          gift_id: 'gift-9', gift_name: '雷霆一击', unit_diamonds: null,
          price_source: 'unknown', price_pending: true, occurrence_count: 2,
          first_seen: 90, last_seen: 100, note: ''
        }]})
      }
      if (url === '/api/admin/gifts/gift-9' && options.method === 'PUT') {
        return jsonResponse({success: true, recalculated_rows: 3, gift: {
          gift_id: 'gift-9', gift_name: '雷霆一击', unit_diamonds: 520,
          price_source: 'manual', price_pending: false
        }})
      }
      return jsonResponse({success: true, map: {}, count: 0, active: []})
    }
  })

  app.run(`currentView = 'giftLibrary'; renderGiftLibrary()`)
  await app.flush()
  assert.match(app.eventsText(), /价格待补/)
  assert.match(app.eventsText(), /雷霆一击/)

  app.run(`
    document.getElementById('gift-price-0').value = '520'
    document.getElementById('gift-reason-0').value = '现场确认价格'
    document.getElementById('gift-note-0').value = '管理员补录'
    saveGiftPrice(0)
  `)
  await app.flush()

  const put = calls.find(call => call.url === '/api/admin/gifts/gift-9')
  assert.ok(put)
  assert.equal(put.options.method, 'PUT')
  assert.deepEqual(JSON.parse(put.options.body), {
    gift_name: '雷霆一击', unit_diamonds: 520,
    note: '管理员补录', reason: '现场确认价格'
  })
})

test('gift library blocks zero price or blank reason before request', async t => {
  const calls = []
  const app = createApp(t, {
    fetchImpl: async (url, options = {}) => {
      calls.push({url: String(url), options})
      if (url === '/api/admin/gifts') {
        return jsonResponse({success: true, pending_count: 1, records: [{
          gift_id: 'gift-0', gift_name: '未知礼物', unit_diamonds: null,
          price_source: 'unknown', price_pending: true, occurrence_count: 1,
          first_seen: 90, last_seen: 100, note: ''
        }]})
      }
      return jsonResponse({success: true})
    }
  })

  app.run(`currentView = 'giftLibrary'; renderGiftLibrary()`)
  await app.flush()
  app.run(`
    document.getElementById('gift-price-0').value = '0'
    document.getElementById('gift-reason-0').value = '   '
    saveGiftPrice(0)
  `)
  await app.flush()

  assert.equal(calls.filter(call => call.options.method === 'PUT').length, 0)
  assert.match(app.eventsText(), /正整数|修改原因/)
})

test('public daily view renders aligned visitor and host columns for one room', async t => {
  const calls = []
  const app = createApp(t, {
    fetchImpl: async url => {
      calls.push(String(url))
      if (url === '/api/daily_rank/a') {
        return jsonResponse({success: true, room_id: 'a', scope: 'beijing_today',
          note: '仅统计今天监听期间', visitors: [
            {rank: 1, sender_key: 'a-user', display: '游客A', tickets: 2752}
          ], hosts: [
            {rank: 1, recipient_key: 'host-a', display: '主持A', tickets: 6880}
          ]})
      }
      return jsonResponse({success: true, map: {}, count: 0, active: [], data: []})
    }
  })

  assert.equal(app.run(`typeof renderDailyRanks`), 'function')
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentView = 'daily'
    selectRoom('a')
  `)
  await app.flush()
  const columns = app.window.document.querySelectorAll('.daily-rank-column')
  assert.equal(columns.length, 2)
  assert.match(columns[0].textContent, /游客日榜/)
  assert.match(columns[0].textContent, /游客A/)
  assert.match(columns[0].textContent, /2,752 票/)
  assert.match(columns[1].textContent, /主持日榜/)
  assert.match(columns[1].textContent, /主持A/)
  assert.match(columns[1].textContent, /6,880 票/)
  assert.match(app.eventsText(), /北京时间今日 00:00/)
  assert.ok(calls.includes('/api/daily_rank/a'))
})

test('late daily response from a previous room cannot overwrite the selected room', async t => {
  const pending = {}
  const app = createApp(t, {
    fetchImpl: url => {
      if (!String(url).startsWith('/api/daily_rank/')) {
        return Promise.resolve(jsonResponse({success: true, map: {}, active: []}))
      }
      const roomId = decodeURIComponent(String(url).split('/').pop())
      return new Promise(resolve => {
        pending[roomId] = display => resolve(jsonResponse({
          success: true, room_id: roomId,
          visitors: [{rank: 1, sender_key: roomId, display, tickets: 1}],
          hosts: []
        }))
      })
    }
  })

  assert.equal(app.run(`typeof renderDailyRanks`), 'function')
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentRooms.b = {nickname: 'B'}
    currentView = 'daily'
    selectRoom('a')
    selectRoom('b')
  `)
  pending.b('B厅日榜')
  await app.flush()
  assert.match(app.eventsText(), /B厅日榜/)
  pending.a('A厅迟到日榜')
  await app.flush()
  assert.match(app.eventsText(), /B厅日榜/)
  assert.doesNotMatch(app.eventsText(), /A厅迟到日榜/)
})

test('admin host weekly view expands large two-hour visitor detail', async t => {
  const app = createApp(t, {
    fetchImpl: async url => {
      if (url === '/api/admin/weekly_rank/a/periods') {
        return jsonResponse({success: true, periods: []})
      }
      if (url === '/api/admin/weekly_rank/a') {
        return jsonResponse({success: true, room_id: 'a', locked: false,
          period_start: 100, period_end: 200, hosts: [
            {rank: 1, recipient_key: 'host-a', display: '主持A', tickets: 79999,
             large_details: [{visitor_key: 'viewer-a', visitor_display: '游客甲',
               slot_start: 100, slot_end: 7300, tickets: 30000}]}
          ], visitors: []})
      }
      return jsonResponse({success: true, records: []})
    }
  })

  assert.equal(app.run(`typeof renderWeeklyRanks`), 'function')
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentView = 'hostWeekly'
    selectRoom('a')
  `)
  await app.flush()
  assert.match(app.eventsText(), /主持周榜/)
  assert.match(app.eventsText(), /主持A/)
  assert.match(app.eventsText(), /79,999 票/)
  assert.match(app.eventsText(), /游客甲/)
  assert.match(app.eventsText(), /30,000 票/)
  assert.match(app.eventsText(), /大额时段/)
})

test('admin visitor weekly view shows sent tickets without host detail', async t => {
  const app = createApp(t, {
    fetchImpl: async url => {
      if (url === '/api/admin/weekly_rank/a/periods') {
        return jsonResponse({success: true, periods: []})
      }
      if (url === '/api/admin/weekly_rank/a') {
        return jsonResponse({success: true, room_id: 'a', locked: false,
          visitors: [{rank: 1, sender_key: 'viewer-a', display: '游客甲', tickets: 5000}],
          hosts: [{rank: 1, recipient_key: 'host-a', display: '主持不应显示',
                   tickets: 5000, large_details: []}]})
      }
      return jsonResponse({success: true, records: []})
    }
  })

  assert.equal(app.run(`typeof renderWeeklyRanks`), 'function')
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentView = 'visitorWeekly'
    selectRoom('a')
  `)
  await app.flush()
  assert.match(app.eventsText(), /游客周榜/)
  assert.match(app.eventsText(), /游客甲/)
  assert.match(app.eventsText(), /5,000 票/)
  assert.doesNotMatch(app.eventsText(), /主持不应显示|大额时段/)
})

test('admin assignment saves target host and note without rewriting raw gift', async t => {
  const calls = []
  const app = createApp(t, {
    fetchImpl: async (url, options = {}) => {
      calls.push({url: String(url), options})
      if (url === '/api/admin/gift_assignments/a') {
        return jsonResponse({success: true, records: [{
          id: 7, display: '游客甲', content: '万象烟花', ticket_count: 2752,
          original_recipient_key: 'room:a', original_recipient_name: '大头',
          timestamp: 100
        }]})
      }
      if (url === '/api/admin/gift_assignments/7' && options.method === 'PUT') {
        return jsonResponse({success: true, adjustment: {
          interaction_id: 7, recipient_key: 'host-a', recipient_name: '主持A'
        }})
      }
      return jsonResponse({success: true})
    }
  })

  assert.equal(app.run(`typeof renderGiftAssignments`), 'function')
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentView = 'giftAssignments'
    selectRoom('a')
  `)
  await app.flush()
  assert.match(app.eventsText(), /万象烟花/)
  assert.match(app.eventsText(), /大头/)
  app.run(`
    document.getElementById('assignment-key-0').value = 'host-a'
    document.getElementById('assignment-name-0').value = '主持A'
    document.getElementById('assignment-note-0').value = '刷错给大头'
    saveGiftAssignment(0)
  `)
  await app.flush()
  const put = calls.find(call => call.url === '/api/admin/gift_assignments/7')
  assert.ok(put)
  assert.deepEqual(JSON.parse(put.options.body), {
    recipient_key: 'host-a', recipient_name: '主持A',
    note: '刷错给大头'
  })
})

test('authenticated write wrapper sends csrf cookie and preserves response', async t => {
  let captured
  const app = createApp(t, {
    fetchImpl: async (url, options = {}) => {
      captured = {url: String(url), options}
      return jsonResponse({success: true, saved: true})
    }
  })
  app.window.document.cookie = 'kekemi_csrf=csrf-live-token; path=/'

  const response = await app.run(`apiFetch('/api/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
  })`)
  const data = await response.json()

  assert.equal(captured.url, '/api/start')
  assert.equal(captured.options.headers['X-CSRF-Token'], 'csrf-live-token')
  assert.equal(data.saved, true)
})

test('authenticated write wrapper makes permission denial visible', async t => {
  const app = createApp(t, {
    fetchImpl: async () => ({
      status: 403,
      json: async () => ({success: false, error: 'forbidden'})
    })
  })

  await app.run(`apiFetch('/api/admin/users')`)

  assert.match(app.eventsText(), /无权限/)
})

test('stopRoom exposes a completion promise for its asynchronous refresh', async t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')
  app.run(`currentRooms.b = {nickname: 'B厅'}`)

  const completion = app.run(`stopRoom('b')`)

  assert.equal(typeof completion?.then, 'function')
  await completion
})

test('stopAll exposes a completion promise for its asynchronous refresh', async t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')

  const completion = app.run(`stopAll()`)

  assert.equal(typeof completion?.then, 'function')
  await completion
})

// ========== 公屏上翻冻结 + 新消息提示 ==========

test('template ships the frozen feed new message button', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '..', 'templates', 'index.html'), 'utf8'
  )
  assert.match(source, /id="feedPane"/)
  assert.match(source, /id="feedNewMessageBtn"/)
})

test('scrolling up freezes the feed so a new message leaves the snapshot untouched', t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')

  const result = app.run(`
    scrollFeedTo(2400)
    const feed = document.getElementById('feedContainer')
    const before = feed.innerHTML
    appendFeedItem(chatEvent('a', '冻结后消息', 2000), 'a')
    ;({
      unchanged: feed.innerHTML === before,
      scrollTop: feed.scrollTop,
      cached: feedByRoom.a.some(ev => ev.content === '冻结后消息'),
      frozen: feedViewStateByRoom.a.frozen,
      button: newMessageButtonText()
    })
  `)

  assert.equal(result.unchanged, true)
  assert.equal(result.scrollTop, 2400)
  assert.equal(result.cached, true)
  assert.equal(result.frozen, true)
  assert.equal(result.button, '↓ 1 条新消息')
  assert.doesNotMatch(app.feedText(), /冻结后消息/)
})

test('a frozen feed counts every new event once and ignores duplicates', t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')

  const result = app.run(`
    scrollFeedTo(2400)
    appendFeedItem(chatEvent('a', '新消息1', 2001), 'a')
    appendFeedItem(chatEvent('a', '新消息2', 2002), 'a')
    appendFeedItem(chatEvent('a', '新消息1', 2001), 'a')
    appendFeedItem(chatEvent('a', '新消息3', 2003), 'a')
    ;({
      unseen: feedViewStateByRoom.a.unseenCount,
      button: newMessageButtonText()
    })
  `)

  assert.equal(result.unseen, 3)
  assert.equal(result.button, '↓ 3 条新消息')
})

test('a frozen feed shows 999+ once the unseen count passes 999', t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')

  const result = app.run(`
    scrollFeedTo(2400)
    for (let i = 0; i < 1000; i++) {
      appendFeedItem(chatEvent('a', '批量消息' + i, 3000 + i), 'a')
    }
    ;({unseen: feedViewStateByRoom.a.unseenCount, button: newMessageButtonText()})
  `)

  assert.equal(result.unseen, 1000)
  assert.equal(result.button, '↓ 999+ 条新消息')
})

test('clicking the new message button shows the latest feed at the bottom and clears the count', t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')

  const result = app.run(`
    scrollFeedTo(2400)
    appendFeedItem(chatEvent('a', '冻结期消息1', 2001), 'a')
    appendFeedItem(chatEvent('a', '冻结期消息2', 2002), 'a')
    document.getElementById('feedNewMessageBtn').click()
    const feed = document.getElementById('feedContainer')
    ;({
      atBottom: feed.scrollTop === feed.scrollHeight,
      items: feed.children.length,
      unseen: feedViewStateByRoom.a.unseenCount,
      frozen: feedViewStateByRoom.a.frozen,
      button: newMessageButtonText()
    })
  `)

  assert.equal(result.atBottom, true)
  assert.equal(result.items, app.run('FEED_MAX_ITEMS'))
  assert.equal(result.unseen, 0)
  assert.equal(result.frozen, false)
  assert.equal(result.button, '')
  assert.match(app.feedText(), /冻结期消息2/)
})

test('scrolling back to the bottom of a frozen feed restores following', t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')

  const result = app.run(`
    scrollFeedTo(2400)
    appendFeedItem(chatEvent('a', '回到底部前的消息', 2001), 'a')
    const feed = document.getElementById('feedContainer')
    scrollFeedTo(feed.scrollHeight)
    ;({
      atBottom: feed.scrollTop === feed.scrollHeight,
      unseen: feedViewStateByRoom.a.unseenCount,
      frozen: feedViewStateByRoom.a.frozen,
      button: newMessageButtonText()
    })
  `)

  assert.equal(result.frozen, false)
  assert.equal(result.unseen, 0)
  assert.equal(result.atBottom, true)
  assert.equal(result.button, '')
  assert.match(app.feedText(), /回到底部前的消息/)
})

test('a feed resting at the bottom keeps following and trims the DOM to the window size', t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')

  const result = app.run(`
    appendFeedItem(chatEvent('a', '跟随消息', 3000), 'a')
    const feed = document.getElementById('feedContainer')
    ;({
      items: feed.children.length,
      atBottom: feed.scrollTop === feed.scrollHeight,
      frozen: feedViewStateByRoom.a.frozen,
      button: newMessageButtonText()
    })
  `)

  assert.equal(result.items, app.run('FEED_MAX_ITEMS'))
  assert.equal(result.atBottom, true)
  assert.equal(result.frozen, false)
  assert.equal(result.button, '')
  assert.match(app.feedText(), /跟随消息/)
  assert.doesNotMatch(app.feedText(), /旧消息0(?!\d)/)
})

test('an SSE init replay while frozen only refills the cache', t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')

  const result = app.run(`
    scrollFeedTo(2400)
    const feed = document.getElementById('feedContainer')
    const before = feed.innerHTML
    handleEvent({type: 'init', data: {feed: feedByRoom.a.concat([
      {type: 'chat', room_id: 'a', display: '重连游客', content: '重连消息1', timestamp: 4001},
      {type: 'chat', room_id: 'a', display: '重连游客', content: '重连消息2', timestamp: 4002}
    ])}}, 'a')
    ;({
      unchanged: feed.innerHTML === before,
      scrollTop: feed.scrollTop,
      unseen: feedViewStateByRoom.a.unseenCount,
      cached: feedByRoom.a.some(ev => ev.content === '重连消息2'),
      button: newMessageButtonText()
    })
  `)

  assert.equal(result.unchanged, true)
  assert.equal(result.scrollTop, 2400)
  assert.equal(result.unseen, 2)
  assert.equal(result.cached, true)
  assert.equal(result.button, '↓ 2 条新消息')
  assert.doesNotMatch(app.feedText(), /重连消息/)
})

test('an /api/feed refresh while frozen only refills the cache', async t => {
  let extra = []
  const app = createFeedApp(t, {
    fetchImpl: async url => {
      if (roomIdFromFeedUrl(url) === 'a') {
        return jsonResponse({success: true, events: extra})
      }
      return jsonResponse({success: true, map: {}, count: 0, active: [], data: []})
    }
  })
  openFeedRoom(app, 'a')

  const before = app.run(`
    scrollFeedTo(2400)
    extraReady = [
      {type: 'chat', room_id: 'a', display: '接口游客', content: '接口消息1', timestamp: 5001},
      {type: 'chat', room_id: 'a', display: '接口游客', content: '接口消息2', timestamp: 5002}
    ]
    document.getElementById('feedContainer').innerHTML
  `)
  extra = app.run(`extraReady`)
  app.run(`loadFeed()`)
  await app.flush()

  const result = app.run(`
    const feed = document.getElementById('feedContainer')
    ;({
      unchanged: feed.innerHTML === ${JSON.stringify(before)},
      scrollTop: feed.scrollTop,
      unseen: feedViewStateByRoom.a.unseenCount,
      cached: feedByRoom.a.some(ev => ev.content === '接口消息2'),
      button: newMessageButtonText()
    })
  `)

  assert.equal(result.unchanged, true)
  assert.equal(result.scrollTop, 2400)
  assert.equal(result.unseen, 2)
  assert.equal(result.cached, true)
  assert.equal(result.button, '↓ 2 条新消息')
  assert.doesNotMatch(app.feedText(), /接口消息/)
})

test('frozen state and unseen count stay isolated between two rooms', async t => {
  const app = createFeedApp(t, {
    fetchImpl: async url => {
      if (roomIdFromFeedUrl(url)) return jsonResponse({success: true, events: []})
      return jsonResponse({success: true, map: {}, count: 0, active: [], data: []})
    }
  })
  openFeedRoom(app, 'a')
  app.run(`
    currentRooms.b = {nickname: 'B厅'}
    seedFeed('b', FEED_MAX_ITEMS, 'B旧消息', 1000)
    scrollFeedTo(2400)
    appendFeedItem(chatEvent('a', 'A厅冻结消息', 2001), 'a')
    appendFeedItem(chatEvent('b', 'B厅后台消息', 2002), 'b')
  `)

  const afterBackground = app.run(`({
    aUnseen: feedViewStateByRoom.a.unseenCount,
    aFrozen: feedViewStateByRoom.a.frozen,
    bUnseen: (feedViewStateByRoom.b || {}).unseenCount || 0,
    bFrozen: (feedViewStateByRoom.b || {}).frozen || false,
    button: newMessageButtonText()
  })`)
  assert.equal(afterBackground.aUnseen, 1)
  assert.equal(afterBackground.aFrozen, true)
  assert.equal(afterBackground.bUnseen, 0)
  assert.equal(afterBackground.bFrozen, false)
  assert.equal(afterBackground.button, '↓ 1 条新消息')

  app.run(`selectRoom('b')`)
  await app.flush()
  const inRoomB = app.run(`({
    atBottom: feedScrollTop() === feedScrollHeight(),
    frozen: feedViewStateByRoom.b.frozen,
    unseen: feedViewStateByRoom.b.unseenCount,
    button: newMessageButtonText()
  })`)
  assert.equal(inRoomB.atBottom, true)
  assert.equal(inRoomB.frozen, false)
  assert.equal(inRoomB.unseen, 0)
  assert.equal(inRoomB.button, '')
  assert.match(app.feedText(), /B厅后台消息/)
  assert.doesNotMatch(app.feedText(), /A厅冻结消息/)

  app.run(`selectRoom('a')`)
  await app.flush()
  const backInRoomA = app.run(`({
    atBottom: feedScrollTop() === feedScrollHeight(),
    frozen: feedViewStateByRoom.a.frozen,
    unseen: feedViewStateByRoom.a.unseenCount,
    button: newMessageButtonText()
  })`)
  assert.equal(backInRoomA.atBottom, true)
  assert.equal(backInRoomA.frozen, false)
  assert.equal(backInRoomA.unseen, 0)
  assert.equal(backInRoomA.button, '')
  assert.match(app.feedText(), /A厅冻结消息/)
})

test('stopping a background room keeps the selected room frozen reading intact', async t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')

  const completion = app.run(`
    currentRooms.b = {nickname: 'B厅'}
    feedViewStateByRoom.b = {frozen: true, unseenCount: 4}
    scrollFeedTo(2400)
    appendFeedItem(chatEvent('a', 'A厅冻结消息', 2001), 'a')
    stopRoom('b')
  `)
  await completion

  const result = app.run(`({
      bCleared: feedViewStateByRoom.b === undefined,
      aFrozen: feedViewStateByRoom.a.frozen,
      aUnseen: feedViewStateByRoom.a.unseenCount,
      scrollTop: document.getElementById('feedContainer').scrollTop,
      button: newMessageButtonText()
    })`)

  assert.equal(result.bCleared, true)
  assert.equal(result.aFrozen, true)
  assert.equal(result.aUnseen, 1)
  assert.equal(result.scrollTop, 2400)
  assert.equal(result.button, '↓ 1 条新消息')
})

test('stopping the selected room never carries its unseen count to the remaining room', async t => {
  const app = createFeedApp(t, {
    fetchImpl: async url => {
      if (roomIdFromFeedUrl(url)) return jsonResponse({success: true, events: []})
      return jsonResponse({success: true, map: {}, count: 0, active: [], data: []})
    }
  })
  openFeedRoom(app, 'a')

  const completion = app.run(`
    currentRooms.b = {nickname: 'B厅'}
    seedFeed('b', 3, 'B旧消息', 1000)
    scrollFeedTo(2400)
    appendFeedItem(chatEvent('a', 'A厅冻结消息', 2001), 'a')
    stopRoom('a')
  `)
  await completion

  const result = app.run(`({
    aCleared: feedViewStateByRoom.a === undefined,
    selected: selectedRoomId,
    bUnseen: (feedViewStateByRoom.b || {}).unseenCount || 0,
    bFrozen: (feedViewStateByRoom.b || {}).frozen || false,
    button: newMessageButtonText()
  })`)

  assert.equal(result.aCleared, true)
  assert.equal(result.selected, 'b')
  assert.equal(result.bUnseen, 0)
  assert.equal(result.bFrozen, false)
  assert.equal(result.button, '')
  assert.match(app.feedText(), /B旧消息2/)
  assert.doesNotMatch(app.feedText(), /A厅冻结消息/)
})

// ========== 渲染开销优化 ==========

test('the feed window keeps at most 300 items', t => {
  const app = createApp(t)
  assert.equal(app.run(`FEED_MAX_ITEMS`), 300)
})

test('escapeHtml escapes quotes so it is safe inside an attribute', t => {
  const app = createApp(t)
  assert.equal(
    app.run(`escapeHtml('<b>a&b"c\\'d</b>')`),
    '&lt;b&gt;a&amp;b&quot;c&#39;d&lt;/b&gt;'
  )
})

test('the live page title is written at most once per second', t => {
  const app = createApp(t)
  const result = app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'feed'
    handleEvent({type: 'mystery_chat', data: {
      room_id: 'a', sec_uid: 'u1', display: '甲', content: '一', timestamp: 1
    }}, 'a')
    const first = document.title
    handleEvent({type: 'mystery_chat', data: {
      room_id: 'a', sec_uid: 'u2', display: '乙', content: '二', timestamp: 2
    }}, 'a')
    ;({first: first, second: document.title})
  `)

  assert.match(result.first, /甲/)
  assert.equal(result.second, result.first)
})

test('only the incrementally appended feed item gets the fade-in class', t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')

  assert.equal(app.run(`document.querySelectorAll('.feed-item.feed-new').length`), 0)

  const result = app.run(`
    appendFeedItem(chatEvent('a', '增量消息', 9000), 'a')
    const items = document.querySelectorAll('.feed-item')
    ;({
      newCount: document.querySelectorAll('.feed-item.feed-new').length,
      lastIsNew: items[items.length - 1].classList.contains('feed-new')
    })
  `)

  assert.equal(result.newCount, 1)
  assert.equal(result.lastIsNew, true)
})

test('the fade-in animation is declared only for newly appended feed items', () => {
  const css = fs.readFileSync(
    path.join(__dirname, '..', 'static', 'style.css'), 'utf8'
  )
  const baseRule = css.match(/\.feed-item\s*\{[^}]*\}/)
  assert.ok(baseRule)
  assert.doesNotMatch(baseRule[0], /animation/)
  assert.match(css, /\.feed-item\.feed-new\s*\{[^}]*animation:\s*feedFadeIn/)
})

test('an out-of-order event is still sorted into the cache and the rendered feed', t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a', 5)

  const timestamps = app.run(`
    appendFeedItem(chatEvent('a', '乱序消息', 1002.5), 'a')
    feedByRoom.a.map(ev => ev.timestamp).join(',')
  `)

  assert.equal(timestamps, '1000,1001,1002,1002.5,1003,1004')
  const text = app.feedText()
  assert.ok(text.indexOf('旧消息2') < text.indexOf('乱序消息'))
  assert.ok(text.indexOf('乱序消息') < text.indexOf('旧消息3'))
})

test('dense mystery events collapse into a single list render', async t => {
  const app = createApp(t)

  const immediate = app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'mystery'
    globalThis.renderCount = 0
    const origMysteryRender = renderMysteries
    renderMysteries = function() { globalThis.renderCount++; origMysteryRender() }
    for (let i = 0; i < 5; i++) {
      handleEvent({type: 'mystery_chat', data: {
        room_id: 'a', sec_uid: 'u' + i, display: '甲' + i, content: 'x', timestamp: i
      }}, 'a')
    }
    globalThis.renderCount
  `)

  assert.equal(immediate, 0)
  await new Promise(resolve => setTimeout(resolve, 350))
  assert.equal(app.run(`globalThis.renderCount`), 1)
})

test('stopping every room clears all frozen feed state', async t => {
  const app = createFeedApp(t)
  openFeedRoom(app, 'a')

  const completion = app.run(`
    currentRooms.b = {nickname: 'B厅'}
    feedViewStateByRoom.b = {frozen: true, unseenCount: 4}
    scrollFeedTo(2400)
    appendFeedItem(chatEvent('a', 'A厅冻结消息', 2001), 'a')
    stopAll()
  `)
  await completion

  const result = app.run(`({
      states: Object.keys(feedViewStateByRoom).length,
      button: newMessageButtonText()
    })`)

  assert.equal(result.states, 0)
  assert.equal(result.button, '')
})

// ========== 个人视图：自己页面上收起某个厅 ==========
// 收起只改本地显示，绝不能碰后台监听——那 5 个厅是所有人共用的，
// 一旦真停掉，所有人当场断流且期间记录永久缺失。

function roomsApp(t, roomIds) {
  const app = createApp(t)
  app.run(`
    ${roomIds.map(r => `currentRooms['${r}'] = {nickname: '${r}厅'}`).join('\n    ')}
    selectedRoomId = '${roomIds[0]}'
    currentView = 'feed'
    localStorage.clear()
  `)
  return app
}

function visibleRoomIds(app) {
  return app.run(`
    Array.from(document.querySelectorAll('#roomSelector .room-filter-btn'))
      .map(el => el.dataset.rid)
  `)
}

function dropdownRoomIds(app) {
  return app.run(`
    Array.from(document.querySelectorAll('#historyDropdown .item'))
      .map(el => el.dataset.rid || '')
  `)
}

test('hiding a room removes it from the room bar but keeps the listener running', t => {
  const app = roomsApp(t, ['a', 'b', 'c'])
  app.run(`updateRoomSelector()`)
  assert.deepEqual(Array.from(visibleRoomIds(app)), ['a', 'b', 'c'])

  app.run(`hideRoom('b')`)

  assert.deepEqual(Array.from(visibleRoomIds(app)), ['a', 'c'])
  // 后台监听不受影响
  assert.deepEqual(Array.from(app.run(`Object.keys(currentRooms)`)), ['a', 'b', 'c'])
})

test('a hidden room shows up in the dropdown as addable', t => {
  const app = roomsApp(t, ['a', 'b', 'c'])
  app.run(`updateRoomSelector(); hideRoom('b'); renderHistoryFromCache()`)

  assert.deepEqual(Array.from(dropdownRoomIds(app)), ['b'])
})

test('clicking a dropdown entry brings the room back', t => {
  const app = roomsApp(t, ['a', 'b', 'c'])
  app.run(`
    updateRoomSelector()
    hideRoom('b')
    renderHistoryFromCache()
    document.querySelector('#historyDropdown .item').click()
  `)

  assert.deepEqual(Array.from(visibleRoomIds(app)), ['a', 'b', 'c'])
  assert.deepEqual(Array.from(dropdownRoomIds(app)), [])
})

test('hidden rooms survive a page reload', t => {
  const app = roomsApp(t, ['a', 'b', 'c'])
  app.run(`hideRoom('b')`)
  const stored = app.run(`localStorage.getItem('kekemi.hiddenRooms')`)

  assert.match(String(stored), /b/)
  // 模拟重新加载：重新读取存储后仍应隐藏
  assert.equal(app.run(`hiddenRoomIds().has('b')`), true)
})

test('hiding the selected room switches to another visible one', t => {
  const app = roomsApp(t, ['a', 'b', 'c'])
  app.run(`selectedRoomId = 'b'; updateRoomSelector(); hideRoom('b')`)

  const selected = app.run(`selectedRoomId`)
  assert.notEqual(selected, 'b')
  assert.ok(['a', 'c'].includes(selected))
})

test('all rooms visible means an empty dropdown', t => {
  const app = roomsApp(t, ['a', 'b'])
  app.run(`updateRoomSelector(); renderHistoryFromCache()`)

  assert.deepEqual(Array.from(dropdownRoomIds(app)), [])
})

// ========== 日榜静默刷新 ==========
// 2 秒轮询只读本机数据，但原实现每次都先把整页换成「正在加载」占位，
// 再等接口回来重画——用户看到的就是内容→转圈→内容的循环闪烁。

function dailyPayload(tickets) {
  return {
    success: true, room_id: 'a', scope: 'beijing_today',
    note: '仅统计今天监听期间',
    visitors: [{rank: 1, sender_key: 'v1', display: '游客A', tickets: tickets}],
    hosts: [{rank: 1, recipient_key: 'h1', display: '主持A', tickets: tickets * 2}]
  }
}

function createDailyApp(t, state) {
  return createApp(t, {
    fetchImpl: async url => {
      if (String(url).startsWith('/api/daily_rank/')) {
        if (state.fail) throw new Error('网络抖动')
        return jsonResponse(state.payload)
      }
      return jsonResponse({success: true, map: {}, count: 0, active: [], data: []})
    }
  })
}

test('a silent daily refresh never flashes the loading placeholder', async t => {
  const state = {payload: dailyPayload(100), fail: false}
  const app = createDailyApp(t, state)
  app.run(SENTINEL)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'daily'
    renderDailyRanks()
  `)
  await app.flush()
  assert.match(app.eventsText(), /游客A/)

  app.run(`plantSentinel(); renderDailyRanks()`)
  // 关键断言：发起刷新的瞬间页面仍是内容，不是「正在加载」
  assert.doesNotMatch(app.eventsText(), /正在加载/)
  await app.flush()

  assert.equal(app.run(`sentinelSurvived()`), true)
  assert.match(app.eventsText(), /游客A/)
})

test('changed daily data repaints and keeps the reading position', async t => {
  const state = {payload: dailyPayload(100), fail: false}
  const app = createDailyApp(t, state)
  app.run(SENTINEL)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'daily'
    const events = document.getElementById('events')
    Object.defineProperty(events, 'clientHeight', {value: 200, configurable: true})
    Object.defineProperty(events, 'scrollHeight', {configurable: true, get() { return 800 }})
    renderDailyRanks()
  `)
  await app.flush()

  app.run(`document.getElementById('events').scrollTop = 300; plantSentinel()`)
  state.payload = dailyPayload(200)
  app.run(`renderDailyRanks()`)
  await app.flush()

  assert.equal(app.run(`sentinelSurvived()`), false)
  assert.match(app.eventsText(), /400 票|400/)
  assert.equal(app.run(`document.getElementById('events').scrollTop`), 300)
})

test('a transient daily fetch failure keeps the current page', async t => {
  const state = {payload: dailyPayload(100), fail: false}
  const app = createDailyApp(t, state)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'daily'
    renderDailyRanks()
  `)
  await app.flush()

  state.fail = true
  app.run(`renderDailyRanks()`)
  await app.flush()

  assert.match(app.eventsText(), /游客A/)
  assert.doesNotMatch(app.eventsText(), /⚠️|网络抖动/)
})

test('switching rooms on the daily view still shows the loading placeholder', async t => {
  const state = {payload: dailyPayload(100), fail: false}
  const app = createDailyApp(t, state)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentRooms.b = {nickname: 'B'}
    selectedRoomId = 'a'
    currentView = 'daily'
    renderDailyRanks()
  `)
  await app.flush()

  const midSwitch = app.run(`
    selectedRoomId = 'b'
    renderDailyRanks()
    document.getElementById('events').textContent
  `)
  assert.match(midSwitch, /正在加载/)
})

test('a roster sync-time tick updates the clock in place without rebuilding', async t => {
  let payload = onlineRoster(8, '观众')
  const app = createRosterApp(t, () => payload)
  app.run(SENTINEL)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'mystery'
    renderOnlineAudience(true)
  `)
  await app.flush()

  app.run(`plantSentinel()`)
  payload = Object.assign(onlineRoster(8, '观众'), {updated_at: 200})
  app.run(`renderOnlineAudience(true)`)
  await app.flush()

  // 名单没变：不重建 DOM，但时间戳要原地跳到新值
  assert.equal(app.run(`sentinelSurvived()`), true)
  assert.match(app.eventsText(), /08:03:20/)
  assert.doesNotMatch(app.eventsText(), /08:01:40/)
})

// ========== 消费等级徽章 ==========
// 口径：显示「最近观察到的等级」；等级 0 或未知一律不显示，绝不出现 Lv.0。

test('levelBadgeHtml hides zero and unknown levels', t => {
  const app = createApp(t)
  assert.equal(app.run(`levelBadgeHtml(0)`), '')
  assert.equal(app.run(`levelBadgeHtml(undefined)`), '')
  assert.equal(app.run(`levelBadgeHtml(-3)`), '')
  assert.match(app.run(`levelBadgeHtml(7)`), /Lv\.7/)
  assert.match(app.run(`levelBadgeHtml(50)`), /lv-badge/)
})

test('feed items show the sender level badge when known', t => {
  const app = createApp(t)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentView = 'feed'
    selectRoom('a')
    handleEvent({type: 'mystery_chat', data: {
      room_id: 'a', sec_uid: 'u1', display: '壕客', content: '来了',
      consume_level: 41, timestamp: 100
    }}, 'a')
    handleEvent({type: 'mystery_chat', data: {
      room_id: 'a', sec_uid: 'u2', display: '素人', content: '路过', timestamp: 101
    }}, 'a')
  `)

  const html = app.run(`document.getElementById('feedContainer').innerHTML`)
  assert.match(html, /Lv\.41/)
  assert.doesNotMatch(html, /Lv\.0/)
  const text = app.feedText()
  assert.ok(text.indexOf('壕客') > -1 && text.indexOf('素人') > -1)
})

test('online roster cards show the level badge for both pages', async t => {
  const payload = onlineRoster(2, '观众')
  payload.records[0].consume_level = 33
  payload.records[1].consume_level = 0
  const app = createRosterApp(t, () => payload)
  app.run(`
    currentRooms.a = {nickname: 'A'}
    selectedRoomId = 'a'
    currentView = 'mystery'
    renderOnlineAudience(true)
  `)
  await app.flush()

  const html = app.run(`document.getElementById('events').innerHTML`)
  assert.match(html, /Lv\.33/)
  assert.doesNotMatch(html, /Lv\.0/)
})

test('weekly visitor rows show the level badge, daily rows stay untouched', async t => {
  const app = createApp(t, {
    fetchImpl: async url => {
      if (url === '/api/admin/weekly_rank/a/periods') {
        return jsonResponse({success: true, periods: []})
      }
      if (url === '/api/admin/weekly_rank/a') {
        return jsonResponse({success: true, room_id: 'a', locked: false,
          visitors: [
            {rank: 1, sender_key: 'v1', display: '壕客甲', tickets: 9000, consume_level: 48},
            {rank: 2, sender_key: 'display:马甲', display: '马甲', tickets: 100, consume_level: 0}
          ],
          hosts: []})
      }
      if (url === '/api/daily_rank/a') {
        return jsonResponse({success: true, room_id: 'a',
          visitors: [{rank: 1, sender_key: 'v1', display: '壕客甲', tickets: 500}],
          hosts: []})
      }
      return jsonResponse({success: true, records: [], map: {}, count: 0, active: [], data: []})
    }
  })

  app.run(`
    currentRooms.a = {nickname: 'A'}
    currentView = 'visitorWeekly'
    selectRoom('a')
  `)
  await app.flush()
  const weeklyHtml = app.run(`document.getElementById('events').innerHTML`)
  assert.match(weeklyHtml, /Lv\.48/)
  assert.doesNotMatch(weeklyHtml, /Lv\.0/)

  app.run(`switchMode('daily')`)
  await app.flush()
  const dailyHtml = app.run(`document.getElementById('events').innerHTML`)
  assert.doesNotMatch(dailyHtml, /Lv\./)
})
