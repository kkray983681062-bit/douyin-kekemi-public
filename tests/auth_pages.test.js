const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { JSDOM } = require('jsdom')

const ROOT = path.join(__dirname, '..')

function read(relativePath) {
  return fs.readFileSync(path.join(ROOT, relativePath), 'utf8')
}

function evaluate(source, dom, filename) {
  const context = dom.getInternalVMContext()
  new vm.Script(source, {filename}).runInContext(context)
  return context
}

test('auth shell uses confirmed assets and only the two-line left wordmark', () => {
  const base = read('templates/auth_base.html')
  const css = read('static/auth.css')

  assert.match(base, /\/static\/assets\/kekemi-avatar\.jpg/)
  assert.match(css, /\/static\/assets\/kekemi-auth-background\.jpg/)
  assert.match(base, />示例</)
  assert.match(base, />RU·YUE</)
  assert.doesNotMatch(base, /直播数据仅向审核通过的成员开放/)
})

test('login and registration forms expose required fields without leaking invite', () => {
  const login = read('templates/login.html')
  const register = read('templates/register.html')

  assert.match(login, /name="identifier"/)
  assert.match(login, /name="password"/)
  assert.match(login, /id="forgotForm"/)
  assert.match(register, /name="username"/)
  assert.match(register, /name="email"/)
  assert.match(register, /name="password"/)
  assert.match(register, /name="invite_code"/)
  assert.doesNotMatch(register, /value="kekemi"/i)
})

test('reset page strips recovery token from URL and sends it only in POST body', async () => {
  const dom = new JSDOM(`<!doctype html><html><body data-page="reset">
    <form id="resetForm">
      <input name="password" value="newpassword8">
      <input name="password_confirm" value="newpassword8">
      <button type="submit">保存</button>
    </form>
    <div id="formMessage"></div>
  </body></html>`, {
    url: 'http://127.0.0.1:5000/reset-password#access_token=recovery-secret&type=recovery',
    runScripts: 'outside-only'
  })
  const calls = []
  dom.window.fetch = async (url, options) => {
    calls.push({url, options})
    return {ok: true, status: 200, json: async () => ({success: true})}
  }
  evaluate(read('static/auth.js'), dom, 'static/auth.js')
  dom.window.document.dispatchEvent(new dom.window.Event('DOMContentLoaded'))

  assert.equal(dom.window.location.hash, '')
  dom.window.document.getElementById('resetForm').dispatchEvent(
    new dom.window.Event('submit', {bubbles: true, cancelable: true})
  )
  await new Promise(resolve => setImmediate(resolve))

  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, '/api/auth/reset-password')
  const payload = JSON.parse(calls[0].options.body)
  assert.equal(payload.recovery_access_token, 'recovery-secret')
  assert.doesNotMatch(calls[0].url, /recovery-secret/)
  dom.window.close()
})

test('authenticated auth requests bind the csrf cookie to the request header', async () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://127.0.0.1:5000/account-status',
    runScripts: 'outside-only'
  })
  dom.window.document.cookie = 'kekemi_csrf=csrf-token; path=/'
  let captured
  dom.window.fetch = async (url, options) => {
    captured = {url, options}
    return {ok: true, status: 200, json: async () => ({success: true})}
  }
  const context = evaluate(read('static/auth.js'), dom, 'static/auth.js')

  await context.authApiFetch('/api/auth/logout', {method: 'POST'})

  assert.equal(captured.options.headers['X-CSRF-Token'], 'csrf-token')
  dom.window.close()
})

test('admin user cards show role controls only to super administrators', () => {
  const source = read('static/admin.js')
  const user = {
    id: 'member-1', username: 'Member', email: 'member@example.com',
    role: 'member', status: 'active', status_reason: ''
  }

  for (const [role, shouldShow] of [['admin', false], ['super_admin', true]]) {
    const dom = new JSDOM(`<!doctype html><html><body data-role="${role}">
      <div id="userList"></div><div id="adminMessage"></div>
    </body></html>`, {
      url: 'http://127.0.0.1:5000/admin',
      runScripts: 'outside-only'
    })
    dom.window.fetch = async () => ({
      ok: true, status: 200, json: async () => ({success: true, users: []})
    })
    const context = evaluate(source, dom, 'static/admin.js')
    context.renderAdminUsers([user])
    const roleButton = dom.window.document.querySelector('[data-action="role"]')
    assert.equal(Boolean(roleButton), shouldShow)
    dom.window.close()
  }
})

test('status page contains distinct pending rejected and suspended paths', () => {
  const status = read('templates/account_status.html')

  assert.match(status, /current_user\.status == 'pending'/)
  assert.match(status, /current_user\.status == 'rejected'/)
  assert.match(status, /current_user\.status == 'suspended'/)
  assert.match(status, /重新提交审核/)
  assert.match(status, /退出登录/)
})

// ---- 删除账号（硬删，不可恢复）----
//
// 这个按钮跟「暂停」「强制下线」排在同一行，点错的代价是不可恢复的，
// 所以确认方式是「必须手打目标用户名」，不是点一下「确定」。

function adminDom(role) {
  const dom = new JSDOM(`<!doctype html><html><body data-role="${role}">
    <div id="userList"></div><div id="adminMessage"></div><div id="auditList"></div>
  </body></html>`, {url: 'http://127.0.0.1:5000/admin', runScripts: 'outside-only'})
  dom.window.fetch = async () => ({
    ok: true, status: 200, json: async () => ({success: true, users: [], logs: []})
  })
  return dom
}

