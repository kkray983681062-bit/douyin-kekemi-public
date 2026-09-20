// 铃铛面板 + 未读数。取代原来「主页顶部一条告警条」的形态。
//
// 实跑一晚否掉了旧形态：一个厅一晚攒 30 多条灰色的「已恢复发言」，
// 把真正要看的 2 条红色的埋了。而且铃铛点一下就直接订阅、毫无提示，
// 探索界面时随手一点就订上了自己都不知道。
//
// 新形态：厅按钮上 `● 名字 🔔③ ×`，点铃铛打开面板，面板里才有
// 「开始/取消监测」，想误订都难。打开即算读完，数字归 0。

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { JSDOM } = require('jsdom')

const APP_SOURCE = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'app.js'), 'utf8'
)

// state 是这个假后端返回的全部内容。必须从这里给，不能只往全局变量里塞：
// 面板和铃铛都跟着 5 秒轮询刷新（这是想要的行为），塞进去的状态一次轮询
// 就被覆盖了——第一版测试就栽在这上面，看着像实现坏了，其实是骨架不对。
function createApp(t, {fetchImpl, state} = {}) {
  const served = Object.assign(
    {alerts: [], unread_by_hall: {}, douyin_ids: [], records: []}, state || {})
  const dom = new JSDOM(`<!doctype html><body>
    <div id="events"></div><div id="roomSelector"></div>
    <div id="silencePanel"></div><div id="toastLayer"></div>
    <div id="dot"></div><div id="statusText"></div><div id="statsText"></div>
    <div id="dataPanelTitle"></div>
    <div id="feedPane"><div id="feedContainer"></div>
    <button id="feedNewMessageBtn"></button></div>
    <button id="modeMystery"></button><button id="modeAll"></button>
    <button id="modeFeed"></button><button id="modeDaily"></button>
    <button id="modeHostWeekly"></button><button id="modeVisitorWeekly"></button>
    <button id="modeGiftLibrary"></button><button id="modeGiftAssignments"></button>
  </body>`, {url: 'http://127.0.0.1:5000/', runScripts: 'outside-only'})
  const { window } = dom
  window.KEKEMI_AUTH = {role: 'admin', enabled: false}
  const calls = []
  window.fetch = fetchImpl || (async (url, opts) => {
    calls.push({url, opts})
    return {json: async () => Object.assign(
      {success: true, count: (served.alerts || []).length}, served)}
  })
  window.EventSource = class { close() {} }
  window.setInterval = () => 0
  window.clearInterval = () => {}
  window.setTimeout = (fn) => { return 0 }   // toast 的自动消失不在测试范围
  window.anime = {animate: () => ({pause() {}}), stagger: () => 0, spring: () => 0}
  if (!window.CSS) window.CSS = {}
  if (!window.CSS.escape) window.CSS.escape = v => String(v)
  const context = dom.getInternalVMContext()
  new vm.Script(APP_SOURCE, {filename: 'static/app.js'}).runInContext(context)
  t.after(() => window.close())
  return {
    window, calls,
    run(code) { return new vm.Script(code).runInContext(context) },
    async flush() {
      await new Promise(r => setImmediate(r))
      await new Promise(r => setImmediate(r))
    },
  }
}

// ---------- 铃铛本身 ----------

test('铃铛不再带独立的取消订阅按钮（⊘），厅按钮才装得下', t => {
  const app = createApp(t)
  const html = app.run(`silenceBellHtml('demo_hall_a', true, true, 0)`)
  assert.ok(!html.includes('⊘'), '退订入口挪进面板了，不该再占一个图标')
  assert.ok(!html.includes('data-unwatch-id'))
})

test('有未读就在铃铛上挂红色数字，没有就不挂', t => {
  const app = createApp(t)
  const withUnread = app.run(`silenceBellHtml('demo_hall_a', true, true, 3)`)
  const none = app.run(`silenceBellHtml('demo_hall_a', true, true, 0)`)
  assert.ok(withUnread.includes('silence-unread'), '有未读要有角标')
  assert.ok(withUnread.includes('>3<'), '角标写实际条数')
  assert.ok(!none.includes('silence-unread'), '0 条不显示角标')
})

