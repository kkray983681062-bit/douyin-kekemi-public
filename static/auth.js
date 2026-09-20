function readCookie(name) {
  const prefix = encodeURIComponent(name) + '='
  const item = document.cookie.split(';').map(value => value.trim())
    .find(value => value.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : ''
}

async function authApiFetch(url, options = {}) {
  const method = String(options.method || 'GET').toUpperCase()
  const headers = Object.assign({}, options.headers || {})
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = readCookie('kekemi_csrf')
    if (csrf) headers['X-CSRF-Token'] = csrf
  }
  return fetch(url, Object.assign({}, options, {method, headers}))
}

function setFormMessage(message, kind = '') {
  const element = document.getElementById('formMessage')
  if (!element) return
  element.textContent = message || ''
  element.className = 'form-message' + (kind ? ' ' + kind : '')
}

function formPayload(form) {
  return Object.fromEntries(new FormData(form).entries())
}

async function submitJson(form, url, payload, button) {
  if (button) button.disabled = true
  setFormMessage('正在处理…')
  try {
    const response = await authApiFetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok || data.success === false) {
      throw new Error(data.error || '请求失败，请稍后重试')
    }
    setFormMessage(data.message || '操作成功', 'success')
    if (data.next_url) window.location.assign(data.next_url)
    return data
  } catch (error) {
    setFormMessage(error.message || '请求失败，请稍后重试', 'error')
    return null
  } finally {
    if (button) button.disabled = false
  }
}

let recoveryAccessToken = ''

function initLoginPage() {
  const loginForm = document.getElementById('loginForm')
  const forgotForm = document.getElementById('forgotForm')
  const forgotToggle = document.getElementById('forgotToggle')
  if (loginForm) {
    loginForm.addEventListener('submit', event => {
      event.preventDefault()
      const payload = formPayload(loginForm)
      submitJson(loginForm, '/api/auth/login', payload, loginForm.querySelector('button[type="submit"]'))
    })
  }
  if (forgotToggle && forgotForm) {
    forgotToggle.addEventListener('click', () => {
      forgotForm.hidden = !forgotForm.hidden
      if (!forgotForm.hidden) forgotForm.querySelector('input')?.focus()
    })
  }
  if (forgotForm) {
    forgotForm.addEventListener('submit', event => {
      event.preventDefault()
      submitJson(forgotForm, '/api/auth/forgot-password', formPayload(forgotForm), forgotForm.querySelector('button'))
    })
  }
}

function initRegisterPage() {
  const form = document.getElementById('registerForm')
  if (!form) return
  form.addEventListener('submit', event => {
    event.preventDefault()
    submitJson(form, '/api/auth/register', formPayload(form), form.querySelector('button'))
  })
}

function initStatusPage() {
  const logout = document.getElementById('logoutButton')
  const resubmit = document.getElementById('resubmitButton')
  if (logout) {
    logout.addEventListener('click', async () => {
      const data = await submitJson(null, '/api/auth/logout', {}, logout)
      if (data && !data.next_url) window.location.assign('/login')
    })
  }
  if (resubmit) {
    resubmit.addEventListener('click', async () => {
      const data = await submitJson(null, '/api/auth/resubmit', {}, resubmit)
      if (data) window.location.reload()
    })
  }
}

function initResetPage() {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
  recoveryAccessToken = params.get('access_token') || ''
  if (window.location.hash) {
    window.history.replaceState(null, '', window.location.pathname + window.location.search)
  }
  const form = document.getElementById('resetForm')
  if (!form) return
  if (!recoveryAccessToken) {
    setFormMessage('重置链接无效或已过期，请重新申请。', 'error')
  }
  form.addEventListener('submit', event => {
    event.preventDefault()
    const payload = formPayload(form)
    if (payload.password !== payload.password_confirm) {
      setFormMessage('两次输入的密码不一致', 'error')
      return
    }
    submitJson(form, '/api/auth/reset-password', {
      recovery_access_token: recoveryAccessToken,
      password: payload.password
    }, form.querySelector('button'))
  })
}

function initAuthPage() {
  if (document.documentElement.dataset.authInitialized === '1') return
  document.documentElement.dataset.authInitialized = '1'
  const page = document.body?.dataset.page
  if (page === 'login') initLoginPage()
  if (page === 'register') initRegisterPage()
  if (page === 'status') initStatusPage()
  if (page === 'reset') initResetPage()
}

document.addEventListener('DOMContentLoaded', initAuthPage)
