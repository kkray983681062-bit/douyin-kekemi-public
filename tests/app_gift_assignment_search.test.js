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

// 归属修正页只需要 #events 容器；其余壳子元素给空实现即可。
function createApp(t) {
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
  window.fetch = async () => ({json: async () => ({success: true})})
  window.EventSource = class { close() {} }
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
    html() { return window.document.getElementById('events').innerHTML },
    text() { return window.document.getElementById('events').textContent },
  }
}

// 三条礼物：刷的人 / 原收礼主持 / 修正后主持 各不相同，便于分辨维度串没串。
const SEED = `
giftAssignmentCache = [
  {id: 11, display: '示例用户甲', content: '玫瑰', ticket_count: 1,
   original_recipient_name: 'Ry.灰', adjusted_recipient_name: '', note: ''},
  {id: 22, display: '無', content: '热气球', ticket_count: 520,
   original_recipient_name: 'Ry.淚', adjusted_recipient_name: '主持甲', note: ''},
  {id: 33, display: 'kiyoshi', content: '跑车', ticket_count: 1200,
   original_recipient_name: 'Ry.灰', adjusted_recipient_name: '', note: ''}
]
giftAssignmentSearch = ''
giftAssignmentSearchBy = 'sender'
document.getElementById('events').innerHTML =
  assignmentSearchBarHtml() + '<div id="giftAssignmentList"></div>'
paintGiftAssignmentList()
`

test('空搜索显示全部礼物', t => {
  const app = createApp(t)
  app.run(SEED)
  const text = app.text()
  assert.ok(text.includes('示例用户甲') && text.includes('無') && text.includes('kiyoshi'))
})

test('按刷的人过滤，大小写不敏感且不串到主持名', t => {
  const app = createApp(t)
  app.run(SEED)
  app.run(`onGiftAssignmentSearch('KIYO')`)
  const text = app.text()
  assert.ok(text.includes('kiyoshi'), '应命中送礼人 kiyoshi')
  assert.ok(!text.includes('示例用户甲'))
  // 主持名不该在「刷的人」维度下被搜到
  app.run(`onGiftAssignmentSearch('Ry.灰')`)
  assert.ok(app.text().includes('没有'), '切在刷的人时搜主持名应无结果')
})

test('切到收礼主持后按修正后名字优先匹配', t => {
  const app = createApp(t)
  app.run(SEED)
  app.run(`setGiftAssignmentSearchBy('recipient'); onGiftAssignmentSearch('主持甲')`)
  const text = app.text()
  assert.ok(text.includes('無'), '修正后主持=主持甲的那条应命中')
  assert.ok(!text.includes('kiyoshi'))
  // 该条已被修正，原收礼名不应再匹配
  app.run(`onGiftAssignmentSearch('Ry.淚')`)
  assert.ok(!app.text().includes('無'), '已修正的礼物不应再按原收礼名匹配')
})

test('过滤后卡片仍用原始下标，保存不会存到别的礼物', t => {
  const app = createApp(t)
  app.run(SEED)
  app.run(`onGiftAssignmentSearch('kiyoshi')`)
  const html = app.html()
  // kiyoshi 在缓存里是第 3 条（下标 2），过滤后仍必须是 2 而不是 0
  assert.ok(html.includes('saveGiftAssignment(2)'), '必须用原始下标 2')
  assert.ok(html.includes('assignment-key-2'), '输入框 id 也要用原始下标')
  assert.ok(!html.includes('saveGiftAssignment(0)'))
  const mapped = app.run(`giftAssignmentCache[2].id`)
  assert.equal(mapped, 33, '下标 2 必须仍指向 kiyoshi 那条礼物')
})

test('搜索时只重绘列表，搜索框不被重建（不丢焦点）', t => {
  const app = createApp(t)
  app.run(SEED)
  const before = app.run(`document.getElementById('assignmentSearchInput')`)
  app.run(`onGiftAssignmentSearch('無')`)
  app.run(`setGiftAssignmentSearchBy('recipient')`)
  const after = app.run(`document.getElementById('assignmentSearchInput')`)
  assert.ok(before === after, '搜索框必须是同一个 DOM 节点，否则输入会丢焦点')
})

test('无匹配时给出带维度和搜索词的空状态', t => {
  const app = createApp(t)
  app.run(SEED)
  app.run(`onGiftAssignmentSearch('查无此人')`)
  const text = app.text()
  assert.ok(text.includes('查无此人'))
  assert.ok(text.includes('刷的人'), '空状态要说明当前搜的是哪个维度')
})

test('切换开关会更新高亮状态', t => {
  const app = createApp(t)
  app.run(SEED)
  app.run(`setGiftAssignmentSearchBy('recipient')`)
  assert.equal(app.run(`document.getElementById('asByRecipient').className.includes('active')`), true)
  assert.equal(app.run(`document.getElementById('asBySender').className.includes('active')`), false)
})