test('未读数很大时截断成 99+，别把厅按钮撑爆', t => {
  const app = createApp(t)
  assert.ok(app.run(`silenceBellHtml('X', true, true, 137)`).includes('99+'))
})

test('临时厅（没有抖音号）和非 admin 都不显示铃铛', t => {
  const app = createApp(t)
  assert.equal(app.run(`silenceBellHtml('', true, true, 5)`), '')
  assert.equal(app.run(`silenceBellHtml('demo_hall_a', true, false, 5)`), '')
})

// ---------- 点铃铛 = 打开面板，不是直接订阅 ----------

test('点没订阅的厅的铃铛，只打开面板，不会直接订上', async t => {
  const app = createApp(t)
  app.run(`silenceWatches = []`)
  app.run(`openSilencePanel('demo_hall_a')`)
  await app.flush()
  const puts = app.calls.filter(c => (c.opts || {}).method === 'PUT')
  assert.equal(puts.length, 0, '打开面板不该顺手订阅——误订正是要根治的问题')
  assert.ok(app.window.document.getElementById('silencePanel').textContent
    .includes('开始监测'), '未订阅的面板里给一个明确的开始按钮')
})

test('已订阅的厅，面板里给的是「取消监测」和「查看静默记录」', async t => {
  const app = createApp(t, {state: {douyin_ids: ['demo_hall_a']}})
  app.run(`silenceWatches = ['demo_hall_a']`)
  app.run(`openSilencePanel('demo_hall_a')`)
  await app.flush()
  const text = app.window.document.getElementById('silencePanel').textContent
  assert.ok(text.includes('取消监测'))
  assert.ok(text.includes('静默记录'))
})

test('面板里列出该厅的未读告警', async t => {
  // mock 要返回同一批告警：面板会跟着 5 秒轮询刷新（这是想要的行为），
  // 只塞进全局变量的话，一次轮询回来就被覆盖成空的了。
  const alerts = [{id: 1, douyin_id: 'demo_hall_a', nickname: 'Ry.猫猫',
                   hall_name: '示例·RUYUE', alert_index: 3, created_at: 1},
                  {id: 2, douyin_id: 'BieDe', nickname: '别厅的人',
                   hall_name: '别的厅', alert_index: 1, created_at: 1}]
  const app = createApp(t, {state: {
    alerts, unread_by_hall: {demo_hall_a: 1}, douyin_ids: ['demo_hall_a']}})
  app.run(`
    silenceWatches = ['demo_hall_a']
    silenceAlerts = ${JSON.stringify(alerts)}
  `)
  app.run(`openSilencePanel('demo_hall_a')`)
  await app.flush()
  const text = app.window.document.getElementById('silencePanel').textContent
  assert.ok(text.includes('Ry.猫猫'))
  assert.ok(text.includes('第 3 次'))
  assert.ok(!text.includes('别厅的人'), '只列这个厅的')
})

test('打开面板就把这个厅的未读标为已读', async t => {
  const app = createApp(t)
  app.run(`
    silenceWatches = ['demo_hall_a']
    silenceAlerts = [{id:7, douyin_id:'demo_hall_a', nickname:'A',
                      hall_name:'H', alert_index:1, created_at:1},
                     {id:9, douyin_id:'BieDe', nickname:'B',
                      hall_name:'H2', alert_index:1, created_at:1}]
  `)
  app.run(`openSilencePanel('demo_hall_a')`)
  await app.flush()
  const read = app.calls.find(c => String(c.url).includes('/alerts/read'))
  assert.ok(read, '打开即已读')
  assert.deepEqual(JSON.parse(read.opts.body).alert_ids, [7],
    '只标这个厅的，别把别厅的未读一起清了')
})

