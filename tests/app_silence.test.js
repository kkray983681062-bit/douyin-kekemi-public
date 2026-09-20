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
  const dom = new JSDOM(
    `<!doctype html><html><body>
      <div id="events"></div><div id="roomSelector"></div>
      <div id="silencePanel"></div><div id="toastLayer"></div><div id="dot"></div>
      <div id="statusText"></div><div id="statsText"></div>
      <div id="dataPanelTitle"></div>
    </body></html>`,
    {url: 'http://127.0.0.1:5000/', runScripts: 'outside-only'}
  )
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
  return {
    window,
    run(code) { return new vm.Script(code).runInContext(context) },
    async flush() {
      await new Promise(r => setImmediate(r))
      await new Promise(r => setImmediate(r))
    },
  }
}

test('铃铛只对 admin 显示，且区分订阅状态', t => {
  const app = createApp(t)
  assert.equal(app.run(`silenceBellHtml('demo_hall_a', true, false)`), '',
    '非 admin 不显示铃铛')
  const on = app.run(`silenceBellHtml('demo_hall_a', true, true)`)
  const off = app.run(`silenceBellHtml('demo_hall_a', false, true)`)
  assert.ok(on.includes('silence-bell-on'), '已订阅要有开启样式')
  assert.ok(!off.includes('silence-bell-on'), '未订阅不能是开启样式')
})

test('临时厅（没有抖音号）不显示铃铛', t => {
  const app = createApp(t)
  assert.equal(app.run(`silenceBellHtml('', false, true)`), '')
})

test('同一条告警只弹一次系统通知', t => {
  const app = createApp(t)
  const shown = []
  app.window.Notification = function (title, opts) { shown.push(title) }
  app.window.Notification.permission = 'granted'
  app.run(`notifySilenceAlerts([{id:7, nickname:'A', hall_name:'H',
    alert_index:1, resolved:false}])`)
  app.run(`notifySilenceAlerts([{id:7, nickname:'A', hall_name:'H',
    alert_index:1, resolved:false}])`)
  assert.equal(shown.length, 1, '弹过的不能再弹')
})

test('弹过的通知 id 要写进 localStorage，不能只留在内存里', t => {
  const app = createApp(t)
  app.window.Notification = function () {}
  app.window.Notification.permission = 'granted'
  app.run(`notifySilenceAlerts([{id:7, nickname:'A', hall_name:'H',
    alert_index:1, resolved:false}])`)
  const stored = app.run(`localStorage.getItem('kekemi.silenceNotifiedIds')`)
  assert.match(String(stored), /7/, '弹过的 id 应该被持久化')
})

test('刷新页面后（localStorage 里已经有上次弹过的 id），不能重复弹通知', t => {
  // 不复用同一个内存里的 Set：这里直接在“执行 app 脚本之前”往
  // localStorage 里预写一条，模拟“上次页面加载时已经弹过、现在刷新了”，
  // 而不是靠同一个 JS 进程里还留着的变量——那样测不出真正的持久化。
  const app = createApp(t)
  app.run(`localStorage.setItem('kekemi.silenceNotifiedIds', JSON.stringify([7]))`)
  const shown = []
  app.window.Notification = function (title) { shown.push(title) }
  app.window.Notification.permission = 'granted'
  app.run(`notifySilenceAlerts([{id:7, nickname:'A', hall_name:'H',
    alert_index:1, resolved:false}])`)
  assert.equal(shown.length, 0, '刷新前弹过的告警，刷新后不能再弹一次')
})

test('通知去重集合没有任何变化时不重复写 localStorage（每 5 秒轮询一次，白写没意义）', t => {
  const app = createApp(t)
  app.run(`
    globalThis.__writes = 0
    const _origSetItem = Storage.prototype.setItem
    Storage.prototype.setItem = function (...args) {
      globalThis.__writes++
      return _origSetItem.apply(this, args)
    }
  `)
  app.window.Notification = function () {}
  app.window.Notification.permission = 'granted'
  const one = `[{id:7, nickname:'A', hall_name:'H', alert_index:1, resolved:false}]`
  app.run(`notifySilenceAlerts(${one})`)              // 新告警：必须写一次
  const afterFirst = app.run('globalThis.__writes')
  app.run(`notifySilenceAlerts(${one})`)              // 同一条，响应内容完全没变
  const afterSecond = app.run('globalThis.__writes')
  assert.equal(afterFirst, 1, '第一次有新增要写一次')
  assert.equal(afterSecond, afterFirst,
    '集合内容（剪掉的/新增的）都没变化，第二次不该再写 localStorage')
})

