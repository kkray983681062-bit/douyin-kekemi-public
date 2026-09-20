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
  if (!window.CSS.escape) window.CSS.escape = v => String(v)
  const context = dom.getInternalVMContext()
  new vm.Script(APP_SOURCE, {filename: 'static/app.js'}).runInContext(context)
  t.after(() => window.close())
  return {run(code) { return new vm.Script(code).runInContext(context) }}
}

test('周榜的展开按钮走周榜自己的开关，不能误触发日榜', t => {
  const app = createApp(t)
  const html = app.run(`rankMoreHtml(true, 185, 'weekly')`)
  assert.ok(html.includes('setWeeklyRankShowAll'), '周榜要用周榜的开关')
  assert.ok(!html.includes('setDailyRankShowAll'))
  const daily = app.run(`rankMoreHtml(true, 185, 'visitor')`)
  assert.ok(daily.includes('setDailyRankShowAll'), '日榜仍用日榜的开关')
})

test('切厅会把周榜的展开状态一起重置', t => {
  const app = createApp(t)
  app.run(`weeklyRankShowAll = true; resetWeeklyRankPaging()`)
  assert.equal(app.run(`weeklyRankShowAll`), false)
})

test('展开状态会改变 weeklyTag，否则静默刷新会拦住重绘', t => {
  // tag 里必须体现 top/all，不然点了展开画面不会更新
  const line = APP_SOURCE.split(String.fromCharCode(10)).find(l => l.includes('const weeklyTag'))
  assert.ok(line, '找不到 weeklyTag')
  assert.ok(line.includes('weeklyRankShowAll'), 'weeklyTag 必须包含展开状态')
})

test('周榜静默刷新：已经在看同一厅同一周期时不盖加载占位', t => {
  const src = APP_SOURCE
  const idx = src.indexOf('正在加载周榜')
  const before = src.slice(Math.max(0, idx - 260), idx)
  assert.ok(before.includes('container.dataset.weeklyTag !== weeklyTag'),
    '加载占位必须被条件包住，否则每 30 秒整块闪一次')
})