test('面板里的昵称和厅名要转义（都来自抖音，攻击者可控）', async t => {
  const evil = [{id: 1, douyin_id: 'X', nickname: '<img src=x onerror=alert(1)>',
                 hall_name: '<b>厅</b>', alert_index: 1, created_at: 1}]
  const app = createApp(t, {state: {alerts: evil, douyin_ids: ['X']}})
  app.run(`silenceWatches = ['X']; silenceAlerts = ${JSON.stringify(evil)}`)
  app.run(`openSilencePanel('X')`)
  await app.flush()
  const panel = app.window.document.getElementById('silencePanel')
  // 先确认内容真的渲染出来了，否则下面两条「不含」断言会空过
  assert.ok(panel.textContent.includes('onerror'), '昵称要真的出现在面板里')
  assert.ok(!panel.innerHTML.includes('<img src=x'))
  assert.ok(!panel.innerHTML.includes('<b>厅</b>'))
})

// ---------- 订阅切换要有反馈 ----------

test('订阅和取消都要弹一句提示，让人知道刚才那一下干了什么', async t => {
  const app = createApp(t)
  app.run(`silenceWatches = []`)
  app.run(`toggleSilenceWatch('demo_hall_a')`)
  await app.flush()
  assert.ok(app.window.document.getElementById('toastLayer').textContent.length > 0,
    '开启要有提示')
})

test('提示用浮层，不能像 showToast 那样把主内容区覆盖掉', async t => {
  const app = createApp(t)
  app.run(`document.getElementById('events').innerHTML = '<div>原有内容</div>'`)
  app.run(`silenceToast('已开始监测 示例·RUYUE')`)
  assert.ok(app.window.document.getElementById('events').textContent.includes('原有内容'),
    '主内容区不能被提示冲掉')
})

// ---------- 主页不再有告警条 ----------

test('主页模板里不再有告警条容器', () => {
  // 原来这条是在测试自己搭的 DOM 上查 #silenceBar，那个 DOM 本来就没有，
  // 断言恒成立、什么也没证明。要查就查真的模板。
  const html = fs.readFileSync(
    path.join(__dirname, '..', 'templates', 'index.html'), 'utf8')
  assert.ok(!html.includes('silenceBar'), '告警条整个拿掉，改由铃铛承载')
  assert.ok(html.includes('id="silencePanel"'), '换成面板容器')
  assert.ok(html.includes('id="toastLayer"'), '和提示浮层')
})

// ---------- 系统通知留着 ----------

test('新告警仍然弹浏览器系统通知，同一条只弹一次', t => {
  const app = createApp(t)
  const shown = []
  app.window.Notification = function (title) { shown.push(title) }
  app.window.Notification.permission = 'granted'
  const a = `[{id:11, douyin_id:'X', nickname:'A', hall_name:'H', alert_index:1, created_at:1}]`
  app.run(`notifySilenceAlerts(${a})`)
  app.run(`notifySilenceAlerts(${a})`)
  assert.equal(shown.length, 1)
})

// ---------- 老记录不能被编出数字来（数据正确红线） ----------

test('改动之前写的证据记录没有 gap 字段，不能渲染成「静默 0 秒」', async t => {
  // 线上已经有 5 条这种老记录，而且这张表是故意不参与 7 天清理的。
  // Number(undefined || 0) === 0，一不留神就会在证据里写下一个从没
  // 测量过的数字——「整轮上麦 静默 0 秒」正好是要证明的事情的反面。
  const old = [{nickname: 'Ry.鱼小小', beijing_date: '2026-08-20', alert_count: 3,
                chat_snapshot: {buckets: [{date: '2026-08-20', start: '22:00',
                  end: '24:00', count: 1,
                  messages: [{at: '22:58:03', text: '来了'}]}]}}]
  const app = createApp(t, {state: {records: old}})
  app.run(`currentView = 'silence'; renderSilenceRecords('room-1')`)
  await app.flush()
  const text = app.window.document.getElementById('events').textContent
  assert.ok(text.includes('来了'), '老记录的内容仍要显示')
  assert.ok(!text.includes('0 秒'), '没测量过的间隔不能编一个 0 出来')
})

test('老记录里一条发言都没有时，也不能声称整轮静默 0 秒', async t => {
  const old = [{nickname: 'A', beijing_date: '2026-08-20', alert_count: 3,
                chat_snapshot: {buckets: [{date: '2026-08-20', start: '22:00',
                  end: '24:00', count: 0, messages: []}]}}]
  const app = createApp(t, {state: {records: old}})
  app.run(`currentView = 'silence'; renderSilenceRecords('room-1')`)
  await app.flush()
  assert.ok(!app.window.document.getElementById('events').textContent.includes('0 秒'))
})