// admin.js 一加载就会去拉用户列表/备注/审计日志。它们的 .then 必须在
// window.close() 之前跑完，否则 document 已经没了，冒出一堆未处理拒绝
// 把整个文件判失败——单条测试却全是 ✔，很容易看漏。
async function settle() {
  for (let i = 0; i < 4; i += 1) await new Promise(r => setImmediate(r))
}

const MEMBER = {
  id: 'member-1', username: '小淘气', email: '3488325539@qq.com',
  role: 'member', status: 'active', status_reason: ''
}

test('删除账号按钮只对超管出现', async () => {
  for (const [role, shouldShow] of [['admin', false], ['super_admin', true]]) {
    const dom = adminDom(role)
    const context = evaluate(read('static/admin.js'), dom, 'static/admin.js')
    context.renderAdminUsers([MEMBER])
    const btn = dom.window.document.querySelector('[data-action="delete"]')
    assert.equal(Boolean(btn), shouldShow, role)
    await settle()
    await settle()
    dom.window.close()
  }
})

test('目标是超管时不给删除入口', async () => {
  // 能不能删超管由后端判定（要留住最后一个超管）。前端不给入口，
  // 是为了不做出「点了就能删」的暗示，而不是把它当安全边界。
  const dom = adminDom('super_admin')
  const context = evaluate(read('static/admin.js'), dom, 'static/admin.js')
  context.renderAdminUsers([{...MEMBER, id: 'super-1', role: 'super_admin'}])
  assert.equal(dom.window.document.querySelector('[data-action="delete"]'), null)
  await settle()
  await settle()
  dom.window.close()
})

test('用户名打错就不发请求', async () => {
  const dom = adminDom('super_admin')
  let called = 0
  dom.window.fetch = async url => { if (String(url).includes('/delete')) called += 1
    return {ok: true, status: 200, json: async () => ({success: true, users: [], logs: []})} }
  const context = evaluate(read('static/admin.js'), dom, 'static/admin.js')
  context.renderAdminUsers([MEMBER])
  const card = dom.window.document.querySelector('[data-user-id="member-1"]')
  dom.window.prompt = () => '小淘'          // 少打一个字
  await context.runUserAction(card, 'delete')
  assert.equal(called, 0, '名字不完全一致就一个请求都不该发出去')
  await settle()
  dom.window.close()
})

test('前后空格不算打错，但错字算', async () => {
  const cases = [['  小淘气  ', true], ['小淘气', true], ['小陶气', false], ['', false]]
  for (const [typed, shouldSend] of cases) {
    const dom = adminDom('super_admin')
    let hit = null
    dom.window.fetch = async (url, options) => {
      if (String(url).includes('/delete')) hit = {url, options}
      return {ok: true, status: 200, json: async () => ({success: true, users: [], logs: []})} }
    const context = evaluate(read('static/admin.js'), dom, 'static/admin.js')
    context.renderAdminUsers([MEMBER])
    const card = dom.window.document.querySelector('[data-user-id="member-1"]')
    let round = 0
    dom.window.prompt = () => (round++ === 0 ? typed : '离职')
    await context.runUserAction(card, 'delete')
    assert.equal(Boolean(hit), shouldSend, JSON.stringify(typed))
    await settle()
    dom.window.close()
  }
})

test('名字对上之后才问原因，请求打到 delete 接口并带上原因', async () => {
  const dom = adminDom('super_admin')
  let hit = null
  dom.window.fetch = async (url, options) => {
    if (String(url).includes('/delete')) hit = {url, options}
    return {ok: true, status: 200, json: async () => ({success: true, users: [], logs: []})} }
  const context = evaluate(read('static/admin.js'), dom, 'static/admin.js')
  context.renderAdminUsers([MEMBER])
  const card = dom.window.document.querySelector('[data-user-id="member-1"]')
  const asked = []
  let round = 0
  dom.window.prompt = message => { asked.push(message); return round++ === 0 ? '小淘气' : '离职交接完毕' }
  await context.runUserAction(card, 'delete')
  assert.ok(hit, '应该发出请求')
  assert.match(hit.url, /\/api\/admin\/users\/member-1\/delete$/)
  assert.equal(hit.options.method, 'POST')
  assert.equal(JSON.parse(hit.options.body).reason, '离职交接完毕')
  assert.match(asked[0], /不可恢复/, '第一句就要把「不可恢复」说清楚')
  assert.match(asked[0], /小淘气/, '要显示目标是谁')
  await settle()
  dom.window.close()
})

test('原因留空就不发请求', async () => {
  const dom = adminDom('super_admin')
  let called = 0
  dom.window.fetch = async url => { if (String(url).includes('/delete')) called += 1
    return {ok: true, status: 200, json: async () => ({success: true, users: [], logs: []})} }
  const context = evaluate(read('static/admin.js'), dom, 'static/admin.js')
  context.renderAdminUsers([MEMBER])
  const card = dom.window.document.querySelector('[data-user-id="member-1"]')
  let round = 0
  dom.window.prompt = () => (round++ === 0 ? '小淘气' : '   ')
  await context.runUserAction(card, 'delete')
  assert.equal(called, 0)
  await settle()
  dom.window.close()
})
