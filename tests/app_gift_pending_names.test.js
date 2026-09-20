// 礼物库要把「没定价的皮肤名」显示在最前面。
//
// 礼物库按 gift_id 列，一个 id 一行一个数字。但一个 gift_id 底下可能挂着
// 多个皮肤名、各价不同：2026-08-22 线上 id=3729 底下 6 个名字 3 种价
// （钻石飞机 3600 / 私人飞机 3000 / 碧空飞机 没定价）。没定价的那个在
// 按 id 的列表里根本不出现，于是永远补不上价、对应礼物一直不计入账目。

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { JSDOM } = require('jsdom')

const APP_SOURCE = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'app.js'), 'utf8'
)

const PENDING = [{
  gift_id: '3729', gift_name: '碧空飞机', occurrence_count: 1,
  first_seen: 1787000000, last_seen: 1787000000,
  siblings: [
    {gift_name: '钻石飞机', unit_diamonds: 3600},
    {gift_name: '私人飞机', unit_diamonds: 3000},
  ],
}]
const RECORDS = [{
  gift_id: '9001', gift_name: '小心心', unit_diamonds: 1,
  price_source: 'douyin', price_pending: false, occurrence_count: 500,
  first_seen: 1787000000, last_seen: 1787000000, note: '',
}]

function createApp(t, {fetchImpl} = {}) {
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
  window.fetch = fetchImpl || (async () => ({json: async () => ({success: true})}))
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

test('没定价的名字排在按 id 的卡片前面', t => {
  const app = createApp(t)
  app.run(`
    giftLibraryCache = ${JSON.stringify(RECORDS)}
    giftPendingNames = ${JSON.stringify(PENDING)}
    renderGiftLibraryCards(giftLibraryCache, 0)
  `)
  const html = app.window.document.getElementById('events').innerHTML
  const posPending = html.indexOf('碧空飞机')
  const posNormal = html.indexOf('小心心')
  assert.ok(posPending >= 0, '待定价的名字要出现')
  assert.ok(posNormal >= 0, '普通礼物照常出现')
  assert.ok(posPending < posNormal, '待定价的必须排在前面')
})

test('待定价的卡片要说清楚「不计入账目」，并给出同 id 的参照价', t => {
  const app = createApp(t)
  app.run(`
    giftLibraryCache = []
    giftPendingNames = ${JSON.stringify(PENDING)}
    renderGiftLibraryCards([], 0)
  `)
  const html = app.window.document.getElementById('events').innerHTML
  assert.match(html, /不计入账目/, '要写明后果，不能只标个红')
  assert.match(html, /3729/, '要显示 gift_id')
  assert.match(html, /钻石飞机/, '同 id 的参照价：名字')
  assert.match(html, /3600/, '同 id 的参照价：价格')
  assert.match(html, /私人飞机/)
  assert.match(html, /3000/)
})

test('一个待定价的都没有时，不显示这一段', t => {
  const app = createApp(t)
  app.run(`
    giftLibraryCache = ${JSON.stringify(RECORDS)}
    giftPendingNames = []
    renderGiftLibraryCards(giftLibraryCache, 0)
  `)
  const html = app.window.document.getElementById('events').innerHTML
  assert.doesNotMatch(html, /不计入账目/)
  assert.match(html, /小心心/)
})

test('保存打到按名字定价的接口，带上名字、价格和原因', async t => {
  let hit = null
  const app = createApp(t, {fetchImpl: async (url, options) => {
    if (String(url).includes('gift-names')) hit = {url, options}
    return {json: async () => ({success: true, result: {recalculated_rows: 2}})}
  }})
  app.run(`
    giftLibraryCache = []
    giftPendingNames = ${JSON.stringify(PENDING)}
    renderGiftLibraryCards([], 0)
    document.getElementById('gift-name-price-0').value = '3000'
    document.getElementById('gift-name-reason-0').value = '现场确认'
  `)
  await app.run(`saveGiftNamePrice(0)`)
  assert.ok(hit, '应该发出请求')
  assert.match(hit.url, /\/api\/admin\/gift-names$/)
  assert.equal(hit.options.method, 'PUT')
  const body = JSON.parse(hit.options.body)
  assert.equal(body.gift_name, '碧空飞机')
  assert.equal(body.unit_diamonds, 3000)
  assert.equal(body.reason, '现场确认')
})

test('价格不是正整数、或原因留空，都不发请求', async t => {
  for (const [price, reason] of [['0', '理由'], ['-5', '理由'], ['abc', '理由'],
                                 ['3.5', '理由'], ['3000', ''], ['3000', '  ']]) {
    let called = 0
    const app = createApp(t, {fetchImpl: async url => {
      if (String(url).includes('gift-names')) called += 1
      return {json: async () => ({success: true, result: {}})}
    }})
    app.run(`
      giftLibraryCache = []
      giftPendingNames = ${JSON.stringify(PENDING)}
      renderGiftLibraryCards([], 0)
      document.getElementById('gift-name-price-0').value = ${JSON.stringify(price)}
      document.getElementById('gift-name-reason-0').value = ${JSON.stringify(reason)}
    `)
    await app.run(`saveGiftNamePrice(0)`)
    assert.equal(called, 0, `price=${price} reason=${JSON.stringify(reason)}`)
  }
})

test('礼物名要转义，不能当成标签渲染', t => {
  const app = createApp(t)
  app.run(`
    giftLibraryCache = []
    giftPendingNames = [{gift_id:'1', gift_name:'<img src=x onerror=alert(1)>',
      occurrence_count:1, first_seen:0, last_seen:0, siblings:[]}]
    renderGiftLibraryCards([], 0)
  `)
  const html = app.window.document.getElementById('events').innerHTML
  assert.doesNotMatch(html, /<img src=x/)
  assert.match(html, /&lt;img/)
})