test('新记录有 gap 就照常标，首尾无论多久都标', async t => {
  const rec = [{nickname: 'A', beijing_date: '2026-08-20', alert_count: 3,
                chat_snapshot: {mic_since: '22:40:00', until: '00:08:00',
                  tail_gap: 3480, gap_threshold: 900,
                  buckets: [{date: '2026-08-20', start: '22:00', end: '24:00',
                    count: 3, messages: [
                      {at: '22:58:03', text: '一', gap: 1083},
                      {at: '23:05:00', text: '二', gap: 417},
                      {at: '23:40:00', text: '三', gap: 2100}]}]}}]
  const app = createApp(t, {state: {records: rec}})
  app.run(`currentView = 'silence'; renderSilenceRecords('room-1')`)
  await app.flush()
  const text = app.window.document.getElementById('events').textContent
  assert.ok(text.includes('上麦后 静默 18 分钟'), '第一条无论多久都标')
  assert.ok(!text.includes('7 分钟'), '中间没到阈值的那段不标，免得刷屏时全是噪音')
  assert.ok(text.includes('静默 35 分钟'), '中间超过阈值的要标')
  assert.ok(text.includes('之后到出证据 静默 58 分钟'), '尾部无论多久都标')
})

// ---------- 读过之后，还在静默的人不能从面板消失 ----------

test('打开面板标为已读之后，仍在静默的人还要留在面板里', async t => {
  // 踩过的坑：「已读」曾经用叉掉表实现，而叉掉是把告警整个从返回里
  // 剔掉的。结果一打开面板、5 秒后轮询回来，面板就写「此刻没有人在
  // 静默」——而那几个人还在麦上、还在静默。看一眼就把要看的东西看没了。
  const alerts = [{id: 1, douyin_id: 'X', nickname: 'Ry.猫猫', hall_name: 'H',
                   alert_index: 3, created_at: 1, read: true}]
  const app = createApp(t, {state: {
    alerts, unread_by_hall: {}, douyin_ids: ['X']}})
  app.run(`silenceWatches = ['X']; silenceAlerts = ${JSON.stringify(alerts)}`)
  app.run(`openSilencePanel('X')`)
  await app.flush()
  const text = app.window.document.getElementById('silencePanel').textContent
  assert.ok(text.includes('Ry.猫猫'), '读过不等于不再静默')
  assert.ok(!text.includes('没有人在静默'))
})

test('读过的不再计入未读角标', t => {
  const app = createApp(t)
  assert.ok(!app.run(`silenceBellHtml('X', true, true, 0)`).includes('silence-unread'))
})

// ---------- 点击路由：这三条路径此前一条测试都没有 ----------

test('点厅按钮上的铃铛会打开面板（走真实的事件委托，不是直接调函数）', async t => {
  const app = createApp(t, {state: {douyin_ids: ['aaa']}})
  app.run(`
    currentRooms['room-a'] = {nickname: 'A厅', douyin_id: 'aaa'}
    silenceWatches = ['aaa']
    updateRoomSelector()
    document.querySelector('[data-silence-id="aaa"]').click()
  `)
  assert.ok(app.window.document.getElementById('silencePanel').textContent
    .includes('取消监测'), '铃铛点击要经由委托打开面板')
})

test('点面板里的「开始监测」真的会发订阅请求', async t => {
  const app = createApp(t)
  app.run(`
    currentRooms['room-a'] = {nickname: 'A厅', douyin_id: 'aaa'}
    silenceWatches = []
    openSilencePanel('aaa')
    document.querySelector('[data-silence-toggle="aaa"]').click()
  `)
  await app.flush()
  const put = app.calls.find(c => (c.opts || {}).method === 'PUT')
  assert.ok(put, '要真的发出订阅请求')
  assert.deepEqual(JSON.parse(put.opts.body).douyin_ids, ['aaa'])
})

