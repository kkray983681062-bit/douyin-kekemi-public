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

test('榜单被截断时给出「显示全部」入口并说明总人数', t => {
  const app = createApp(t)
  const html = app.run(`rankMoreHtml(true, 185, 'visitor')`)
  assert.ok(html.includes('185'), '要告诉用户一共多少人')
  assert.ok(/显示全部/.test(html), '要有展开入口')
})

test('没截断时不出现展开入口', t => {
  const app = createApp(t)
  assert.equal(app.run(`rankMoreHtml(false, 12, 'visitor')`), '')
})

test('点展开会改成请求全量', t => {
  const app = createApp(t)
  app.run(`dailyRankShowAll = false; setDailyRankShowAll(true)`)
  assert.equal(app.run(`dailyRankShowAll`), true)
})

test('切厅时重置回只看前几名', t => {
  const app = createApp(t)
  app.run(`dailyRankShowAll = true; resetDailyRankPaging()`)
  assert.equal(app.run(`dailyRankShowAll`), false)
})