test('已经不在最新响应里的 id 要保留，不能剪掉——剪了会重复弹', t => {
  // 这条原来断言的是反过来的：「不在响应里就剪掉」，前提是「离开了就
  // 不会回来」。这个前提不成立：接口有 LIMIT 50，未结告警攒多了老 id
  // 会被挤出去，等某人恢复发言、他名下的告警被一次性 resolve，位置空
  // 出来，老 id 又回到响应里，于是重复弹一遍系统通知。集合大小改由
  // SILENCE_NOTIFIED_CAP 封顶控制，不靠这种裁剪。
  const app = createApp(t)
  app.run(`localStorage.setItem('kekemi.silenceNotifiedIds', JSON.stringify([99, 7]))`)
  app.window.Notification = function () {}
  app.window.Notification.permission = 'granted'
  app.run(`notifySilenceAlerts([{id:7, nickname:'A', hall_name:'H', alert_index:1}])`)
  const stored = JSON.parse(app.run(
    `localStorage.getItem('kekemi.silenceNotifiedIds')`))
  assert.ok(stored.includes(99), '暂时不在响应里的 id 要留着，它可能会回来')
  assert.ok(stored.includes(7))
})

test('通知去重集合有上限，不能无限增长', t => {
  const app = createApp(t)
  const seeded = Array.from({length: 200}, (_, i) => i + 1)   // 1..200，已弹过
  app.run(`localStorage.setItem('kekemi.silenceNotifiedIds',
    ${JSON.stringify(JSON.stringify(seeded))})`)
  app.window.Notification = function () {}
  app.window.Notification.permission = 'granted'
  // 这次响应里 1..200 依然都在（还没叉掉），外加一条全新的 201
  const alerts = seeded.map(id => ({id, nickname:'A', hall_name:'H',
    alert_index:1, resolved:false}))
  alerts.push({id:201, nickname:'A', hall_name:'H', alert_index:1, resolved:false})
  app.run(`notifySilenceAlerts(${JSON.stringify(alerts)})`)
  const stored = JSON.parse(app.run(
    `localStorage.getItem('kekemi.silenceNotifiedIds')`))
  assert.ok(stored.length <= 200, `不能超过上限，实际 ${stored.length}`)
  assert.ok(stored.includes(201), '新弹的这条不能被自己挤掉')
  assert.ok(!stored.includes(1), '超过上限时应该先丢最老的')
})

test('getItem 抛异常（比如隐私模式拒绝访问）时也要在内存里去重，不能每次都当成没弹过', t => {
  const app = createApp(t)
  app.run(`Storage.prototype.getItem = function () { throw new Error('boom') }`)
  const shown = []
  app.window.Notification = function (title) { shown.push(title) }
  app.window.Notification.permission = 'granted'
  assert.doesNotThrow(() => {
    app.run(`notifySilenceAlerts([{id:7, nickname:'A', hall_name:'H',
      alert_index:1, resolved:false}])`)
  })
  // 关键在第二次调用：只测一次调用查不出去重坏没坏，第一次谁都是"没弹过"。
  app.run(`notifySilenceAlerts([{id:7, nickname:'A', hall_name:'H',
    alert_index:1, resolved:false}])`)
  assert.equal(shown.length, 1,
    'getItem 一直抛异常时，第二次不能把同一条 alert 又弹一遍')
})

test('setItem 抛异常（比如配额满）时也要在内存里去重，不能每次都当成没弹过', t => {
  const app = createApp(t)
  // 跟上面那条分开测：getItem 能读、只有 setItem 写不进去，是另一种故障
  // 模式——读到的永远是"从没成功写过"的空值，如果去重只靠读 localStorage
  // 本身，这条路径会跟 getItem 抛异常那条以完全不同的方式失败。
  app.run(`Storage.prototype.setItem = function () { throw new Error('boom') }`)
  const shown = []
  app.window.Notification = function (title) { shown.push(title) }
  app.window.Notification.permission = 'granted'
  assert.doesNotThrow(() => {
    app.run(`notifySilenceAlerts([{id:7, nickname:'A', hall_name:'H',
      alert_index:1, resolved:false}])`)
  })
  app.run(`notifySilenceAlerts([{id:7, nickname:'A', hall_name:'H',
    alert_index:1, resolved:false}])`)
  assert.equal(shown.length, 1,
    'setItem 一直抛异常时，第二次不能把同一条 alert 又弹一遍')
})