test('点「取消监测」不能顺带把视图切进静默记录', async t => {
  // 取消监测和「查看静默记录」现在是面板里紧挨着的两行，点击路由
  // 一旦写串，取消订阅会连带跳视图。旧测试守过这条，改版后不能丢。
  const app = createApp(t, {state: {douyin_ids: ['aaa']}})
  app.run(`
    currentRooms['room-a'] = {nickname: 'A厅', douyin_id: 'aaa'}
    silenceWatches = ['aaa']
    currentView = 'mystery'
    openSilencePanel('aaa')
    document.querySelector('[data-silence-toggle="aaa"]').click()
  `)
  await app.flush()
  // 先确认点击真的路由到了退订，否则「视图没变」可能只是因为什么都没发生
  assert.ok(app.calls.some(c => (c.opts || {}).method === 'PUT'), '要真的退订')
  assert.equal(app.run(`currentView`), 'mystery', '取消订阅不该改视图')
})

test('点面板的 × 关掉面板', t => {
  const app = createApp(t)
  app.run(`
    currentRooms['room-a'] = {nickname: 'A厅', douyin_id: 'aaa'}
    openSilencePanel('aaa')
    document.querySelector('[data-silence-close]').click()
  `)
  assert.equal(app.window.document.getElementById('silencePanel').innerHTML, '')
  assert.equal(app.run(`silencePanelHall`), '')
})

// ---------- 两条回归 ----------

test('订阅信息加载完要立刻重画铃铛，否则刷新后已订阅的厅显示成没订阅', async t => {
  // 「只在角标变了才重画」这个守卫是为了保住横向滚动位置，但铃铛的
  // 订阅态也是同一个函数画的。零未读是绝大多数时间的稳态，不在
  // loadSilenceWatches 里补一次的话，刷新页面后蓝铃铛会变回灰的。
  const app = createApp(t, {state: {douyin_ids: ['aaa'], unread_by_hall: {}}})
  app.run(`
    currentRooms['room-a'] = {nickname: 'A厅', douyin_id: 'aaa'}
    silenceWatches = []
    updateRoomSelector()
  `)
  assert.ok(!app.run(`document.querySelector('[data-silence-id="aaa"]').className`)
    .includes('silence-bell-on'), '前置条件：先是未订阅的样子')
  await app.run(`loadSilenceWatches()`)
  assert.ok(app.run(`document.querySelector('[data-silence-id="aaa"]').className`)
    .includes('silence-bell-on'), '加载到订阅后铃铛要变成已订阅')
})

test('告警 id 掉出返回窗口再回来，不能重复弹系统通知', t => {
  // 接口有 LIMIT 50，未结告警攒多了老 id 会被挤出去；某人恢复发言后
  // 他名下的告警被一次性 resolve，位置空出来，老 id 又回到响应里。
  // 曾经按「本次响应里还在不在」裁剪已弹集合，这种往返就会重复弹。
  const app = createApp(t)
  const shown = []
  app.window.Notification = function (title) { shown.push(title) }
  app.window.Notification.permission = 'granted'
  const mk = id => `{id:${id}, douyin_id:'X', nickname:'n${id}', hall_name:'H', alert_index:1, created_at:1}`
  app.run(`notifySilenceAlerts([${mk(1)},${mk(2)},${mk(3)}])`)
  assert.equal(shown.length, 3)
  app.run(`notifySilenceAlerts([${mk(2)},${mk(3)}])`)      // id 1 被挤出窗口
  app.run(`notifySilenceAlerts([${mk(1)},${mk(2)},${mk(3)}])`)  // 又回来了
  assert.equal(shown.length, 3, 'id 1 不能因为往返就再弹一次')
})

// ---------- 老记录：裁掉他根本不在麦上的那些档 ----------

