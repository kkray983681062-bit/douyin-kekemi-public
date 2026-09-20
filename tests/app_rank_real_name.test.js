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
  return {run(code) { return new vm.Script(code).runInContext(context) }}
}

test('日榜里解析出真名的神秘人，用括号带出真名', t => {
  const app = createApp(t)
  const html = app.run(`renderRankRows([
    {rank: 1, display: '神秘人602191', real_name: 'kiyo', tickets: 7694}
  ], 'sender_key', '空')`)
  // 按用户实际看到的文字断言（中间隔着标签，不能用裸正则匹配 HTML）
  const text = app.run(`(() => {
    const d = document.createElement('div')
    d.innerHTML = ${JSON.stringify(html)}
    return d.textContent
  })()`)
  assert.ok(text.includes('神秘人602191（kiyo）'),
    `马甲名后面要紧跟括号真名，实际: ${text}`)
})

test('没解析出真名的不加括号', t => {
  const app = createApp(t)
  const html = app.run(`renderRankRows([
    {rank: 1, display: '神秘人111697', real_name: '', tickets: 6000}
  ], 'sender_key', '空')`)
  assert.ok(html.includes('神秘人111697'))
  assert.ok(!html.includes('（'), '没有真名时不该出现括号')
})

test('普通用户不会把名字重复一遍', t => {
  const app = createApp(t)
  const html = app.run(`renderRankRows([
    {rank: 1, display: 'CACTUS', tickets: 7500}
  ], 'sender_key', '空')`)
  assert.ok(!html.includes('（'), '没有 real_name 字段时也不能出括号')
})

test('真名里的 HTML 会被转义（真名来自抖音，攻击者可控）', t => {
  const app = createApp(t)
  const html = app.run(`renderRankRows([
    {rank: 1, display: '神秘人1', real_name: '<img src=x onerror=alert(1)>', tickets: 1}
  ], 'sender_key', '空')`)
  assert.ok(!html.includes('<img src=x'), '真名必须转义')
  assert.ok(html.includes('&lt;img'), '应转义成实体')
})

test('合并来的行不在界面上做标记（合并靠 id 匹配，本来就可信）', t => {
  // 一度加过一个 ⇢N 角标，本意是万一身份库有条错关联能一眼看出来。
  // Ray 看到之后第一反应是「这什么意思」——符号本身不表达意思、解释
  // 藏在悬停提示里，等于没解释。而合并匹配的是 sec_uid / webcast_uid /
  // numeric_uid，从不按显示名匹配，关联稳定可信，不值得为它占一块界面。
  // merged_keys 仍在响应里，排查时按它查即可。
  const app = createApp(t)
  const merged = app.run(`renderRankRows([{sender_key:'id:706', display:'潯',
    tickets:13901, rank:2, merged_keys:2}], 'sender_key', '空')`)
  assert.ok(!merged.includes('rank-merged'))
  assert.ok(!merged.includes('⇢'))
  assert.ok(merged.includes('13,901'), '票数照常显示')
})
