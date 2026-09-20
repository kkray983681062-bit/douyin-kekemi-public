function adminEscape(value) {
  const div = document.createElement('div')
  div.textContent = String(value ?? '')
  return div.innerHTML
}

function adminCookie(name) {
  const prefix = encodeURIComponent(name) + '='
  const item = document.cookie.split(';').map(value => value.trim())
    .find(value => value.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : ''
}

async function adminFetch(url, options = {}) {
  const method = String(options.method || 'GET').toUpperCase()
  const headers = Object.assign({}, options.headers || {})
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    headers['X-CSRF-Token'] = adminCookie('kekemi_csrf')
  }
  const response = await fetch(url, Object.assign({}, options, {method, headers}))
  if (response.status === 401) {
    window.location.assign('/login')
    throw new Error('需要重新登录')
  }
  const data = await response.json().catch(() => ({}))
  if (!response.ok || data.success === false) {
    throw new Error(data.error || '请求失败')
  }
  return data
}

function setAdminMessage(message, kind = '') {
  const element = document.getElementById('adminMessage')
  if (!element) return
  element.textContent = message || ''
  element.className = 'form-message' + (kind ? ' ' + kind : '')
}

function statusText(status) {
  return ({pending: '待审核', active: '已启用', rejected: '已驳回', suspended: '已暂停'})[status] || status
}

function userActions(user, isSuper) {
  const buttons = []
  if (user.status === 'pending') {
    buttons.push('<button data-action="approve">通过</button>')
    buttons.push('<button class="danger" data-action="reject">驳回</button>')
  }
  if (user.status === 'active') buttons.push('<button class="danger" data-action="suspend">暂停</button>')
  if (user.status === 'suspended') buttons.push('<button data-action="restore">恢复</button>')
  if (isSuper) buttons.push('<button data-action="note">备注</button>')
  if (isSuper && user.role !== 'super_admin') {
    buttons.push('<button data-action="role">修改角色</button>')
    buttons.push('<button class="danger" data-action="revoke-sessions">强制下线</button>')
    // 删超管不给入口：后端要留住最后一个超管，前端不做出「点了就能删」的暗示。
    // 这不是安全边界——真正的判定在 public.admin_delete_user 里。
    buttons.push('<button class="danger" data-action="delete">删除账号</button>')
  }
  return buttons.join('')
}

function renderAdminUsers(users, notes = {}) {
  const list = document.getElementById('userList')
  if (!list) return
  const isSuper = document.body.dataset.role === 'super_admin'
  if (!users.length) {
    list.innerHTML = '<div class="empty-admin">当前筛选条件下没有用户</div>'
    return
  }
  list.innerHTML = users.map(user => {
    const note = notes[user.id] || ''
    return `
    <article class="user-card" data-user-id="${adminEscape(user.id)}" data-status="${adminEscape(user.status)}" data-role="${adminEscape(user.role)}" data-note="${adminEscape(note)}" data-username="${adminEscape(user.username)}" data-email="${adminEscape(user.email || '')}">
      <div class="user-identity">
        <strong>${adminEscape(user.username)}</strong>
        <span>${adminEscape(user.email || '')}</span>
      </div>
      <div class="user-meta">
        <span class="status-chip ${adminEscape(user.status)}">${adminEscape(statusText(user.status))}</span>
        <span>${adminEscape(user.role)}</span>
        ${user.status_reason ? `<span title="${adminEscape(user.status_reason)}">${adminEscape(user.status_reason)}</span>` : ''}
        ${note ? `<span class="user-note">${adminEscape(note)}</span>` : ''}
      </div>
      <div class="user-actions">${userActions(user, isSuper)}</div>
    </article>`
  }).join('')
}

async function loadAdminUsers() {
  const filter = document.getElementById('statusFilter')
  const status = filter?.value || ''
  const isSuper = document.body.dataset.role === 'super_admin'
  const [data, notesData] = await Promise.all([
    adminFetch('/api/admin/users' + (status ? '?status=' + encodeURIComponent(status) : '')),
    isSuper
      ? adminFetch('/api/admin/user-notes').catch(() => ({notes: {}}))
      : Promise.resolve({notes: {}}),
  ])
  renderAdminUsers(data.users || [], notesData.notes || {})
  const count = document.getElementById('userCount')
  if (count) count.textContent = `共 ${Number(data.count || 0)} 位用户`
}