test('老记录里不在麦上的那些档不显示，别再是满屏「0 条」', async t => {
  // 上线前写的记录快照覆盖整天/整轮，里面全是他根本不在麦上的时段。
  // 记录行上存了 mic_since 和 created_at，按它裁掉即可——不编任何数字，
  // 只是不显示无关的档。
  const mic = 1798902000            // 2027-01-02 23:00 +08:00
  const until = mic + 90 * 60       // 次日 00:30
  const old = [{nickname: 'Ry.猫猫', beijing_date: '2027-01-03', alert_count: 3,
                mic_since: mic, created_at: until,
                chat_snapshot: {buckets: [
                  {date: '2027-01-02', start: '00:00', end: '02:00', count: 0, messages: []},
                  {date: '2027-01-02', start: '12:00', end: '14:00', count: 0, messages: []},
                  {date: '2027-01-02', start: '22:00', end: '24:00', count: 1,
                   messages: [{at: '23:10:00', text: '在麦上说的'}]},
                  {date: '2027-01-03', start: '00:00', end: '02:00', count: 0, messages: []},
                  {date: '2027-01-03', start: '10:00', end: '12:00', count: 0, messages: []}]}}]
  const app = createApp(t, {state: {records: old}})
  app.run(`currentView = 'silence'; renderSilenceRecords('room-1')`)
  await app.flush()
  const text = app.window.document.getElementById('events').textContent
  assert.ok(text.includes('22:00–24:00'), '在麦那一档要留')
  assert.ok(text.includes('在麦上说的'))
  assert.ok(text.includes('2027-01-03 00:00–02:00'), '跨到次日那一档也在麦上，要留')
  assert.ok(!text.includes('12:00–14:00'), '中午那档他根本不在麦上，别显示')
  assert.ok(!text.includes('10:00–12:00'), '出证据之后的档也别显示')
})

test('新记录只有一档，裁剪逻辑不能把它裁没', async t => {
  const mic = 1798902000
  const rec = [{nickname: 'A', beijing_date: '2027-01-02', alert_count: 3,
                mic_since: mic, created_at: mic + 48 * 60, band_start: mic - 3600,
                chat_snapshot: {mic_since: '23:00:00', until: '23:48:00',
                  tail_gap: 2880, gap_threshold: 900,
                  buckets: [{date: '2027-01-02', start: '22:00', end: '24:00',
                             count: 0, messages: []}]}}]
  const app = createApp(t, {state: {records: rec}})
  app.run(`currentView = 'silence'; renderSilenceRecords('room-1')`)
  await app.flush()
  const text = app.window.document.getElementById('events').textContent
  assert.ok(text.includes('22:00–24:00'))
  assert.ok(text.includes('整轮上麦 静默 48 分钟'), '一句没说，尾部标整段')
})

test('跨档持续静默时，面板要显示当前档的次数，不是上一档的终值', async t => {
  // 分档归零之后「alert_index 最大」不再等于「最新」：主持只要不说话、
  // 不下麦，上一档的 1..7 会和本档的 1..3 同时留在未读列表里。
  // 按 alert_index 挑就会显示上一档那个 7——正是这次改动要消灭的数字。
  const alerts = [
    {id: 1, douyin_id: 'X', nickname: 'Ry.眠鱼怪', hall_name: 'H',
     alert_index: 7, created_at: 1000},          // 上一档的终值
    {id: 2, douyin_id: 'X', nickname: 'Ry.眠鱼怪', hall_name: 'H',
     alert_index: 3, created_at: 9000},          // 本档，最新
  ]
  const app = createApp(t, {state: {alerts, douyin_ids: ['X']}})
  app.run(`silenceWatches = ['X']; silenceAlerts = ${JSON.stringify(alerts)}`)
  app.run(`openSilencePanel('X')`)
  await app.flush()
  const text = app.window.document.getElementById('silencePanel').textContent
  assert.ok(text.includes('第 3 次'), '要显示当前档的次数')
  assert.ok(!text.includes('第 7 次'), '不能显示上一档的终值')
})

