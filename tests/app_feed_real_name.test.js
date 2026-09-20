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
  window.fetch = () => new Promise(() => {})
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

function nameOf(app, ev) {
  return app.run(`feedDisplayName(${JSON.stringify(ev)})`)
}

// ---- 该揭真名的：神秘人 / 匿名用户 ----

test('dou 马甲揭出真名', t => {
  const app = createApp(t)
  const out = nameOf(app, {display: 'dou7101055', real_name: '示例用户甲'})
  assert.ok(out.includes('示例用户甲'), '马甲名应带出真名')
  assert.ok(out.includes('真实'))
})

test('神秘人编号揭出真名', t => {
  const app = createApp(t)
  const out = nameOf(app, {display: '神秘人12345', matched_identity: {real_name: '無'}})
  assert.ok(out.includes('無'))
})

test('后端明确标了非普通用户也揭', t => {
  const app = createApp(t)
  const out = nameOf(app, {display: '某马甲', real_name: '真名甲', is_regular: false})
  assert.ok(out.includes('真名甲'))
})

// ---- 不该揭的：普通用户改昵称 ----

test('普通用户改过昵称，不该把曾用名当真实身份公示', t => {
  const app = createApp(t)
  // 线上真实案例：同一个 sec_uid 用过 Moi-Nixxxx(174次) 和 Moi-Nixxx(51次)，
  // is_regular=1 是普通用户，身份库把旧名存成了 real_name。
  const out = nameOf(app, {
    display: 'Moi-Nixxx',
    matched_identity: {real_name: 'Moi-Nixxxx'},
    is_regular: true
  })
  assert.equal(out, 'Moi-Nixxx', '普通用户改名不该带括号')
  assert.ok(!out.includes('真实'))
})

test('没有任何标记的普通名字也不揭', t => {
  const app = createApp(t)
  const out = nameOf(app, {display: '示例用户甲', matched_identity: {real_name: '旧名字'}})
  assert.equal(out, '示例用户甲', '来源不带 is_regular 时，普通名字默认不揭')
})

test('马甲判定与后端 is_real_mystery_user 同定义（dou 需长于 5）', t => {
  const app = createApp(t)
  // 后端定义：dou 开头且长度>5 才算马甲。前端不另立更严的标准，
  // 否则两边对「谁是神秘人」的判断会分叉。
  const short = nameOf(app, {display: 'dou12', matched_identity: {real_name: '别的名'}})
  assert.equal(short, 'dou12', '长度不足 6 不算马甲')
  const long = nameOf(app, {display: 'dou7101055', matched_identity: {real_name: '示例用户甲'}})
  assert.ok(long.includes('示例用户甲'), '长度够则算马甲，照常揭真名')
})

test('真名与显示名相同则不带括号', t => {
  const app = createApp(t)
  assert.equal(nameOf(app, {display: 'dou7101055', real_name: 'dou7101055'}), 'dou7101055')
})

test('名字里的 HTML 仍被转义', t => {
  const app = createApp(t)
  const out = nameOf(app, {display: 'dou9999999', real_name: '<img src=x onerror=alert(1)>'})
  assert.ok(!out.includes('<img'), '真名必须转义')
  assert.ok(out.includes('&lt;img'))
})