test('看着某厅的静默记录时切到别的厅，要用新厅的 room_id 重新拉记录', t => {
  const app = createApp(t)
  const urls = app.run(`
    currentRooms['room-a'] = {nickname: 'A厅', douyin_id: 'aaa'}
    currentRooms['room-b'] = {nickname: 'B厅', douyin_id: 'bbb'}
    selectedRoomId = 'room-a'
    currentView = 'silence'
    window.__urls = []
    window.fetch = (url) => { window.__urls.push(String(url)); return new Promise(() => {}) }
    selectRoom('room-b')
    window.__urls
  `)
  assert.equal(urls.length, 1, '切厅要重新拉一次静默记录，不能一次都不拉')
  assert.ok(urls[0].includes('room-b'), '必须用切过去的新厅 room_id 去拉')
  assert.ok(!urls[0].includes('room-a'), '不能还挂着切之前那个厅的 room_id')
})

test('数据面板的静默记录视图有专属标题，不会退化成通用的"数据"', t => {
  const app = createApp(t)
  assert.equal(app.run(`DATA_PANEL_VIEW_LABELS.silence`), '🔕 静默记录')
})

test('A 厅记录还没回来时已经切去看 B 厅，A 厅的慢响应回来后不能覆盖 B 厅画面（防串厅证据）', async t => {
  const app = createApp(t)
  const resolvers = {}
  // 每次 fetch 都发一个永远不主动 resolve 的 promise，resolve 函数按 URL
  // 记下来，好在测试里手动按想要的顺序触发——用来还原「先发的请求反而后
  // 回来」这种真实网络时序。
  app.window.fetch = (url) => new Promise(resolve => { resolvers[String(url)] = resolve })

  app.run(`
    currentView = 'silence'
    renderSilenceRecords('room-a')
    renderSilenceRecords('room-b')
  `)

  const urlA = Object.keys(resolvers).find(u => u.includes('room-a'))
  const urlB = Object.keys(resolvers).find(u => u.includes('room-b'))
  assert.ok(urlA && urlB, '两次点击都应该已经各发出一次请求')

  // 反着结：后发的 B 厅先回来（快、空），先发的 A 厅后回来（慢、有记录）。
  resolvers[urlB]({json: async () => ({success: true, records: []})})
  await new Promise(r => setImmediate(r))
  resolvers[urlA]({json: async () => ({success: true, records: [
    {nickname: 'A厅主持', beijing_date: '2027-01-02', alert_count: 3,
     chat_snapshot: {buckets: []}}
  ]})})
  await new Promise(r => setImmediate(r))

  const html = app.window.document.getElementById('events').innerHTML
  assert.ok(!html.includes('A厅主持'), 'A 厅的慢响应不能把已经切到 B 厅的画面覆盖掉')
  assert.ok(html.includes('这个厅还没有静默记录'), '画面应该停在最后一次点击的 B 厅（空）结果上')
})

test('从面板进静默记录，标题要跟着换到那个厅，不能停在原来选中的厅上（防串厅证据）', t => {
  // 流程变了：铃铛只开面板，记录入口在面板里。但要守的东西没变——
  // 这功能全部产出就是「某人在某厅」的证据，厅名挂错等于证据废了。
  const app = createApp(t)
  app.run(`
    currentRooms['room-a'] = {nickname: 'A厅', douyin_id: 'aaa'}
    currentRooms['room-b'] = {nickname: 'B厅', douyin_id: 'bbb'}
    silenceWatches = ['aaa', 'bbb']
    selectedRoomId = 'room-a'
    currentView = 'mystery'
    updateRoomSelector()
    // 面板是同步渲染的，紧接着点。中间不能 await：那会让启动时那串
    // loadSilenceWatches 轮询跑完，把 silenceWatches 刷成假后端的空值，
    // 面板就变成「未订阅」那一版，压根没有记录入口。
    openSilencePanel('bbb')
    document.querySelector('[data-silence-records="bbb"]').click()
  `)
  const title = app.run(`document.getElementById('dataPanelTitle').textContent`)
  assert.ok(title.includes('静默记录'), '进记录视图后标题应该跟着换')
  assert.ok(title.includes('B厅'), '标题要跟点的那个厅走')
  assert.ok(!title.includes('A厅'), '不能还挂着切换前选中的那个厅')
  assert.equal(app.run('selectedRoomId'), 'room-b',
    'selectedRoomId 也要跟着切过去')
})

test('订阅切换请求失败要提示用户，不能吞掉网络错误', async t => {
  const app = createApp(t)
  app.window.fetch = async () => { throw new Error('网络不可用') }
  await app.run(`toggleSilenceWatch('aaa')`)
  // 提示改走浮层：既有的 showToast 是把 #events 整个覆盖掉，而且只在
  // 神秘人列表为空时才显示，在最常见的界面状态下等于没提示。
  const layer = app.window.document.getElementById('toastLayer').textContent
  assert.ok(layer.includes('网络错误'), '订阅失败要有提示，不能悄悄什么都不做')
})