test('裁掉档之后，留下来的间隔标注不能照旧显示——锚点已经不成立了', async t => {
  // 藏掉一档会给保留下来的 gap 换锚点：老快照里第一条的 gap 是「从整天
  // 00:00 到这条」，裁掉前面的档之后它还挂着「上麦后」的名头，就变成
  // 一句测量过但不成立的断言。这是永久证据，宁可少显示也别显示错。
  const mic = 1798902000              // 2027-01-02 23:00
  const until = mic + 90 * 60
  const old = [{nickname: 'A', beijing_date: '2027-01-03', alert_count: 3,
                mic_since: mic, created_at: until,
                chat_snapshot: {mic_since: '00:00:00', until: '00:30:00',
                  tail_gap: 37080, gap_threshold: 900,
                  buckets: [
                    {date: '2027-01-02', start: '12:00', end: '14:00', count: 0, messages: []},
                    {date: '2027-01-02', start: '22:00', end: '24:00', count: 1,
                     messages: [{at: '23:10:00', text: '在麦上说的', gap: 36600}]}]}}]
  const app = createApp(t, {state: {records: old}})
  app.run(`currentView = 'silence'; renderSilenceRecords('room-1')`)
  await app.flush()
  const text = app.window.document.getElementById('events').textContent
  assert.ok(text.includes('在麦上说的'), '内容照常显示')
  assert.ok(!text.includes('10 小时'), '换了锚点的间隔不能照旧标出来')
  assert.ok(!text.includes('在麦 00:00:00'), '老快照自带的区间也跟档位矛盾，别显示')
  assert.ok(text.includes('口径'), '要说明这是老记录、口径已变')
})

// ---------- 从公屏进静默记录 ----------

test('从公屏进静默记录，要真的切过去，不能标题变了内容还是公屏', async t => {
  // switchMode 是唯一切换 #events / #feedPane 显隐的地方。静默入口原来
  // 直接设 currentView 再调 selectRoom，绕过了它——从公屏进来时 #events
  // 还是 display:none、#feedPane 还显示着，记录渲染进了一个隐藏容器，
  // 屏幕上留着公屏。线上表现就是「标题成了静默记录、下面还是聊天」。
  const app = createApp(t, {state: {douyin_ids: ['aaa'], records: []}})
  app.run(`
    currentRooms['room-a'] = {nickname: 'A厅', douyin_id: 'aaa'}
    selectedRoomId = 'room-a'
    silenceWatches = ['aaa']
    switchMode('feed')
  `)
  assert.equal(app.run(`document.getElementById('events').style.display`), 'none',
    '前置条件：公屏视图下 #events 是隐藏的')

  app.run(`
    openSilencePanel('aaa')
    document.querySelector('[data-silence-records="aaa"]').click()
  `)
  await app.flush()

  assert.notEqual(app.run(`document.getElementById('events').style.display`), 'none',
    '进静默记录后主内容区要显示出来')
  assert.equal(app.run(`document.getElementById('feedPane').style.display`), 'none',
    '公屏面板要收起来')
  assert.equal(app.run(`currentView`), 'silence')
})

test('从静默记录切回公屏，公屏要能正常回来', async t => {
  const app = createApp(t, {state: {douyin_ids: ['aaa'], records: []}})
  app.run(`
    currentRooms['room-a'] = {nickname: 'A厅', douyin_id: 'aaa'}
    selectedRoomId = 'room-a'
    silenceWatches = ['aaa']
    switchMode('feed')
    openSilencePanel('aaa')
    document.querySelector('[data-silence-records="aaa"]').click()
  `)
  await app.flush()
  app.run(`switchMode('feed')`)
  assert.equal(app.run(`document.getElementById('events').style.display`), 'none')
  assert.notEqual(app.run(`document.getElementById('feedPane').style.display`), 'none')
  assert.equal(app.run(`currentView`), 'feed')
})

test('进静默记录时，公屏按钮不能还亮着', async t => {
  const app = createApp(t, {state: {douyin_ids: ['aaa'], records: []}})
  app.run(`
    currentRooms['room-a'] = {nickname: 'A厅', douyin_id: 'aaa'}
    selectedRoomId = 'room-a'
    silenceWatches = ['aaa']
    switchMode('feed')
    openSilencePanel('aaa')
    document.querySelector('[data-silence-records="aaa"]').click()
  `)
  await app.flush()
  assert.ok(!app.run(`document.getElementById('modeFeed').className`).includes('active'),
    '已经不在公屏了，按钮不该还亮着')
})