function formatAuditTime(value) {
  if (!value) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  }).format(new Date(value))
}

async function loadAuditLogs() {
  const list = document.getElementById('auditList')
  if (!list) return
  const data = await adminFetch('/api/admin/audit-logs?limit=100')
  const logs = data.logs || []
  list.innerHTML = logs.length ? logs.map(log => `
    <div class="audit-row">
      <span>${adminEscape(formatAuditTime(log.created_at))}</span>
      <strong>${adminEscape(log.action)} · ${adminEscape(log.target_user_id || '')}</strong>
      <span>${adminEscape(log.reason || '')}</span>
    </div>
  `).join('') : '<div class="empty-admin">暂无操作记录</div>'
}

async function runUserAction(card, action) {
  const userId = card.dataset.userId
  const expectedStatus = card.dataset.status
  let payload = {expected_status: expectedStatus, reason: ''}
  if (action === 'role') {
    const role = window.prompt('输入新角色：member 或 admin', card.dataset.role === 'member' ? 'admin' : 'member')
    if (!role) return
    const reason = window.prompt('填写角色修改原因')
    if (!reason?.trim()) return
    payload = {role: role.trim(), reason: reason.trim()}
  } else if (action === 'revoke-sessions') {
    const reason = window.prompt('填写强制下线原因')
    if (!reason?.trim()) return
    payload = {reason: reason.trim()}
  } else if (action === 'delete') {
    // 硬删除，不可恢复。这一行按钮挨得近，点错的代价没法撤销，
    // 所以确认方式是「手打目标用户名」，不是点一下「确定」。
    const username = card.dataset.username || ''
    const typed = window.prompt(
      `删除账号不可恢复。

目标：${username}（${card.dataset.email || ''}）
` +
      `他的登录、资料、会话会一起清空，用户名和邮箱会被释放。

` +
      `确认请手动输入用户名：${username}`)
    if (typed === null) return
    if (typed.trim() !== username) {
      setAdminMessage('用户名不一致，已取消删除', 'error')
      return
    }
    const reason = window.prompt('填写删除原因')
    if (!reason?.trim()) return
    payload = {reason: reason.trim()}
  } else if (action === 'note') {
    const note = window.prompt('填写备注（留空清除）', card.dataset.note || '')
    if (note === null) return
    payload = {note}
  } else if (action !== 'approve') {
    const reason = window.prompt('填写操作原因')
    if (!reason?.trim()) return
    payload.reason = reason.trim()
  }
  await adminFetch(`/api/admin/users/${encodeURIComponent(userId)}/${action}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  })
  setAdminMessage('操作已保存', 'success')
  await Promise.all([loadAdminUsers(), loadAuditLogs()])
}

function initAdminPage() {
  if (document.documentElement.dataset.adminInitialized === '1') return
  document.documentElement.dataset.adminInitialized = '1'
  document.getElementById('statusFilter')?.addEventListener('change', () => {
    loadAdminUsers().catch(error => setAdminMessage(error.message, 'error'))
  })
  document.getElementById('refreshAudit')?.addEventListener('click', () => {
    loadAuditLogs().catch(error => setAdminMessage(error.message, 'error'))
  })
  document.getElementById('userList')?.addEventListener('click', event => {
    const button = event.target.closest('button[data-action]')
    const card = button?.closest('[data-user-id]')
    if (!button || !card) return
    runUserAction(card, button.dataset.action).catch(error => setAdminMessage(error.message, 'error'))
  })
  document.getElementById('adminLogout')?.addEventListener('click', async () => {
    await adminFetch('/api/auth/logout', {method: 'POST'})
    window.location.assign('/login')
  })
  Promise.all([loadAdminUsers(), loadAuditLogs()])
    .catch(error => setAdminMessage(error.message, 'error'))
}

document.addEventListener('DOMContentLoaded', initAdminPage)
