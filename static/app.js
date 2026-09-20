let eventSources = {}       // room_id -> EventSource
const currentRooms = {}     // room_id -> {nickname, is_temporary, douyin_id}
const mysteries = {}        // sec_uid -> {display, real_name, ..., room_id, room_nickname}
const feedByRoom = {}        // room_id -> 最近500条公屏事件
const feedViewStateByRoom = {}  // room_id -> {frozen, unseenCount}：上翻冻结阅读状态
const FEED_MAX_ITEMS = 300   // 公屏缓存与 DOM 的上限（手机端 500 个节点偏重）
const FEED_BOTTOM_GAP = 60   // 距底部这么多像素以内算“跟随最新”
let feedLoadSeq = 0          // 公屏切房请求序号，防止旧房响应覆盖新房
let disconnectTimers = {}   // room_id -> timer
let recordAllEnabled = false  // 是否记录全部用户
let currentView = 'mystery'   // mystery / all / feed / daily / hostWeekly / visitorWeekly / giftLibrary / giftAssignments / silence
// 只能由 switchMode 改：面板显隐和按钮状态都归它管，绕过去就会出现
// 「标题变了、下面还是上一个视图」。
let lastRoomId = null         // 最近监听的房间，用于按钮切换停止
let historyCache = []          // 搜索历史缓存（预加载零延迟）
let emojiMap = {}              // 抖音表情映射：名字 -> {file, emoji}
let selectedRoomId = null      // 当前选中的房间ID，null=显示全部
let dataPanelCollapsed = false // 只控制本页内容可见性，不暂停监听或重建数据 DOM

function appCookie(name) {
  const prefix = encodeURIComponent(name) + '='
  const match = document.cookie.split(';').map(item => item.trim()).find(item => item.startsWith(prefix))
  if (!match) return ''
  try {
    return decodeURIComponent(match.slice(prefix.length))
  } catch (_) {
    return ''
  }
}

async function apiFetch(url, options = {}) {
  const method = String(options.method || 'GET').toUpperCase()
  const headers = Object.assign({}, options.headers || {})
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = appCookie('kekemi_csrf')
    if (csrf) headers['X-CSRF-Token'] = csrf
  }
  const response = await window.fetch(url, Object.assign({}, options, {method, headers}))
  if (response.status === 401 && window.KEKEMI_AUTH?.enabled) {
    window.location.assign('/login')
  } else if (response.status === 403) {
    showToast('❌ 无权限执行此操作')
  }
  return response
}

// 所有现有请求统一经过认证包装器，写操作自动携带 CSRF。
const fetch = apiFetch

async function logoutAccount() {
  const response = await apiFetch('/api/auth/logout', {method: 'POST'})
  if (response.ok) window.location.assign('/login')
}

const HTML_ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}
// 纯字符串替换：原来每调一次就建一个临时 DOM 节点，渲染整屏时开销可观。
// 顺带把引号也转义掉——本函数的结果同时用在 HTML 属性里。
function escapeHtml(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/[&<>"']/g, ch => HTML_ESCAPES[ch])
}

// ========== 抖音表情渲染 ==========
function loadEmojiMap() {
  fetch('/api/emoji_map')
    .then(r => r.json())
    .then(data => {
      if (data.success && data.map) emojiMap = data.map
    })
    .catch(() => {})
}

function renderEmoji(text) {
  if (!text || !emojiMap) return text
  return text.replace(/\[([^\]]+)\]/g, function(match, name) {
    const em = emojiMap[name]
    if (em && em.file) {
      return '<img src="/static/emoji/' + em.file + '" class="dy-emoji" alt="[' + name + ']" title="[' + name + ']">'
    }
    if (em && em.emoji) {
      return em.emoji
    }
    return match  // 找不到就不换
  })
}

// 消费等级徽章。口径是「最近观察到的等级」；0 或未知不显示，绝不出现 Lv.0。
// 分四档配色（自定义样式，不使用抖音官方徽章素材）。
function levelBadgeHtml(level) {
  const value = Number(level) || 0
  if (value <= 0) return ''
  const tier = value >= 46 ? 4 : value >= 31 ? 3 : value >= 16 ? 2 : 1
  return `<span class="lv-badge lv-t${tier}">Lv.${value}</span>`
}

function setStatus(text, color) {
  document.getElementById('statusText').textContent = text
  document.getElementById('dot').className = 'dot ' + color
}

const DATA_PANEL_VIEW_LABELS = {
  mystery: '🎯 神秘人',
  all: '📋 全部',
  feed: '🖥 公屏',
  daily: '📊 日榜',
  hostWeekly: '🎤 主持周榜',
  visitorWeekly: '💎 游客周榜',
  giftLibrary: '🧰 礼物库',
  giftAssignments: '📝 归属修正',
  silence: '🔕 静默记录'
}

function updateDataPanelTitle(view = currentView) {
  const title = document.getElementById('dataPanelTitle')
  if (!title) return
  const label = DATA_PANEL_VIEW_LABELS[view] || '数据'
  const roomName = selectedRoomId ? (currentRooms[selectedRoomId]?.nickname || selectedRoomId) : ''
  title.textContent = roomName ? `${label} · ${roomName}` : label
}

function applyDataPanelState() {
  const panel = document.getElementById('dataPanel')
  const button = document.getElementById('dataPanelToggle')
  if (!panel || !button) return
  panel.classList.toggle('is-collapsed', dataPanelCollapsed)
  button.textContent = dataPanelCollapsed ? '展开 ↓' : '收起 ↑'
  button.setAttribute('aria-expanded', dataPanelCollapsed ? 'false' : 'true')
  updateDataPanelTitle()
}

function toggleDataPanel() {
  dataPanelCollapsed = !dataPanelCollapsed
  applyDataPanelState()
}

function bindDataPanelControls() {
  const button = document.getElementById('dataPanelToggle')
  if (!button || button.dataset.bound === '1') return
  button.dataset.bound = '1'
  button.addEventListener('click', toggleDataPanel)
  applyDataPanelState()
}

function resetBtnText() {
  const btn = document.getElementById('btn')
  const inputEl = document.getElementById('input')
  if (!btn || !inputEl) return
  const rooms = Object.keys(currentRooms)
  const input = inputEl.value.trim()
  if (rooms.length > 0 && !input) {
    btn.textContent = '停止'
    btn.className = 'stop-btn'
  } else {
    btn.textContent = '🔍 监听'
    btn.className = ''
  }
}

// 解析失败的收尾。以前每条失败分支只复位按钮，开头那句
// setStatus('解析中...') 从没被复位，用户就一直看着「解析中」；
// 而 showToast 只在神秘人列表为空时才往 #events 写，错误提示也大概率
// 看不见——两个凑一起，失败看起来就像卡死。原因必须落到状态栏上。
function resolveFailed(message) {
  showToast('❌ ' + message)
  setStatus(message, 'red')
  const btn = document.getElementById('btn')
  if (btn) btn.disabled = false
  resetBtnText()
}

// 与后端 _looks_like_douyin_id 同一套判断：字母数字下划线点连字符，
// 不含斜杠、不是 http 开头、不是 douyin.com 裸域名。
function looksLikeDouyinId(text) {
  const value = String(text || '')
  if (!value || value.startsWith('http')) return false
  if (value.includes('/') || value.toLowerCase().includes('douyin.com')) return false
  return /^[A-Za-z0-9._-]{1,64}$/.test(value)
}

function connect() {
  const inputEl = document.getElementById('input')
  const btn = document.getElementById('btn')
  if (!inputEl || !btn) return
  const input = inputEl.value.trim()
  if (!input) return
  btn.disabled = true
  btn.textContent = '解析中...'
  setStatus('解析中...', 'gray')

  fetch('/api/resolve', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({input: input})
  })
  .then(r => r.json())
  .then(data => {
    if (!data.success) {
      resolveFailed(data.error || '解析失败')
      return
    }
    if (data.room_id && (data.live_status == 1 || data.live_status === undefined)) {
      if (currentRooms[data.room_id]) {
        resolveFailed('已在监听该直播间')
        return
      }
      // 输入本身是抖音号时把它当 douyin_id 传下去；输入是链接就留空，
      // 别把链接当成号存进去。
      startListening(data.room_id, data.nickname || '', {
        sec_uid: data.sec_uid || '',
        anchor_id: data.anchor_id || '',
        douyin_id: looksLikeDouyinId(input) ? input : ''
      })
      // 保存搜索历史
      const saveInput = input
      const saveNickname = data.nickname || ''
      const saveRoomId = data.room_id
      fetch('/api/search_history/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({input: saveInput, nickname: saveNickname, room_id: saveRoomId})
      }).then(r => r.json()).then(res => {
        if (res.success) {
          // 本地缓存同步更新
          historyCache = historyCache.filter(item => item.input_text !== saveInput)
          historyCache.unshift({input_text: saveInput, nickname: saveNickname, room_id: saveRoomId, created_at: Math.floor(Date.now()/1000)})
          if (historyCache.length > 20) historyCache = historyCache.slice(0, 20)
        }
      }).catch(() => {})
    } else if (data.room_id && data.live_status == 0) {
      resolveFailed('该主播未在直播')
    } else {
      resolveFailed('无法获取直播间信息')
    }
  })
  .catch(err => {
    resolveFailed('网络错误: ' + err.message)
  })
}

// ========== 搜索历史 ==========

function loadRoomHistory() {
  const dd = document.getElementById('historyDropdown')
  if (!dd) return
  renderHistoryFromCache()
  // 后台刷新缓存，供下一次 focus
  refreshHistoryCache()
}

// 下拉列的是「后台在监听、但被我收起来」的厅，点一下加回页面。
// 房间 ID 与昵称都是外部数据，只能进 data-*，绝不拼进 onclick——
// 属性里的实体会被浏览器解码后当 JS 解析，一个引号就能注入。
function renderHistoryFromCache() {
  const dd = document.getElementById('historyDropdown')
  if (!dd) return
  const addable = addableRoomIds()
  if (addable.length === 0) {
    dd.innerHTML = '<div class="empty-msg">所有直播间都已显示</div>'
    dd.style.display = 'block'
    return
  }
  let html = ''
  addable.forEach(rid => {
    const nick = currentRooms[rid]?.nickname || rid
    html += `<div class="item" data-rid="${escapeHtml(rid)}">`
    html += `<span class="name">🎙 ${escapeHtml(nick)}</span>`
    html += `<span class="add-btn">+ 显示</span>`
    html += `</div>`
  })
  dd.innerHTML = html
  dd.style.display = 'block'
}

function refreshHistoryCache() {
  fetch('/api/search_history/list')
    .then(r => r.json())
    .then(res => {
      if (res.success && res.data) historyCache = res.data
    })
    .catch(() => {})
}

function removeRoomHistory(input) {
  // 本地同步删除
  historyCache = historyCache.filter(item => item.input_text !== input)
  renderHistoryFromCache()
  // 服务端异步删除
  fetch('/api/search_history/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({input: input})
  })
  .then(r => r.json())
  .catch(() => {})
}

function selectHistory(input, nickname) {
  const inputEl = document.getElementById('input')
  const dropdown = document.getElementById('historyDropdown')
  if (!inputEl || !dropdown) return
  inputEl.value = input
  dropdown.style.display = 'none'
  connect()
}

function handleBtnClick() {
  const btn = document.getElementById('btn')
  const inputEl = document.getElementById('input')
  if (!btn || !inputEl) return
  const rooms = Object.keys(currentRooms)
  // 有输入内容 → 监听模式
  const input = inputEl.value.trim()
  if (input) {
    connect()
    return
  }
  // 无输入内容 + 有房间 → 停止模式
  if (rooms.length === 1) {
    stopRoom(rooms[0])
    btn.textContent = '🔍 监听'
    btn.className = ''
    lastRoomId = null
  } else if (rooms.length > 1) {
    showStopDialog()
  }
}

function showStopDialog() {
  const rooms = Object.keys(currentRooms)
  const roomColors = ['#fe2c55', '#5ac8fa', '#34c759']
  let html = '<div class="stop-overlay" id="stopOverlay" onclick="closeStopDialog(event)"><div class="stop-box" onclick="event.stopPropagation()">'
  html += '<h3>选择要停止的房间</h3>'
  rooms.forEach((rid, i) => {
    const nick = currentRooms[rid]?.nickname || rid.slice(0,10)
    html += `<div class="stop-item" data-rid="${escapeHtml(rid)}"><span class="stop-dot" style="background:${roomColors[i%3]}"></span>${escapeHtml(nick)}<span style="margin-left:auto;color:#fe2c55;font-weight:600">停止</span></div>`
  })
  html += '<div class="stop-all-item" onclick="stopAllAndClose()">全部停止</div>'
  html += '<div class="stop-cancel" onclick="closeStopDialog()">取消</div>'
  html += '</div></div>'
  document.body.insertAdjacentHTML('beforeend', html)
  const overlay = document.getElementById('stopOverlay')
  if (overlay) {
    overlay.addEventListener('click', event => {
      const item = event.target.closest('.stop-item')
      if (item) stopRoomAndClose(item.dataset.rid)
    })
  }
}

function closeStopDialog(e) {
  const el = document.getElementById('stopOverlay')
  if (el) el.remove()
}

function stopRoomAndClose(roomId) {
  closeStopDialog()
  stopRoom(roomId)
  const rooms = Object.keys(currentRooms)
  const btn = document.getElementById('btn')
  if (btn && rooms.length === 0) {
    btn.textContent = '🔍 监听'
    btn.className = ''
    lastRoomId = null
  }
}

function stopAllAndClose() {
  closeStopDialog()
  stopAll()
  const btn = document.getElementById('btn')
  if (btn) {
    btn.textContent = '🔍 监听'
    btn.className = ''
  }
  lastRoomId = null
}

function toggleRecordAll() {
  // 已废弃，由 switchMode 替代
}
function switchView(view) {
  // 已废弃，由 switchMode 替代
}

function switchMode(mode) {
  updateDataPanelTitle(mode)
  const allBtn = document.getElementById('modeAll')
  const feedBtn = document.getElementById('modeFeed')
  const dailyBtn = document.getElementById('modeDaily')
  const hostWeeklyBtn = document.getElementById('modeHostWeekly')
  const visitorWeeklyBtn = document.getElementById('modeVisitorWeekly')
  const giftLibraryBtn = document.getElementById('modeGiftLibrary')
  const giftAssignmentsBtn = document.getElementById('modeGiftAssignments')
  // 再次点击当前模式时刷新该页。
  if (mode === 'all' && currentView === 'all') {
    renderAllRecords()
    return
  }
  if (mode === 'feed' && currentView === 'feed') {
    loadFeed()
    return
  }
  if (mode === 'daily' && currentView === 'daily') {
    renderDailyRanks()
    return
  }
  if ((mode === 'hostWeekly' || mode === 'visitorWeekly') && currentView === mode) {
    renderWeeklyRanks(mode)
    return
  }
  if (mode === 'giftLibrary' && currentView === 'giftLibrary') {
    renderGiftLibrary()
    return
  }
  if (mode === 'giftAssignments' && currentView === 'giftAssignments') {
    renderGiftAssignments()
    return
  }
  if (mode === 'silence' && currentView === 'silence') {
    renderSilenceRecords(analyticsRoomId())
    return
  }
  currentView = mode
  const isAll = mode === 'all'
  const isFeed = mode === 'feed'
  const isDaily = mode === 'daily'
  const isHostWeekly = mode === 'hostWeekly'
  const isVisitorWeekly = mode === 'visitorWeekly'
  const isGiftLibrary = mode === 'giftLibrary'
  const isGiftAssignments = mode === 'giftAssignments'
  // 更新按钮状态
  document.getElementById('modeMystery').className = 'mode-btn' + (mode === 'mystery' ? ' active' : '')
  allBtn.className = 'mode-btn' + (isAll ? ' active' : '')
  if (feedBtn) feedBtn.className = 'mode-btn' + (isFeed ? ' active' : '')
  if (dailyBtn) dailyBtn.className = 'mode-btn' + (isDaily ? ' active' : '')
  if (hostWeeklyBtn) hostWeeklyBtn.className = 'mode-btn admin-mode' + (isHostWeekly ? ' active' : '')
  if (visitorWeeklyBtn) visitorWeeklyBtn.className = 'mode-btn admin-mode' + (isVisitorWeekly ? ' active' : '')
  if (giftLibraryBtn) giftLibraryBtn.className = 'mode-btn admin-mode' + (isGiftLibrary ? ' active' : '')
  if (giftAssignmentsBtn) giftAssignmentsBtn.className = 'mode-btn admin-mode' + (isGiftAssignments ? ' active' : '')
  allBtn.textContent = isAll ? '刷新' : '📋全部'
  // 切换容器显示
  const eventsEl = document.getElementById('events')
  // 浏览器可能还缓存着没有 feedPane 的旧页面，此时退回直接控制容器，别让公屏整个不显示。
  const feedEl = document.getElementById('feedPane') || document.getElementById('feedContainer')
  if (isFeed) {
    eventsEl.style.display = 'none'
    if (feedEl) feedEl.style.display = ''
    loadFeed()
    return
  } else {
    eventsEl.style.display = ''
    if (feedEl) feedEl.style.display = 'none'
    updateFeedNewMessageButton()
  }
  // 通知后端：全部用户模式才开启录制
  const canManageListener = ['admin', 'super_admin'].includes(window.KEKEMI_AUTH?.role)
  if (isAll && canManageListener) {
    fetch('/api/toggle_record_all', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: true})
    })
  }
  if (isAll) {
    renderAllRecords()
  } else if (isDaily) {
    renderDailyRanks()
  } else if (isHostWeekly || isVisitorWeekly) {
    renderWeeklyRanks(mode)
  } else if (isGiftLibrary) {
    renderGiftLibrary()
  } else if (mode === 'silence') {
    // 静默记录没有自己的 tab 按钮（入口在铃铛面板里），但面板显隐、
    // 按钮取消高亮这些事必须跟其它视图走同一条路——绕过 switchMode
    // 就会出现「标题变成静默记录、下面还是公屏」。
    renderSilenceRecords(analyticsRoomId())
  } else if (isGiftAssignments) {
    renderGiftAssignments()
  } else {
    renderMysteries()
  }
}

// ========== 房间筛选 ==========

function selectRoom(roomId) {
  resetDailyRankPaging()   // 换厅回到只看前几名，别把上个厅的展开状态带过来
  resetWeeklyRankPaging()
  // 切厅即回到该厅最新画面：冻结状态和未读数不跨厅携带。
  if (selectedRoomId !== roomId) resetFeedViewState(roomId)
  selectedRoomId = roomId
  updateDataPanelTitle()
  updateRoomSelector()
  // 重渲染当前tab
  if (currentView === 'mystery') renderMysteries()
  else if (currentView === 'all') renderAllRecords()
  else if (currentView === 'feed') loadFeed()
  else if (currentView === 'daily') renderDailyRanks()
  else if (currentView === 'hostWeekly' || currentView === 'visitorWeekly') renderWeeklyRanks(currentView)
  else if (currentView === 'giftLibrary') renderGiftLibrary()
  else if (currentView === 'giftAssignments') renderGiftAssignments()
  else if (currentView === 'silence') renderSilenceRecords(roomId)
}


// ========== 个人视图：在自己页面上收起某个厅 ==========
// 只改本地显示，绝不碰后台监听。默认那几个厅是所有人共用的，
// 真停掉会让所有人当场断流，且停掉期间的互动记录事后补不回来。
// 状态存浏览器本地，因此换设备或清缓存会恢复成「全部显示」。
const HIDDEN_ROOMS_KEY = 'kekemi.hiddenRooms'

function hiddenRoomIds() {
  try {
    const raw = localStorage.getItem(HIDDEN_ROOMS_KEY)
    return new Set(raw ? JSON.parse(raw) : [])
  } catch (e) {
    return new Set()
  }
}

function saveHiddenRoomIds(ids) {
  try {
    localStorage.setItem(HIDDEN_ROOMS_KEY, JSON.stringify(Array.from(ids)))
  } catch (e) {
    console.warn('[ROOM] 无法保存个人视图:', e)
  }
}

// 当前该显示在房间栏里的厅：在监听中、且没被自己收起来。
function visibleRoomIds() {
  const hidden = hiddenRoomIds()
  return Object.keys(currentRooms).filter(rid => !hidden.has(rid))
}

// 被自己收起、但后台仍在监听的厅——下拉里列出来供加回。
function addableRoomIds() {
  const hidden = hiddenRoomIds()
  return Object.keys(currentRooms).filter(rid => hidden.has(rid))
}

function stopTemporaryRoom(roomId) {
  // 停监听是全局动作：别人正在看也会断，而且停掉期间的记录事后补不回来，
  // 所以这里挡一下，不像「收起」那样点了就走。
  const name = currentRooms[roomId]?.nickname || roomId
  if (!window.confirm(`停止监听「${name}」？停掉期间的互动记录补不回来。`)) return
  stopRoom(roomId)
}

function hideRoom(roomId) {
  const hidden = hiddenRoomIds()
  hidden.add(roomId)
  saveHiddenRoomIds(hidden)
  // 收起的正好是当前选中的厅时，切到另一个还看得见的。
  if (selectedRoomId === roomId) {
    const remaining = visibleRoomIds()
    if (remaining.length > 0) {
      selectRoom(remaining[0])
    } else {
      selectedRoomId = null
    }
  }
  updateRoomSelector()
  renderHistoryFromCache()
}

function showRoom(roomId) {
  const hidden = hiddenRoomIds()
  hidden.delete(roomId)
  saveHiddenRoomIds(hidden)
  updateRoomSelector()
  renderHistoryFromCache()
}

let silenceWatches = []          // 我订阅的抖音号
let silenceAlerts = []           // 当前未读的告警（已恢复发言的后端就不返回了）
let silenceUnreadByHall = {}     // 抖音号 -> 未读条数，铃铛角标用
let silencePanelHall = ''        // 面板正开在哪个厅，'' = 没开
let silenceRecordsLoadSeq = 0     // 静默记录请求序号：防止慢响应把旧厅证据挂到新厅名下

// 已弹过通知的 alert id：必须落 localStorage，不能只是内存里的 Set——
// 内存版一刷新页面就清空，几十条已经弹过的旧告警（尤其是 Fix A 之前遗留
// 的陈旧告警）会在每次刷新时被当成新的重新弹一遍系统通知。
const SILENCE_NOTIFIED_KEY = 'kekemi.silenceNotifiedIds'
// 兜底上限：正常情况下这个集合已经被「按最新响应剪掉」限制住了，
// 这里只防极端情况下无限增长把 localStorage 写爆。
const SILENCE_NOTIFIED_CAP = 200

// localStorage 读写失败时的内存兜底。踩过的坑：只在 loadNotifiedIds 的
// catch 分支里退回内存是不够的——getItem 和 setItem 是两次独立的调用，
// 可能只有其中一个失败。setItem 一直失败但 getItem 能读的情况下，
// getItem 读到的永远是「从没成功写过」的空值，不查这个兜底的话每次都会
// 判定成没弹过，重复通知照样发生，跟只写 catch 分支时是同一个后果。
// 所以两个函数都要读写这个变量，不能只在异常分支里露一面。
let silenceNotifiedFallback = new Set()

function loadNotifiedIds() {
  let stored = []
  try {
    const raw = localStorage.getItem(SILENCE_NOTIFIED_KEY)
    stored = raw ? JSON.parse(raw) : []
  } catch (e) {
    // getItem 本身就不可用：下面会用内存兜底补上
  }
  return new Set([...stored, ...silenceNotifiedFallback])
}

function saveNotifiedIds(ids) {
  silenceNotifiedFallback = new Set(ids)
  try {
    localStorage.setItem(SILENCE_NOTIFIED_KEY, JSON.stringify(Array.from(ids)))
  } catch (e) {
    // 存不进去（隐私模式/配额满/环境没有 localStorage）：上面那行已经
    // 把当前状态存进内存兜底了，本次页面存活期间的去重不受影响，
    // 只是刷新后不持久——localStorage 能用的话前面的 setItem 已经
    // 顺带写过一份，这里不用再重复处理。
  }
}

// 与 static/app.js:432 的既有写法保持一致
function isAdminUser() {
  return ['admin', 'super_admin'].includes(window.KEKEMI_AUTH?.role)
}

// 铃铛只负责「打开这个厅的面板」，订阅/退订/看记录都在面板里。
// 早先是「点一下直接订阅」，没有任何提示——探索界面时随手一点就订上了
// 自己都不知道，实跑当天就踩了。而且订阅后还要额外一个 ⊘ 做退订入口，
// 三个图标把厅按钮撑爆、房间栏溢出到滚不动。收敛成一个图标一举两得。
// 临时厅没有抖音号，无法稳定订阅，不显示。
function silenceBellHtml(douyinId, watched, isAdmin, unread) {
  if (!isAdmin || !douyinId) return ''
  const cls = watched ? 'silence-bell silence-bell-on' : 'silence-bell'
  const n = Number(unread || 0)
  // 99+ 封顶：厅按钮宽度有限，三位数会把房间栏再次挤到滚不动
  const badge = n > 0
    ? `<span class="silence-unread">${n > 99 ? '99+' : n}</span>`
    : ''
  const title = watched ? '静默监测中，点开看未读和记录' : '点开可以开启静默监测'
  return `<span class="${cls}" data-silence-id="${escapeHtml(douyinId)}" title="${title}">🔔${badge}</span>`
}

function silencePanelHtml(douyinId) {
  const watched = silenceWatches.includes(douyinId)
  const mine = silenceAlerts.filter(a => a.douyin_id === douyinId)
  const hall = (mine[0] && mine[0].hall_name) || hallNameByDouyinId(douyinId) || douyinId
  // 按人聚合：一个人连续静默 3 小时会攒十几条告警，逐条列出来又是一堵墙
  // ——那正是把主页告警条废掉的原因，别在面板里重演。每人只留**最新**那条。
  // 不能按 alert_index 取最大：分档归零之后「最大」不再等于「最新」，
  // 上一档的 1..7 会和本档的 1..3 同时在列表里，按最大挑就会显示上一档
  // 那个 7——正是这次改动要消灭的那种跨档累计数字。
  const byHost = {}
  mine.forEach(a => {
    const key = a.sec_uid || a.nickname
    if (!byHost[key] || Number(a.created_at) > Number(byHost[key].created_at)) {
      byHost[key] = a
    }
  })
  const hosts = Object.keys(byHost).map(k => byHost[k])
  const rows = hosts.length
    ? hosts.map(a =>
        `<div class="silence-panel-row">` +
        `<strong>${escapeHtml(a.nickname || '?')}</strong>` +
        `<em>已静默 ${silenceSilentForLabel(a)}　第 ${Number(a.alert_index || 0)} 次</em>` +
        `</div>`).join('')
    : `<div class="silence-panel-empty">此刻没有人在静默</div>`
  const actions = watched
    ? `<div class="silence-panel-act" data-silence-records="${escapeHtml(douyinId)}">📋 查看静默记录</div>` +
      `<div class="silence-panel-act" data-silence-toggle="${escapeHtml(douyinId)}">🔕 取消监测这个厅</div>`
    : `<div class="silence-panel-act" data-silence-toggle="${escapeHtml(douyinId)}">🔔 开始监测这个厅</div>`
  return `<div class="silence-panel-box">` +
    `<div class="silence-panel-head">${escapeHtml(hall)}` +
    `<span class="silence-panel-x" data-silence-close="1">×</span></div>` +
    rows + `<div class="silence-panel-sep"></div>` + actions + `</div>`
}

function hallNameByDouyinId(douyinId) {
  const rid = Object.keys(currentRooms).find(
    k => currentRooms[k] && currentRooms[k].douyin_id === douyinId)
  return rid ? (currentRooms[rid].nickname || '') : ''
}

// 阈值可配（SILENCE_THRESHOLD_SECONDS），别写死。告警里带的 alert_index
// 是「第几次」，每次都是又静默了一个阈值周期。
let silenceThresholdSeconds = 900

function silenceThresholdLabel() {
  return silenceDurationLabel(silenceThresholdSeconds)
}

// 这一条告警发出时，人已经静默了一个阈值周期；此后又过了多久也得算进去。
// 写死成阈值的话，静默 45 分钟的人会显示成「已静默 15 分钟」，差一个数量级。
function silenceSilentForLabel(alert) {
  const since = Number(alert.created_at || 0)
  const extra = since ? Math.max(0, Math.floor(Date.now() / 1000) - since) : 0
  return silenceDurationLabel(silenceThresholdSeconds + extra)
}

function openSilencePanel(douyinId) {
  silencePanelHall = douyinId
  const box = document.getElementById('silencePanel')
  if (box) box.innerHTML = silencePanelHtml(douyinId)
  // 打开即读完：只标这个厅的，别把别厅的未读一起清了
  const ids = silenceAlerts
    .filter(a => a.douyin_id === douyinId).map(a => Number(a.id))
  if (!ids.length) return Promise.resolve()
  return fetch('/api/admin/silence/alerts/read', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({alert_ids: ids})
  })
    .then(r => r.json())
    .then(data => {
      if (!data.success) {
        // 后端没确认就别把角标假装清了，但也不能一声不吭——用户只会
        // 看到角标不动、毫无解释。
        silenceToast('标记已读失败，稍后自动重试')
        return
      }
      silenceUnreadByHall = Object.assign({}, silenceUnreadByHall)
      delete silenceUnreadByHall[douyinId]
      updateRoomSelector()
    })
    .catch(err => silenceToast('网络错误: ' + err.message))
}

function closeSilencePanel() {
  silencePanelHall = ''
  const box = document.getElementById('silencePanel')
  if (box) box.innerHTML = ''
}

// 真正的浮层提示。不能用既有的 showToast：那个是把 #events（主内容区）
// 整个覆盖掉，而且只在神秘人列表为空时才显示——在最常见的界面状态下
// 根本看不见，等于没提示。这里只给静默这条线用，不动那 8 个既有调用点。
function silenceToast(msg) {
  const layer = document.getElementById('toastLayer')
  if (!layer) return
  const el = document.createElement('div')
  el.className = 'silence-toast'
  el.textContent = msg
  layer.appendChild(el)
  setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el) }, 2600)
}

function notifySilenceAlerts(alerts) {
  if (typeof Notification === 'undefined') return
  if (Notification.permission !== 'granted') return
  const list = alerts || []
  // 不按「本次响应里还在不在」裁剪。曾经这么做，前提是「离开了就不会
  // 回来」——这个前提不成立：接口有 LIMIT 50，一个主持静默 3 小时就攒
  // 十几条，四个人就压满窗口，老 id 会被挤出去；等某人恢复发言、他名下
  // 的告警被一次性 resolve 掉，位置空出来，被挤出去的老 id 又回到响应里，
  // 于是重复弹一遍。已读的告警现在还会在返回里留最多 24 小时，窗口更容易
  // 压满。只靠下面的封顶控制大小，不做这种裁剪。
  let notified = loadNotifiedIds()
  let changed = false
  list.forEach(alert => {
    // 同一条只弹一次。已恢复的后端已经不返回了，这里不用再判。
    if (notified.has(alert.id)) return
    notified.add(alert.id)
    changed = true
    new Notification('麦上静默提醒', {
      body: `${alert.nickname} · ${alert.hall_name} · 第 ${alert.alert_index} 次`
    })
  })
  if (notified.size > SILENCE_NOTIFIED_CAP) {
    // Set 按插入顺序迭代，slice(-CAP) 留下的是最近插入的那批，先丢最老的。
    notified = new Set(Array.from(notified).slice(-SILENCE_NOTIFIED_CAP))
    changed = true
  }
  // 没剪掉也没新增也没封顶＝内容跟上一次一模一样：这个函数挂在每 5 秒
  // 一次的轮询上，长时间没有新告警时白白重复写 localStorage 没有意义。
  if (changed) saveNotifiedIds(notified)
}

function loadSilenceAlerts() {
  if (!isAdminUser()) return Promise.resolve()
  return fetch('/api/admin/silence/alerts')
    .then(r => r.json())
    .then(data => {
      if (!data.success) return
      silenceAlerts = data.alerts || []
      if (data.threshold) silenceThresholdSeconds = Number(data.threshold)
      const nextUnread = data.unread_by_hall || {}
      // 只在角标真的变了才重建：innerHTML 一换，scrollLeft 就被浏览器
      // 夹回 0，横向滚动位置每 5 秒被清一次——正好把同一版加的滚动功能
      // 废掉。refreshRooms 早就有同样的 changed 守卫，这里照做。
      if (JSON.stringify(nextUnread) !== JSON.stringify(silenceUnreadByHall)) {
        silenceUnreadByHall = nextUnread
        updateRoomSelector()
      }
      if (silencePanelHall) {
        const box = document.getElementById('silencePanel')
        if (box) box.innerHTML = silencePanelHtml(silencePanelHall)
      }
      notifySilenceAlerts(silenceAlerts)
    })
    .catch(() => {})
}

function loadSilenceWatches() {
  if (!isAdminUser()) return Promise.resolve()
  return fetch('/api/admin/silence/watches')
    .then(r => r.json())
    .then(data => {
      if (!data.success) return
      silenceWatches = data.douyin_ids || []
      // 必须重画：铃铛的订阅态（蓝/灰、title）也是 updateRoomSelector 画的，
      // 而它现在只在角标数变化时才被轮询调用。零未读是绝大多数时间的稳态，
      // 不在这里补一次的话，刷新页面后已订阅的厅会一直显示成没订阅。
      updateRoomSelector()
    })
    .catch(() => {})
}

function toggleSilenceWatch(douyinId) {
  const turningOn = !silenceWatches.includes(douyinId)
  const next = turningOn
    ? silenceWatches.concat([douyinId])
    : silenceWatches.filter(item => item !== douyinId)
  return fetch('/api/admin/silence/watches', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({douyin_ids: next})
  })
    .then(r => r.json())
    .then(data => {
      if (!data.success) return
      silenceWatches = data.douyin_ids || []
      // 明确告诉人刚才那一下干了什么：早先点铃铛直接订阅、零反馈，
      // 结果是随手一点就订上了自己都不知道。
      const hall = hallNameByDouyinId(douyinId) || douyinId
      silenceToast(turningOn ? `已开始监测 ${hall}` : `已取消监测 ${hall}`)
      if (silencePanelHall === douyinId) {
        const box = document.getElementById('silencePanel')
        if (box) box.innerHTML = silencePanelHtml(douyinId)
      }
      updateRoomSelector()
      return loadSilenceAlerts()
    })
    .catch(err => silenceToast('网络错误: ' + err.message))
}

// 上线前写的记录，快照覆盖整天/整轮上麦，里面全是他根本不在麦上的时段，
// 翻出来是满屏「0 条」。记录行上存着 mic_since 和 created_at，按它裁掉即可：
// 不编任何数字，只是不显示无关的档。新记录本来就只有一档，裁剪对它是空操作。
// 档宽，跟后端 mic_silence.BUCKET_HOURS 对齐。改档宽时两边都要动，
// 只改一边会让前端静默裁错档。
const SILENCE_BAND_SECONDS = 2 * 3600

function clipBucketsToMic(buckets, record) {
  const list = buckets || []
  const micSince = Number(record.mic_since || 0)
  const until = Number(record.created_at || 0)
  if (!micSince || !until || list.length <= 1) return list
  return list.filter(b => {
    const start = bandEpoch(b.date, b.start)
    if (start === null) return true          // 日期解析不了就别擅自丢
    const end = start + SILENCE_BAND_SECONDS
    return end > micSince && start < until   // 与在麦区间有重叠才留
  })
}

// 「2027-01-02」+「22:00」-> epoch 秒（北京时间）。档位标签里的 24:00
// 是「当天末尾」，按 00:00 算起点没有意义，这里只会拿到 00:00-22:00。
function bandEpoch(date, start) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(date || ''))
  const h = /^(\d{2}):00$/.exec(String(start || ''))
  if (!m || !h) return null
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +h[1] - 8, 0, 0) / 1000
}

function silenceDurationLabel(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0))
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分钟`
  const h = Math.floor(m / 60)
  return m % 60 ? `${h} 小时 ${m % 60} 分钟` : `${h} 小时`
}

function silenceGapHtml(seconds, prefix) {
  return `<div class="silence-gap">—— ${escapeHtml(prefix || '')}` +
    `${prefix ? ' ' : ''}静默 ${silenceDurationLabel(seconds)} ——</div>`
}

function renderSilenceRecords(roomId) {
  const container = document.getElementById('events')
  const requestSeq = ++silenceRecordsLoadSeq
  container.innerHTML = '<div class="empty"><div class="icon">⏳</div>正在加载静默记录...</div>'
  fetch('/api/admin/silence/records/' + encodeURIComponent(roomId))
    .then(r => r.json())
    .then(data => {
      // 慢响应可能在用户已经切到别的厅之后才回来；不拦住就会把 A 厅的
      // 证据挂到 B 厅名下——比什么都不显示更糟。
      if (requestSeq !== silenceRecordsLoadSeq || currentView !== 'silence') return
      if (!data.success) throw new Error(data.error || '静默记录加载失败')
      const records = data.records || []
      if (!records.length) {
        container.innerHTML = '<div class="empty"><div class="icon">🔕</div>这个厅还没有静默记录</div>'
        return
      }
      container.innerHTML = records.map(item => {
        const snap = item.chat_snapshot || {}
        const threshold = Number(snap.gap_threshold || 900)
        let first = true
        const kept = clipBucketsToMic(snap.buckets, item)
        // 裁掉过档的记录一律不标间隔：老快照里第一条的 gap 是「从整天
        // 00:00 到这条」，裁掉前面的档之后它还挂着「上麦后」的名头，就成了
        // 一句测量过但不成立的断言。这是永久证据，宁可少显示也别显示错。
        const clipped = kept.length !== (snap.buckets || []).length
        const buckets = kept.map(b =>
          `<div class="silence-bucket"><span>${escapeHtml(b.date || '')} ${escapeHtml(b.start)}–${escapeHtml(b.end)}</span>` +
          `<em>${Number(b.count || 0)} 条</em>` +
          (b.messages || []).map(m => {
            // 第一条无论隔多久都标（那是「上麦到开口」）；中间的只标
            // 超过阈值的，不然刷屏时会冒出一堆「隔 3 秒」把人淹了。
            // gap 缺失时什么都不标：这个功能上线前写的记录没有这个字段，
            // 而 Number(undefined || 0) 是 0——那会在一份永久证据里写下
            // 一个从没测量过的数字。宁可不显示，绝不编。
            const measured = !clipped && m.gap !== undefined && m.gap !== null
            const gap = Number(m.gap)
            const mark = (measured && (first || gap >= threshold))
              ? silenceGapHtml(gap, first ? '上麦后' : '') : ''
            if (measured) first = false
            return mark +
              `<div class="silence-msg">${escapeHtml(m.at)}　${escapeHtml(m.text)}</div>`
          }).join('') + `</div>`
        ).join('')
        // 尾部无论隔多久都标：最后一句到出证据那一段，往往正是触发告警的空白。
        // 同样，老记录没有 tail_gap 就什么都不标——「整轮上麦 静默 0 秒」
        // 恰好是这份证据要证明的事情的反面。
        const tail = (clipped || snap.tail_gap === undefined || snap.tail_gap === null)
          ? ''
          : silenceGapHtml(Number(snap.tail_gap),
                           first ? '整轮上麦' : '之后到出证据')
        return `<div class="event silence-record">` +
          `<div class="silence-record-head"><strong>${escapeHtml(item.nickname)}</strong>` +
          `<span>${escapeHtml(item.beijing_date)}</span>` +
          `<em>静默 ${Number(item.alert_count)} 次</em></div>` +
          (clipped
            ? `<div class="silence-record-span">旧记录：当时的快照按整天/整轮` +
              `上麦取档，口径已变，这里只保留他确实在麦的那几档</div>`
            : (snap.mic_since && snap.until
              ? `<div class="silence-record-span">在麦 ${escapeHtml(snap.mic_since)}` +
                ` – ${escapeHtml(snap.until)}</div>`
              : '')) +
          `${buckets}${tail}</div>`
      }).join('')
    })
    .catch(error => {
      if (requestSeq !== silenceRecordsLoadSeq || currentView !== 'silence') return
      container.innerHTML = `<div class="empty"><div class="icon">⚠️</div>${escapeHtml(error.message)}</div>`
    })
}

// 房间栏本来就是 overflow-x:auto，但样式里把滚动条藏了（藏是对的，
// Windows 上横向滚动条会占掉一条高度），结果鼠标用户既看不到也拖不动，
// 厅一多就够不着右边的。竖直滚轮转成横向滚动，这是桌面上最自然的手势。
// 拖动超过这么多像素才算「拖」而不是「点」。手按下去再抬起来总会抖一两
// 像素，阈值太小的话正常点击会被当成拖动吃掉。
const ROOM_DRAG_THRESHOLD = 5

function bindRoomSelectorScroll() {
  const selector = document.getElementById('roomSelector')
  if (!selector || selector.dataset.wheelBound) return
  selector.dataset.wheelBound = '1'

  const overflows = () => selector.scrollWidth > selector.clientWidth

  selector.addEventListener('wheel', event => {
    if (event.deltaY === 0) return           // 本来就是横向滚，别插手
    if (!overflows()) return                 // 没溢出就别抢
    event.preventDefault()
    selector.scrollLeft += event.deltaY
  }, {passive: false})

  // 按住左键横着拖。横向条上这是最自然的手势，滚轮反而是补充。
  let dragging = false
  let startX = 0
  let startScroll = 0
  let moved = 0

  selector.addEventListener('mousedown', event => {
    if (event.button !== 0) return           // 只认左键
    if (!overflows()) return
    dragging = true
    startX = event.clientX
    startScroll = selector.scrollLeft
    moved = 0
    // 拖的时候别顺手把厅名选中成一片蓝
    event.preventDefault()
    selector.classList.add('room-selector-dragging')
  })

  selector.addEventListener('mousemove', event => {
    if (!dragging) return
    const delta = event.clientX - startX
    moved = Math.max(moved, Math.abs(delta))
    selector.scrollLeft = startScroll - delta
  })

  const stop = () => {
    if (!dragging) return
    dragging = false
    selector.classList.remove('room-selector-dragging')
  }
  selector.addEventListener('mouseup', stop)
  // 拖出栏外就松手：不然指针在外面移动时不会有 mousemove，
  // 等回到栏内会一下子跳一大截。
  selector.addEventListener('mouseleave', stop)

  // 拖完那一下 click 要吃掉，否则拖一下就切了厅或开了铃铛。
  // 用捕获阶段：本元素上那几个委托监听器都在冒泡阶段，捕获阶段
  // stopPropagation 能让事件根本到不了它们。
  selector.addEventListener('click', event => {
    if (moved <= ROOM_DRAG_THRESHOLD) return
    moved = 0
    event.stopPropagation()
    event.preventDefault()
  }, true)
}

function updateRoomSelector() {
  const selector = document.getElementById('roomSelector')
  const rooms = visibleRoomIds()
  const roomColors = ['#fe2c55', '#5ac8fa', '#34c759']
  const roomMap = {}; let ci = 0
  rooms.forEach(rid => { roomMap[rid] = ci++ % 3 })

  if (rooms.length === 0) {
    selector.innerHTML = ''
    updateDataPanelTitle()
    return
  }

  let html = ''
  rooms.forEach(rid => {
    const nick = currentRooms[rid]?.nickname || rid
    const shortName = nick
    const color = roomColors[roomMap[rid] % 3]
    const isActive = selectedRoomId === rid
    const borderStyle = isActive ? `box-shadow:0 0 10px ${color}50,0 0 25px ${color}20;border-color:${color}80;` : ''
    html += `<span class="room-filter-btn${isActive ? ' active' : ''}" data-rid="${escapeHtml(rid)}" style="${borderStyle}">`
    html += `<span class="room-filter-dot" style="background:${color}"></span>`
    html += `<span class="room-filter-name">${escapeHtml(shortName)}</span>`
    const douyinId = currentRooms[rid]?.douyin_id || ''
    html += silenceBellHtml(douyinId, silenceWatches.includes(douyinId),
                            isAdminUser(), silenceUnreadByHall[douyinId] || 0)
    // 临时厅（不在常驻名单里、由 super_admin 手动加的）叉掉是真停监听，
    // 默认厅叉掉只是从自己页面收起——两者行为不同，必须让人一眼看出来。
    if (currentRooms[rid]?.is_temporary) {
      html += `<span class="room-temp-tag" title="临时监听，叉掉即停止">临时</span>`
      html += `<span class="room-hide-btn room-stop-btn" data-stop-rid="${escapeHtml(rid)}" title="停止监听这个临时厅">×</span>`
    } else {
      html += `<span class="room-hide-btn" data-hide-rid="${escapeHtml(rid)}" title="从我的页面收起">×</span>`
    }
    html += `</span>`
  })
  const keepScroll = selector.scrollLeft
  selector.innerHTML = html
  // innerHTML 一换，浏览器会把 scrollLeft 夹回 0；这里原样放回去，
  // 免得用户刚滚到右边就被某次重建拉回最左。
  selector.scrollLeft = keepScroll
  bindRoomSelectorScroll()
  // 溢出时给右边缘一层渐隐，提示后面还有厅——滚动条是藏着的，
  // 不给这个提示就完全看不出还有内容。
  selector.classList.toggle(
    'room-selector-more', selector.scrollWidth > selector.clientWidth)
  updateDataPanelTitle()
}

/* ---- 头部紧凑模式 ---- */
function updateHeaderState() {
  const header = document.getElementById('headerArea')
  if (Object.keys(currentRooms).length > 0) {
    header.classList.add('monitoring')
  } else {
    header.classList.remove('monitoring')
  }
}

function showToast(msg) {
  const el = document.getElementById('events')
  // 只在没有任何神秘人时显示toast
  if (Object.keys(mysteries).length === 0) {
    el.innerHTML = `<div class="empty" style="color:#fe2c55">${msg}</div>`
  }
}

// identity 是 /api/resolve 返回的 {sec_uid, anchor_id, douyin_id}。
// 以前这里只发 room_id + nickname，解析拿到的身份转手就扔——手动加的厅
// 因此凑不齐主播身份，在线名单永远「等待首次同步」；douyin_id 也丢了，
// 厅下播重开换房间号就再也跟不上。
function startListening(roomId, nickname, identity = {}) {
  fetch('/api/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      room_id: roomId,
      nickname: nickname,
      sec_uid: identity.sec_uid || '',
      anchor_id: identity.anchor_id || '',
      douyin_id: identity.douyin_id || ''
    })
  })
  .then(r => r.json())
  .then(data => {
    if (data.success) {
      currentRooms[roomId] = {nickname: nickname || roomId,
                              is_temporary: data.is_temporary || false,
                              douyin_id: data.douyin_id || ''}
      lastRoomId = roomId
      const btn = document.getElementById('btn')
      const inputEl = document.getElementById('input')
      if (btn) {
        btn.textContent = '停止'
        btn.className = 'stop-btn'
      }
      // 首次监听显示提示
      if (Object.keys(mysteries).length === 0) {
        document.getElementById('events').innerHTML = '<div class="empty"><div class="icon">🎯</div>正在同步当前在线神秘人...</div>'
      }
      setStatus('监听中', 'green')
      if (btn) btn.disabled = false
      if (inputEl) inputEl.value = ''
      resetBtnText()
      connectSSE(roomId)
      updateRoomSelector()
      updateHeaderState()
      selectRoom(roomId)
    } else {
      showToast('❌ ' + (data.error || '启动失败'))
      const btn = document.getElementById('btn')
      if (btn) {
        btn.disabled = false
        btn.textContent = '🔍 监听'
      }
    }
  })
}

function connectSSE(roomId) {
  const es = new EventSource('/stream/' + roomId)
  eventSources[roomId] = es
  disconnectTimers[roomId] = null

  es.onmessage = function(e) {
    if (!e.data) return
    // 收到消息取消该房间的断线延时
    cancelDisconnect(roomId)
    try {
      const event = JSON.parse(e.data)
      handleEvent(event, roomId)
    } catch(err) { console.warn('[SSE] parse/handle error:', err) }
  }
  es.onerror = function() {
    if (disconnectTimers[roomId]) return
    disconnectTimers[roomId] = setTimeout(() => {
      setStatus('已断开', 'red')
      disconnectTimers[roomId] = null
    }, 5000)
  }
}

function cancelDisconnect(roomId) {
  if (disconnectTimers[roomId]) {
    clearTimeout(disconnectTimers[roomId])
    disconnectTimers[roomId] = null
  }
}

// 每条事件都写 document.title 会让标签栏持续重绘；1 秒最多写一次足够看“还在动”。
let lastTitleWriteAt = 0
function updateLiveTitle(icon, name) {
  const now = Date.now()
  if (now - lastTitleWriteAt < 1000) return
  lastTitleWriteAt = now
  document.title = icon + new Date().toLocaleTimeString() + ' ' +
    String(name || '?').slice(0, 12) + ' | 克克咪'
}

// 事件密集时整表重绘 + stagger 动画是主要卡顿源，合并成 250ms 一次。
let mysteryRenderTimer = null
function scheduleMysteryRender() {
  if (mysteryRenderTimer) return
  mysteryRenderTimer = setTimeout(() => {
    mysteryRenderTimer = null
    if (currentView === 'mystery') renderMysteries()
  }, 250)
}

function mKey(d) {
  const isPrivate = !d.sec_uid && (!d.unique_id || d.unique_id === '?')
  return d.room_id + ':' + (isPrivate
    ? d.display + ':' + (d.consume_level||0) + ':' + (d.badge_level||0)
    : d.sec_uid || d.display || '?')
}

function giftValueText(event) {
  const quantity = Number(event.gift_quantity || event.count || event.gift_count || 1)
  const unit = Number(event.unit_diamonds)
  const total = Number(event.total_diamonds)
  const verified = event.quantity_verified !== false && event.quantity_verified !== 0
  const known = Boolean(event.price_known) && verified &&
    Number.isFinite(unit) && unit > 0 && Number.isFinite(total) && total >= 0
  if (!known) return `×${quantity}｜价格未知`
  return `×${quantity}｜单价 ${unit} 钻石｜合计 ${total} 钻石`
}

function giftSummaryText(user) {
  const quantity = Number(user.gift_quantity_total ?? user.gift_count ?? 0)
  const events = Number(user.gift_event_count ?? (quantity > 0 ? 1 : 0))
  if (events <= 0 && quantity <= 0) return ''
  return `🎁${events}次/${quantity}个`
}

function handleEvent(event, roomId) {
  const d = event.data || {}
  // 所属 SSE 连接是实时事件的房间隔离边界；不能让缺失或脏 payload 串到别厅。
  if (roomId) d.room_id = roomId
  // 补充房间信息
  const roomNick = currentRooms[roomId]?.nickname || roomId
  switch(event.type) {
    case 'init':
    case 'connected':
      cancelDisconnect(roomId)
      setStatus('监听中', 'green')
      // 从 SSE init 事件渲染/补漏公屏历史；冻结阅读时只补缓存，不重绘当前画面。
      if (d.feed && Array.isArray(d.feed)) {
        const added = replaceRoomFeed(roomId, d.feed)
        if (!absorbWhileFrozen(roomId, added) &&
            currentView === 'feed' && selectedRoomId === roomId) {
          loadFeed()
        }
      }
      break
    case 'room_anonymous':
      break
    case 'disconnected':
      if (d.reconnecting) {
        setStatus('重连中...', 'gray')
      } else {
        setStatus('已断开', 'red')
      }
      break
    case 'mystery_enter':
      if (mysteries[mKey(d)]) {
        // 同一个人换马甲了
        const old = mysteries[mKey(d)]
        if (old.display && old.display !== d.display) {
          if (!old.aliases) old.aliases = []
          if (!old.aliases.includes(old.display)) old.aliases.push(old.display)
          if (!old.aliases.includes(d.display)) old.aliases.push(d.display)
        }
        old.display = d.display
        old.real_name = d.real_name
        old.sec_uid = d.sec_uid
        old.unique_id = d.unique_id || d.extra?.unique_id || old.unique_id
        old.badge_level = d.badge_level || old.badge_level
        old.consume_level = d.consume_level || old.consume_level
        old.extra = d.extra || old.extra
        old.room_id = d.room_id || old.room_id
        old.room_nickname = d.room_nickname || old.room_nickname
        old.enter_count = (old.enter_count || 0) + 1
        old.time = Date.now()
      } else {
        mysteries[mKey(d)] = {
          display: d.display, real_name: d.real_name,
          sec_uid: d.sec_uid,
          unique_id: d.unique_id || d.extra?.unique_id || '',
          badge_level: d.badge_level || 0, consume_level: d.consume_level || 0,
          extra: d.extra || null,
          room_id: d.room_id || roomId,
          room_nickname: d.room_nickname || roomNick,
          enter_count: 1,
          aliases: [],
          time: Date.now(),
          is_regular: d.is_regular || false
        }
      }
      // 调试：公屏事件计数器
      updateLiveTitle('📡', d.display)
      if (currentView === 'mystery' && !d.is_regular) scheduleMysteryRender()
      break
    case 'mystery_chat':
      if (!mysteries[mKey(d)]) {
        mysteries[mKey(d)] = {
          display: d.display, real_name: d.real_name,
          sec_uid: d.sec_uid, unique_id: d.unique_id || '',
          badge_level: d.badge_level || 0, consume_level: d.consume_level || 0,
          extra: null, aliases: [],
          time: Date.now(),
          room_id: d.room_id || roomId,
          room_nickname: d.room_nickname || roomNick,
          enter_count: 0, is_regular: d.is_regular || false
        }
      } else {
        if (d.display && d.display !== mysteries[mKey(d)].display) {
          if (!mysteries[mKey(d)].aliases) mysteries[mKey(d)].aliases = []
          if (!mysteries[mKey(d)].aliases.includes(d.display)) mysteries[mKey(d)].aliases.push(d.display)
        }
      }
      updateLiveTitle('💬', d.display)
      if (currentView === 'mystery' && !d.is_regular) scheduleMysteryRender()
      appendFeedItem(event, roomId)
      break
    case 'mystery_gift':
      if (!mysteries[mKey(d)]) {
        mysteries[mKey(d)] = {
          display: d.display, real_name: d.real_name,
          sec_uid: d.sec_uid, unique_id: d.unique_id || d.extra?.unique_id || '',
          badge_level: d.badge_level || 0, consume_level: d.consume_level || 0,
          extra: null, aliases: [],
          time: Date.now(),
          room_id: d.room_id || roomId,
          room_nickname: d.room_nickname || roomNick,
          enter_count: 0, is_regular: d.is_regular || false
        }
      } else {
        // 同一个人换马甲（送礼时发现不同马甲）
        if (d.display && d.display !== mysteries[mKey(d)].display) {
          if (!mysteries[mKey(d)].aliases) mysteries[mKey(d)].aliases = []
          if (!mysteries[mKey(d)].aliases.includes(d.display)) mysteries[mKey(d)].aliases.push(d.display)
        }
        if (d.real_name && d.real_name !== d.display) {
          mysteries[mKey(d)].real_name = d.real_name
          if (d.extra) mysteries[mKey(d)].extra = d.extra
        }
      }
      updateLiveTitle('🎁', d.display)
      if (currentView === 'mystery' && !d.is_regular) scheduleMysteryRender()
      appendFeedItem(event, roomId)
      break
    case 'room_offline':
      setStatus('已断开', 'red')
      // 直播间已下播，自动停止监听
      showToast('📴 直播已结束，已自动停止')
      stopRoom(roomId)
      break
    case 'error':
      break
  }
}

// 历史模式：选直播间后加载跨会话记录
let _historyRoomId = null  // 当前选中的历史直播间
let _historyRoomIds = []  // 合并后该房间的全部room_ids

function renderHistory() {
  const container = document.getElementById('events')
  container.innerHTML = '<div class="empty"><div class="icon">⏳</div>加载中...</div>'

  // 有选中房间 → 优先按 nickname 查跨 room_id 历史
  let url
  if (selectedRoomId) {
    const nick = currentRooms[selectedRoomId]?.nickname
    if (nick) {
      url = '/api/history_by_nickname?name=' + encodeURIComponent(nick)
    } else {
      url = '/api/history_all?room_id=' + encodeURIComponent(selectedRoomId)
    }
  } else {
    url = '/api/history_all_all'
  }

  fetch(url)
    .then(r => r.json())
    .then(res => {
      const records = res.records || (res.success && res.records ? res.records : null)
      if (!res.success || !records || records.length === 0) {
        container.innerHTML = '<div class="empty"><div class="icon">📜</div>暂无历史记录</div>'
        return
      }
      // 渲染选择栏（精简版）
      let html = ''

      records.forEach(item => {
        const extra = item.extra || {}
        const uniqueId = extra.unique_id || item.unique_id || item.sec_uid?.slice(0, 12) || '?'
        const profileUrl = item.sec_uid ? 'https://www.douyin.com/user/' + encodeURIComponent(item.sec_uid) : null
        const roomNick = item.room_nickname || extra.room_nickname || '?'

        // 所有马甲
        let displayHtml = ''
        if (item.displays && item.displays.length > 0) {
          displayHtml = item.displays.map(function(d) {
            const display = d.display || ''
            const isCurrent = d.is_current
            const isDou = display.startsWith('dou')
            if (isCurrent) {
              return '<span style="color:#fe2c55;font-size:11px">✅ ' + escapeHtml(display) + (isDou ? ' <span style="color:#ff6b35;font-size:9px">稳定</span>' : ' <span style="color:#34c759;font-size:9px">有效</span>') + '</span>'
            } else if (isDou) {
              return '<span style="color:#888;font-size:11px">⏳ ' + escapeHtml(display) + ' <span style="color:#666;font-size:9px">仅供参考</span></span>'
            } else {
              return '<span style="color:#ff6b35;font-size:11px">❌ ' + escapeHtml(display) + ' <span style="color:#666;font-size:9px">已失效</span></span>'
            }
          }).join('<br>')
        }

        const key = item.sec_uid || item.display || '?'
        html += '<div class="event mystery" data-su="' + escapeHtml(key) + '">'
        // 房间标签
        html += '<div style="font-size:10px;color:#fe2c55;font-weight:600;margin-bottom:2px">' + escapeHtml(roomNick) + '</div>'
        // 真实名字
        const realName = escapeHtml(item.real_name || item.display || '?')
        html += '<div class="name-box">'
        html += '<span class="name-text mn" onclick="toggleName(' + "'" + escapeHtml(key) + "'" + ')">' + realName + '</span>'
        html += '</div>'
        html += '<div style="font-size:10px;color:#888;margin:1px 0">🆔 ' + escapeHtml(uniqueId) + '</div>'
        if (displayHtml) html += '<div style="font-size:11px;margin:3px 0;line-height:1.6">' + displayHtml + '</div>'
        // 最后出现时间
        if (item.last_seen) {
          const d = new Date(item.last_seen * 1000)
          html += '<div style="font-size:9px;color:#555;margin-top:2px">最后出现: ' + d.toLocaleString('zh-CN') + '</div>'
        }
        if (profileUrl) {
          html += '<div style="font-size:10px;color:#555;margin-top:3px"><a href="' + profileUrl + '" target="_blank" style="color:#5ac8fa;text-decoration:none">🔗 主页</a></div>'
        }
        html += '</div>'
      })
      container.innerHTML = html
    })
    .catch(function(e) {
      container.innerHTML = '<div class="empty"><div class="icon">❌</div>加载失败: ' + escapeHtml(String(e)) + '</div>'
    })
}

function fetchHistoryForRoom(roomId) {
  const container = document.getElementById('events')
  // 保留直播间选择栏
  const tabsHtml = container.querySelector('.room-tab') ? container.querySelector('div:first-child').outerHTML : ''

  // 如果有合并的 room_ids，全部拉取
  const roomIds = (_historyRoomIds && _historyRoomIds.length > 0) ? _historyRoomIds : [roomId]

  Promise.all(roomIds.map(rid =>
    fetch('/api/history_all?room_id=' + encodeURIComponent(rid)).then(r => r.json())
  )).then(results => {
    // 合并所有记录，按 sec_uid 去重
    const merged = {}
    results.forEach(res => {
      if (!res.success || !res.records) return
      res.records.forEach(item => {
        const key = item.sec_uid || item.display
        if (!merged[key]) {
          merged[key] = item
        } else {
          // 合并 displays（去重）
          const existing = merged[key]
          const existingDisplays = (existing.displays || []).map(d => d.display)
          const newDisplays = (item.displays || []).filter(d => !existingDisplays.includes(d.display))
          existing.displays = [...(existing.displays || []), ...newDisplays]
          // 合并 counts
          existing.enter_count = (existing.enter_count || 0) + (item.enter_count || 0)
          existing.chat_count = (existing.chat_count || 0) + (item.chat_count || 0)
          existing.gift_count = (existing.gift_count || 0) + (item.gift_count || 0)
          if ((item.last_seen || 0) > (existing.last_seen || 0)) existing.last_seen = item.last_seen
        }
      })
    })

    // 合并完成后，每个用户每种 display 类型只留最新的一个
    Object.values(merged).forEach(item => {
      const displays = item.displays || []
      if (displays.length <= 1) return
      const mystery = displays.filter(d => d.display.startsWith('神秘人'))
      const dou = displays.filter(d => d.display.startsWith('dou'))
      const other = displays.filter(d => !d.display.startsWith('神秘人') && !d.display.startsWith('dou'))
      const filtered = []
      for (const group of [mystery, dou, other]) {
        if (group.length) {
          group.sort((a, b) => (a.last_seen || 0) - (b.last_seen || 0))
          filtered.push(group[group.length - 1])
        }
      }
      filtered.sort((a, b) => (a.last_seen || 0) - (b.last_seen || 0))
      item.display = filtered[filtered.length - 1].display
      item.displays = filtered
    })

    const records = Object.values(merged).sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0))

    if (records.length === 0) {
      container.innerHTML = (tabsHtml || '') + '<div class="empty"><div class="icon">📜</div>该直播间暂无神秘人历史记录</div>'
      return
    }

    // 统计 - 简洁版
    let html = tabsHtml

    records.forEach(item => {
      const extra = item.extra || {}
      const uniqueId = extra.unique_id || item.sec_uid?.slice(0, 12) || '?'
      // 主页链接
      const profileUrl = item.sec_uid ? 'https://www.douyin.com/user/' + encodeURIComponent(item.sec_uid) : null

      // 所有马甲
      let displayHtml = ''
      if (item.displays && item.displays.length > 0) {
        displayHtml = item.displays.map(function(d) {
          const display = d.display || ''
          const isCurrent = d.is_current
          const isDou = display.startsWith('dou')
          if (isCurrent) {
            return '<span style="color:#fe2c55;font-size:11px">✅ ' + escapeHtml(display) + (isDou ? ' <span style="color:#ff6b35;font-size:9px">稳定</span>' : ' <span style="color:#34c759;font-size:9px">有效</span>') + '</span>'
          } else if (isDou) {
            return '<span style="color:#888;font-size:11px">⏳ ' + escapeHtml(display) + ' <span style="color:#666;font-size:9px">仅供参考</span></span>'
          } else {
            return '<span style="color:#ff6b35;font-size:11px">❌ ' + escapeHtml(display) + ' <span style="color:#666;font-size:9px">已失效</span></span>'
          }
        }).join('<br>')
      }

      const key = item.sec_uid || item.display || '?'
      html += '<div class="event mystery" data-su="' + escapeHtml(key) + '">'
      // 真实名字（点击展开全名）+ 主页链接
      const realName = escapeHtml(item.real_name || item.display || '?')
      html += '<div class="name-box">'
      html += '<span class="name-text mn" onclick="toggleName(' + "'" + escapeHtml(key) + "'" + ')">' + realName + '</span>'
      html += '</div>'
      html += '<div style="font-size:10px;color:#888;margin:1px 0">🆔 ' + escapeHtml(uniqueId) + '</div>'
      if (displayHtml) html += '<div style="font-size:11px;margin:3px 0;line-height:1.6">' + displayHtml + '</div>'
      // 最后出现时间
      if (item.last_seen) {
        const d = new Date(item.last_seen * 1000)
        html += '<div style="font-size:9px;color:#555;margin-top:2px">最后出现: ' + d.toLocaleString('zh-CN') + '</div>'
      }
      if (profileUrl) {
        html += '<div style="font-size:10px;color:#555;margin-top:3px"><a href="' + profileUrl + '" target="_blank" style="color:#5ac8fa;text-decoration:none">🔗 主页</a></div>'
      }
      html += '</div>'
    })
    container.innerHTML = html
  }).catch(function(e) {
    container.innerHTML = (tabsHtml || '') + '<div class="empty"><div class="icon">❌</div>加载失败: ' + escapeHtml(String(e)) + '</div>'
  })
}

// 全部：只显示抖音当前在线名单。后端每个房间统一抓一次，浏览器只读共享快照。
const onlineByRoom = {}
let onlineLoadSeq = 0

// 在线名单每 10 秒同步一次。内容没变、且屏幕上还是上次画的那份时直接跳过，
// 不做整块 innerHTML 重建——那会把正在翻名单的人弹回顶部。
// 必须重绘时保存并恢复 scrollTop（内容被整块换掉会让浏览器把 scrollTop 夹回 0）。
let lastOnlineStable = ''
// 后台每 3 秒都会更新同步时间戳；若把它算进「内容有没有变」，
// 名单每次轮询都会整页重建，防重绘就形同虚设。指纹里剔除它，单独原地更新。
const ONLINE_SYNC_TIME_RE = /(<span id="onlineSyncTime">)[^<]*(<\/span>)/

function paintOnlineAudience(container, html) {
  const stable = html.replace(ONLINE_SYNC_TIME_RE, '$1$2')
  const stillShowingRoster = container.querySelector('.online-summary') !== null
  if (stillShowingRoster && stable === lastOnlineStable) {
    const clock = container.querySelector('#onlineSyncTime')
    const match = html.match(/<span id="onlineSyncTime">([^<]*)<\/span>/)
    if (clock && match) clock.textContent = match[1]
    return false
  }
  lastOnlineStable = stable
  const previousScrollTop = container.scrollTop
  container.innerHTML = html
  const maxScrollTop = Math.max(0, container.scrollHeight - container.clientHeight)
  container.scrollTop = Math.min(previousScrollTop, maxScrollTop)
  return true
}

function renderOnlineAudienceData(data, mysteryOnly = false) {
  const container = document.getElementById('events')
  const records = (data.records || []).filter(user => !mysteryOnly || user.is_mystery).slice().sort((a, b) => {
    if (Boolean(a.is_mic) !== Boolean(b.is_mic)) return a.is_mic ? -1 : 1
    if (a.is_mic && b.is_mic) return Number(a.mic_slot || 999) - Number(b.mic_slot || 999)
    return Number(b.known_diamonds || 0) - Number(a.known_diamonds || 0) ||
      Number(a.rank || 999999) - Number(b.rank || 999999)
  })
  const roomName = currentRooms[data.room_id]?.nickname || data.room_nickname || data.room_id || '?'
  const updateText = data.updated_at ? formatTime(data.updated_at) : '等待首次同步'
  const modeIcon = mysteryOnly ? '🎯' : '📋'
  const modeTitle = mysteryOnly ? `当前在厅神秘人 ${records.length} 人` : `当前在线 ${records.length} 人`
  let html = `<div class="online-summary">` +
    `<div><b>${modeIcon} ${escapeHtml(roomName)} · ${modeTitle}</b>` +
    `${data.stale ? '<span class="online-stale">数据暂时未更新，保留上次名单</span>' : ''}</div>` +
    `<span>抖音在线名单每${Number(data.poll_interval) || 3}秒同步 · 最近成功 <span id="onlineSyncTime">${updateText}</span></span>` +
    `<span>${mysteryOnly ? '显示当前在线的匿名神秘人与已解析神秘人；离厅后立即从本页隐藏。' : '麦上主持置顶；其余观众按本厅北京时间今日0点至现在的实时票数排序。名单不写入数据库。'}</span>` +
    `</div>`
  if (records.length === 0) {
    const message = data.error || (mysteryOnly ? '当前在线名单中没有神秘人' : '正在等待抖音返回当前在线名单')
    html += `<div class="empty"><div class="icon">${modeIcon}</div>${escapeHtml(message)}</div>`
    paintOnlineAudience(container, html)
    return
  }
  records.forEach((user, index) => {
    if (mysteryOnly) {
      const profile = user.mystery_profile || {}
      // 身份库只按 UID 完全一致命中；命中时优先用它，并保留当天匿名编号。
      const matched = user.matched_identity || null
      const resolved = Boolean(user.mystery_profile) || Boolean(matched)
      const alias = matched
        ? (user.nickname || profile.display || '神秘人')
        : (profile.display || user.nickname || '神秘人')
      const realName = matched
        ? (matched.real_name || user.nickname || '?')
        : (profile.real_name || user.nickname || '?')
      const douyinId = (matched && matched.douyin_id) ||
        profile.unique_id || user.display_id || '未知'
      const followerCount = matched ? matched.follower_count : profile.follower_count
      const awemeCount = matched ? matched.aweme_count : profile.aweme_count
      const secUid = (matched && matched.sec_uid) || profile.sec_uid || user.sec_uid || ''
      const profileUrl = secUid ? `https://www.douyin.com/user/${encodeURIComponent(secUid)}` : ''
      const anonymousUid = String(user.webcast_uid || '').trim()
      const shortAnonymousUid = anonymousUid.length > 20
        ? `${anonymousUid.slice(0, 8)}…${anonymousUid.slice(-8)}`
        : anonymousUid
      html += `<div class="event online-card mystery">` +
        `<div class="online-card-head"><span class="online-avatar online-avatar-fallback">🎯</span>` +
        `<div class="online-identity"><span class="online-role">${escapeHtml(alias)}</span>` +
        `<strong>${levelBadgeHtml(user.consume_level)}${escapeHtml(resolved ? realName : '身份待解析')}</strong>` +
        `<span>${resolved ? `抖音号 ${escapeHtml(douyinId)}` : `匿名UID ${escapeHtml(shortAnonymousUid || '待抖音下发')}`}</span></div></div>` +
        `<div class="online-meta">${resolved ? `粉丝 ${Number(followerCount || 0).toLocaleString('zh-CN')} · 作品 ${Number(awemeCount || 0).toLocaleString('zh-CN')}` : '当天匿名编号会变化，系统按 UID 持续核对'}` +
        `${resolved && profileUrl ? ` · <a href="${profileUrl}" target="_blank" rel="noreferrer" style="color:#5ac8fa">主页</a>` : ''}</div>` +
        `</div>`
      return
    }
    const isMic = Boolean(user.is_mic)
    const ticketValue = isMic
      ? Number(user.received_tickets || 0)
      : Number(user.known_tickets || user.known_diamonds || 0)
    const nickname = user.nickname || user.display_id || '?'
    const displayId = user.display_id || ''
    const avatar = user.avatar_url
      ? `<img class="online-avatar" src="${escapeHtml(user.avatar_url)}" alt="">`
      : `<span class="online-avatar online-avatar-fallback">${isMic ? '🎙' : '👤'}</span>`
    html += `<div class="event online-card${isMic ? ' online-host' : ''}">` +
      `<div class="online-card-head">${avatar}<div class="online-identity">` +
      `<span class="online-role">${isMic ? `🎙 麦上主持${user.mic_slot ? ' ' + Number(user.mic_slot) : ''}` : `#${index + 1} 在线观众`}</span>` +
      `<strong>${levelBadgeHtml(user.consume_level)}${escapeHtml(nickname)}</strong>` +
      `${displayId && displayId !== nickname ? `<span>抖音号 ${escapeHtml(displayId)}</span>` : ''}` +
      `</div></div>` +
      `<div class="online-spend">${ticketValue.toLocaleString('zh-CN')} 票</div>` +
      `<div class="online-meta">${isMic ? '抖音麦位实时收票 · 本轮上麦，下麦重算' : '本厅北京时间今日实时累计'}</div>` +
      `</div>`
  })
  paintOnlineAudience(container, html)
}

function renderOnlineAudience(mysteryOnly = false) {
  const container = document.getElementById('events')
  const roomId = selectedRoomId || Object.keys(currentRooms)[0] || null
  if (!roomId) {
    onlineLoadSeq++
    container.innerHTML = '<div class="empty"><div class="icon">📋</div>请先监听并选择一个直播间</div>'
    return
  }
  if (!selectedRoomId) selectedRoomId = roomId
  if (onlineByRoom[roomId]) {
    renderOnlineAudienceData(onlineByRoom[roomId], mysteryOnly)
  } else {
    container.innerHTML = '<div class="empty"><div class="icon">⏳</div>正在同步该厅当前在线名单...</div>'
  }
  const requestSeq = ++onlineLoadSeq
  fetch('/api/online/' + encodeURIComponent(roomId))
    .then(r => r.json())
    .then(data => {
      if (!data.success) throw new Error(data.error || '在线名单加载失败')
      onlineByRoom[roomId] = data
      const expectedView = mysteryOnly ? 'mystery' : 'all'
      if (requestSeq !== onlineLoadSeq || currentView !== expectedView || selectedRoomId !== roomId) return
      renderOnlineAudienceData(data, mysteryOnly)
    })
    .catch(error => {
      const expectedView = mysteryOnly ? 'mystery' : 'all'
      if (requestSeq !== onlineLoadSeq || currentView !== expectedView || selectedRoomId !== roomId) return
      if (onlineByRoom[roomId]) {
        const stale = Object.assign({}, onlineByRoom[roomId], {stale: true, error: error.message})
        renderOnlineAudienceData(stale, mysteryOnly)
      } else {
        container.innerHTML = `<div class="empty"><div class="icon">⚠️</div>${escapeHtml(error.message)}</div>`
      }
    })
}

function renderAllRecords() {
  return renderOnlineAudience(false)
}

function renderMysteries() {
  return renderOnlineAudience(true)
}

// ========== 管理员全局礼物价格库 ==========

let giftLibraryCache = []
// 出现过但拿不到价的礼物「名字」。礼物库按 gift_id 列，一个 id 一行一个
// 数字；而一个 id 底下可能挂着多个皮肤名、各价不同（线上 3729 底下 6 个
// 名字 3 种价）。没定价的皮肤名在按 id 的列表里不存在，于是永远补不上、
// 对应礼物一直不计入账目。这批要单独拎出来放最前面。
let giftPendingNames = []
let giftLibraryLoadSeq = 0
let giftLibraryQuery = ''   // 礼物库搜索关键词（按名字/ID 模糊匹配，支持 1-2 字）

function giftPriceSourceText(source) {
  if (source === 'manual') return '管理员补录'
  if (source === 'douyin') return '抖音原始价格'
  return '价格待补'
}

function giftLibraryMatches(gift, query) {
  const q = String(query || '').trim().toLowerCase()
  if (!q) return true
  const name = String(gift.gift_name || '').toLowerCase()
  const id = String(gift.gift_id || '').toLowerCase()
  return name.includes(q) || id.includes(q)
}

// 只渲染卡片部分。用 giftLibraryCache 的原始 index，保证 saveGiftPrice(index)
// 始终对得上完整缓存里的那条（过滤只是跳过不匹配的，不重排下标）。
function giftLibraryCardsHtml() {
  let html = ''
  let shown = 0
  giftLibraryCache.forEach((gift, index) => {
    if (!giftLibraryMatches(gift, giftLibraryQuery)) return
    shown += 1
    const sourceText = giftPriceSourceText(gift.price_source)
    const sourceClass = gift.price_source === 'manual' ? 'manual' :
      (gift.price_source === 'douyin' ? 'douyin' : 'pending')
    const priceValue = gift.unit_diamonds == null ? '' : Number(gift.unit_diamonds)
    const firstSeen = gift.first_seen ? formatDateTime(gift.first_seen) : '未知'
    const lastSeen = gift.last_seen ? formatDateTime(gift.last_seen) : '未知'
    html += `<div class="event gift-library-card">` +
      `<div class="gift-library-title"><div><strong>${escapeHtml(gift.gift_name || gift.gift_id)}</strong>` +
      `<span>ID ${escapeHtml(gift.gift_id)}</span></div>` +
      `<em class="gift-source ${sourceClass}">${sourceText}</em></div>` +
      `<div class="gift-library-meta">出现 ${Number(gift.occurrence_count || 0)} 次 · ` +
      `首次 ${firstSeen} · 最后 ${lastSeen}</div>` +
      `<label>单价（抖币/钻石）<input id="gift-price-${index}" type="number" min="1" step="1" value="${priceValue}" placeholder="请输入正整数"></label>` +
      `<label>备注<input id="gift-note-${index}" value="${escapeHtml(gift.note || '')}" placeholder="可选"></label>` +
      `<label>修改原因<input id="gift-reason-${index}" value="" placeholder="必填，例如：现场确认价格"></label>` +
      `<div class="gift-library-actions"><span id="gift-save-status-${index}"></span>` +
      `<button id="gift-save-${index}" onclick="saveGiftPrice(${index})">保存并全局生效</button></div>` +
      `</div>`
  })
  if (!shown) {
    return `<div class="empty"><div class="icon">🔍</div>没有匹配「${escapeHtml(giftLibraryQuery)}」的礼物</div>`
  }
  return html
}

function giftPendingNamesHtml() {
  if (!giftPendingNames.length) return ''
  const cards = giftPendingNames.map((item, index) => {
    const siblings = (item.siblings || []).length
      ? `<div class="gift-pending-siblings">同 ID 其他皮肤：` +
        item.siblings.map(s =>
          `<span>${escapeHtml(s.gift_name)} <b>${Number(s.unit_diamonds)}</b></span>`
        ).join('') + `</div>`
      : `<div class="gift-pending-siblings">同 ID 下没有别的已定价皮肤</div>`
    return `<div class="event gift-library-card gift-pending-card">` +
      `<div class="gift-library-title"><div><strong>${escapeHtml(item.gift_name || '')}</strong>` +
      `<span>ID ${escapeHtml(item.gift_id || '')}</span></div>` +
      `<em class="gift-source pending">未定价 · 不计入账目</em></div>` +
      `<div class="gift-library-meta">出现 ${Number(item.occurrence_count || 0)} 次 · ` +
      `最后 ${item.last_seen ? formatDateTime(item.last_seen) : '未知'}</div>` +
      siblings +
      `<label>单价（抖币/钻石）<input id="gift-name-price-${index}" type="number" min="1" step="1" placeholder="请输入正整数"></label>` +
      `<label>备注<input id="gift-name-note-${index}" placeholder="可选"></label>` +
      `<label>修改原因<input id="gift-name-reason-${index}" placeholder="必填，例如：现场确认价格"></label>` +
      `<div class="gift-library-actions"><span id="gift-name-status-${index}"></span>` +
      `<button id="gift-name-save-${index}" onclick="saveGiftNamePrice(${index})">保存并全局生效</button></div>` +
      `</div>`
  }).join('')
  return `<div class="gift-pending-block"><div class="gift-pending-head">` +
    `⚠️ ${giftPendingNames.length} 个礼物名还没有价格，这些礼物<b>不计入账目</b>` +
    `</div>${cards}</div>`
}

function saveGiftNamePrice(index) {
  const item = giftPendingNames[index]
  if (!item) return Promise.resolve(false)
  const priceInput = document.getElementById('gift-name-price-' + index)
  const noteInput = document.getElementById('gift-name-note-' + index)
  const reasonInput = document.getElementById('gift-name-reason-' + index)
  const status = document.getElementById('gift-name-status-' + index)
  const button = document.getElementById('gift-name-save-' + index)
  const price = Number(priceInput?.value)
  const reason = String(reasonInput?.value || '').trim()
  const errors = []
  if (!Number.isInteger(price) || price <= 0) errors.push('单价必须是正整数')
  if (!reason) errors.push('修改原因不能为空')
  if (errors.length) {
    if (status) status.textContent = errors.join('；')
    return Promise.resolve(false)
  }
  if (button) button.disabled = true
  if (status) status.textContent = '保存中...'
  return fetch('/api/admin/gift-names', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      gift_name: item.gift_name,
      unit_diamonds: price,
      note: String(noteInput?.value || '').trim(),
      reason: reason
    })
  })
    .then(r => r.json())
    .then(data => {
      if (!data.success) throw new Error(data.error || '保存失败')
      const rows = Number((data.result || {}).recalculated_rows || 0)
      if (status) status.textContent = `已保存，全局生效；重算 ${rows} 条`
      if (button) button.disabled = false
      // 定完价它就不该再待在这个列表里了，重新拉一次。
      return renderGiftLibrary()
    })
    .catch(error => {
      if (status) status.textContent = error.message
      if (button) button.disabled = false
      return false
    })
}

function renderGiftLibraryCards(records, pendingCount) {
  const container = document.getElementById('events')
  const search = `<label class="gift-search" style="display:flex;align-items:center;gap:.5rem;margin-top:.7rem;font-weight:600">` +
    `<span>🔍</span><input id="giftSearch" type="search" autocomplete="off" ` +
    `placeholder="输入 1-2 字模糊搜索，如 兔 / 飞机 / 火箭" value="${escapeHtml(giftLibraryQuery)}" ` +
    `style="flex:1;min-width:0;padding:.5rem .7rem;border-radius:.6rem;border:1px solid rgba(255,255,255,.22);background:rgba(255,255,255,.06);color:#fff;font:inherit"></label>`
  const summary = `<div class="gift-library-summary"><div><b>🧰 全局礼物库</b>` +
    `<strong>${Number(pendingCount || 0)} 个待补价格</strong></div>` +
    `<span>一次修改，全厅后续礼物直接采用；最近7天数量可信的记录会自动重算。</span>` +
    `<span>每次修改保留管理员、旧价、新价、时间和原因。</span>` + search + `</div>`
  if (!records.length) {
    container.innerHTML = summary + giftPendingNamesHtml() +
      '<div class="empty"><div class="icon">🎁</div>监听到礼物后会自动加入这里</div>'
    return
  }
  container.innerHTML = summary + giftPendingNamesHtml() +
    `<div id="giftLibraryCards">${giftLibraryCardsHtml()}</div>`
  const input = document.getElementById('giftSearch')
  if (input) {
    // 只重渲染卡片容器，搜索框本身不重绘 → 输入焦点和光标不丢。
    input.oninput = () => {
      giftLibraryQuery = input.value
      const cards = document.getElementById('giftLibraryCards')
      if (cards) cards.innerHTML = giftLibraryCardsHtml()
    }
  }
}

function renderGiftLibrary() {
  const container = document.getElementById('events')
  const requestSeq = ++giftLibraryLoadSeq
  container.innerHTML = '<div class="empty"><div class="icon">⏳</div>正在加载全局礼物库...</div>'
  fetch('/api/admin/gifts')
    .then(r => r.json())
    .then(data => {
      if (requestSeq !== giftLibraryLoadSeq || currentView !== 'giftLibrary') return
      if (!data.success) throw new Error(data.error || '礼物库加载失败')
      giftLibraryCache = data.records || []
      giftPendingNames = data.pending_names || []
      renderGiftLibraryCards(giftLibraryCache, data.pending_count)
    })
    .catch(error => {
      if (requestSeq !== giftLibraryLoadSeq || currentView !== 'giftLibrary') return
      container.innerHTML = `<div class="empty"><div class="icon">⚠️</div>${escapeHtml(error.message)}</div>`
    })
}

function saveGiftPrice(index) {
  const gift = giftLibraryCache[index]
  if (!gift) return Promise.resolve(false)
  const priceInput = document.getElementById('gift-price-' + index)
  const noteInput = document.getElementById('gift-note-' + index)
  const reasonInput = document.getElementById('gift-reason-' + index)
  const status = document.getElementById('gift-save-status-' + index)
  const button = document.getElementById('gift-save-' + index)
  const price = Number(priceInput?.value)
  const reason = String(reasonInput?.value || '').trim()
  const note = String(noteInput?.value || '').trim()
  const errors = []
  if (!Number.isInteger(price) || price <= 0) errors.push('单价必须是正整数')
  if (!reason) errors.push('修改原因不能为空')
  if (errors.length) {
    if (status) status.textContent = errors.join('；')
    return Promise.resolve(false)
  }
  if (button) button.disabled = true
  if (status) status.textContent = '保存中...'
  return fetch('/api/admin/gifts/' + encodeURIComponent(gift.gift_id), {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      gift_name: gift.gift_name,
      unit_diamonds: price,
      note: note,
      reason: reason
    })
  })
    .then(r => r.json())
    .then(data => {
      if (!data.success) throw new Error(data.error || '保存失败')
      giftLibraryCache[index] = data.gift
      if (status) status.textContent = `已保存，全局生效；重算 ${Number(data.recalculated_rows || 0)} 条`
      if (button) button.disabled = false
      if (reasonInput) reasonInput.value = ''
      return true
    })
    .catch(error => {
      if (status) status.textContent = error.message
      if (button) button.disabled = false
      return false
    })
}

let analyticsLoadSeq = 0
let weeklyLoadSeq = 0
let giftAssignmentLoadSeq = 0
let giftAssignmentCache = []
let giftAssignmentSearch = ''
let giftAssignmentSearchBy = 'sender'  // 'sender'=刷的人 / 'recipient'=收礼主持

function analyticsRoomId() {
  return selectedRoomId || Object.keys(currentRooms)[0] || null
}

let dailyRankShowAll = false

function setDailyRankShowAll(value) {
  dailyRankShowAll = !!value
  renderDailyRanks()
}

function resetDailyRankPaging() {
  dailyRankShowAll = false
}

// 榜单被截断时的展开入口。默认只发前几名——实测前 20 名占全厅 74~99.9% 的票，
// 后面一百多号人多是 1、2 票，却让每 2 秒一次的刷新多背几十 KB。
function rankMoreHtml(truncated, total, kind) {
  if (!truncated) return ''
  const fn = kind === 'weekly' ? 'setWeeklyRankShowAll' : 'setDailyRankShowAll'
  return `<div class="rank-more"><button onclick="${fn}(true)">` +
    `显示全部 ${Number(total || 0)} 人</button></div>`
}

let weeklyRankShowAll = false

function setWeeklyRankShowAll(value) {
  weeklyRankShowAll = !!value
  renderWeeklyRanks(currentView)
}

function resetWeeklyRankPaging() {
  weeklyRankShowAll = false
}

function renderRankRows(records, keyField, emptyText) {
  if (!records.length) {
    return `<div class="rank-empty">${escapeHtml(emptyText)}</div>`
  }
  return records.map((item, index) => {
    const display = item.display || item[keyField] || '?'
    const rank = Number(item.rank || index + 1)
    // 神秘人已经解析出真名时，在马甲名后面用括号带出来；没解析出的保持原样。
    const realName = String(item.real_name || '').trim()
    const nameHtml = realName && realName !== display
      ? `${escapeHtml(display)}<span class="rank-real-name">（${escapeHtml(realName)}）</span>`
      : escapeHtml(display)
    // 同一个人戴马甲和不戴马甲会被合成一行，界面上不做标记：合并匹配的是
    // sec_uid / webcast_uid / numeric_uid 三种 id，从不按显示名匹配，所以
    // 关联是稳定可信的，不需要让人时时提防。响应里仍带 merged_keys，
    // 排查时按它就能查出哪些行是合并来的。
    return `<div class="daily-rank-row"><span class="daily-rank-no">#${rank}</span>` +
      `<strong>${nameHtml}</strong>` +
      `<em>${Number(item.tickets || 0).toLocaleString('zh-CN')} 票</em></div>`
  }).join('')
}

let lastDailyHtml = ''

function showingDailyFor(container, roomId) {
  return container.dataset.dailyRoom === String(roomId) &&
    container.querySelector('.daily-rank-summary') !== null
}

function renderDailyRanks() {
  const container = document.getElementById('events')
  const roomId = analyticsRoomId()
  if (!roomId) {
    analyticsLoadSeq++
    container.innerHTML = '<div class="empty"><div class="icon">📊</div>请先监听并选择一个直播间</div>'
    container.dataset.dailyRoom = ''
    return
  }
  const requestSeq = ++analyticsLoadSeq
  // 只有首次进入或切了厅才显示加载占位。已经在看本厅日榜时静默刷新——
  // 每 2 秒「内容→转圈→内容」的闪烁正是无条件占位造成的。
  if (!showingDailyFor(container, roomId)) {
    container.innerHTML = '<div class="empty"><div class="icon">⏳</div>正在加载今日日榜...</div>'
    container.dataset.dailyRoom = ''
  }
  const limitParam = dailyRankShowAll ? '?limit=0' : ''
  fetch('/api/daily_rank/' + encodeURIComponent(roomId) + limitParam)
    .then(r => r.json())
    .then(data => {
      if (requestSeq !== analyticsLoadSeq || currentView !== 'daily' || analyticsRoomId() !== roomId) return
      if (!data.success) throw new Error(data.error || '日榜加载失败')
      const visitors = data.visitors || []
      const hosts = data.hosts || []
      const roomName = currentRooms[roomId]?.nickname || roomId
      const html = `<div class="daily-rank-summary"><div><b>📊 ${escapeHtml(roomName)} · 今日日榜</b>` +
        `<strong>北京时间今日 00:00 至现在</strong></div>` +
        `<span>${escapeHtml(data.note || '仅统计今天监听期间票数可核算的礼物；页面实时更新。')}</span></div>` +
        `<div class="daily-rank-grid">` +
        `<section class="daily-rank-column"><h3>💎 游客日榜</h3><p>今天在本厅送出票数</p>` +
        renderRankRows(visitors, 'sender_key', '今天暂无可核算送礼') +
        rankMoreHtml(data.visitors_truncated, data.visitor_total, 'visitor') + `</section>` +
        `<section class="daily-rank-column"><h3>🎤 主持日榜</h3><p>今天在本厅收到票数</p>` +
        renderRankRows(hosts, 'recipient_key', '今天暂无可核算收礼') +
        rankMoreHtml(data.hosts_truncated, data.host_total, 'host') + `</section></div>`
      if (showingDailyFor(container, roomId) && html === lastDailyHtml) return
      lastDailyHtml = html
      const previousScrollTop = container.scrollTop
      container.innerHTML = html
      container.dataset.dailyRoom = String(roomId)
      const maxScrollTop = Math.max(0, container.scrollHeight - container.clientHeight)
      container.scrollTop = Math.min(previousScrollTop, maxScrollTop)
    })
    .catch(error => {
      if (requestSeq !== analyticsLoadSeq || currentView !== 'daily' || analyticsRoomId() !== roomId) return
      // 已经在看日榜时保留画面：2 秒轮询下，瞬时失败若换成错误页同样是闪烁
      if (showingDailyFor(container, roomId)) {
        console.warn('[DAILY] refresh failed:', error)
        return
      }
      container.innerHTML = `<div class="empty"><div class="icon">⚠️</div>${escapeHtml(error.message)}</div>`
    })
}

function weeklyRangeText(start, end) {
  if (!start || !end) return '本周实时'
  const options = {timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric'}
  const startText = new Date(Number(start) * 1000).toLocaleDateString('zh-CN', options)
  const endText = new Date(Number(end) * 1000).toLocaleDateString('zh-CN', options)
  return `${startText} 00:00 — ${endText} 00:00`
}

function renderWeeklyPeriodOptions(periods, selectedStart) {
  let html = `<option value=""${selectedStart ? '' : ' selected'}>本周实时</option>`
  ;(periods || []).forEach(period => {
    const start = Number(period.period_start || 0)
    const selected = Number(selectedStart || 0) === start ? ' selected' : ''
    html += `<option value="${start}"${selected}>${escapeHtml(weeklyRangeText(start, period.period_end))} · 已结算</option>`
  })
  return html
}

function renderHostLargeDetails(details) {
  if (!details || !details.length) return ''
  let html = `<details class="large-detail"><summary>大额时段 ${details.length} 条</summary>`
  details.forEach(item => {
    const slotText = item.slot_start
      ? `${formatDateTime(item.slot_start)} — ${formatTime(item.slot_end)}`
      : '时间未知'
    html += `<div class="large-detail-row"><span>${escapeHtml(item.visitor_display || item.visitor_key || '?')}</span>` +
      `<small>${escapeHtml(slotText)}</small><strong>${Number(item.tickets || 0).toLocaleString('zh-CN')} 票</strong></div>`
  })
  return html + '</details>'
}

function selectWeeklyPeriod(value) {
  renderWeeklyRanks(currentView, value || null)
}

function renderWeeklyRanks(kind, weekStart) {
  const container = document.getElementById('events')
  const roomId = analyticsRoomId()
  const expectedView = kind === 'visitorWeekly' ? 'visitorWeekly' : 'hostWeekly'
  if (!roomId) {
    weeklyLoadSeq++
    container.innerHTML = '<div class="empty"><div class="icon">📅</div>请先监听并选择一个直播间</div>'
    return
  }
  const requestSeq = ++weeklyLoadSeq
  const selectedStart = weekStart ? Number(weekStart) : null
  const baseUrl = '/api/admin/weekly_rank/' + encodeURIComponent(roomId)
  const params = []
  if (selectedStart) params.push('week_start=' + encodeURIComponent(selectedStart))
  if (weeklyRankShowAll) params.push('limit=0')
  const boardUrl = params.length ? baseUrl + '?' + params.join('&') : baseUrl
  // 只有首次进入或换了厅/周期才显示加载占位。已经在看时静默刷新——
  // 每 30 秒「内容→转圈→内容」的整块闪烁正是无条件占位造成的。
  const weeklyTag = expectedView + ':' + roomId + ':' + (selectedStart || '') + ':' + (weeklyRankShowAll ? 'all' : 'top')
  if (container.dataset.weeklyTag !== weeklyTag) {
    container.innerHTML = '<div class="empty"><div class="icon">⏳</div>正在加载周榜...</div>'
    container.dataset.weeklyTag = ''
  }
  Promise.all([
    fetch(baseUrl + '/periods').then(r => r.json()),
    fetch(boardUrl).then(r => r.json())
  ]).then(([periodData, data]) => {
      if (requestSeq !== weeklyLoadSeq || currentView !== expectedView || analyticsRoomId() !== roomId) return
      if (!periodData.success) throw new Error(periodData.error || '周榜周期加载失败')
      if (!data.success) throw new Error(data.error || '周榜加载失败')
      const isHost = expectedView === 'hostWeekly'
      const records = isHost ? (data.hosts || []) : (data.visitors || [])
      const roomName = currentRooms[roomId]?.nickname || roomId
      const title = isHost ? '🎤 主持周榜' : '💎 游客周榜'
      let html = `<div class="weekly-summary"><div><b>${title} · ${escapeHtml(roomName)}</b>` +
        `<strong>${escapeHtml(weeklyRangeText(data.period_start, data.period_end))}</strong></div>` +
        `<span>${data.locked ? '已于周六 00:00 结算并永久锁定' : '本周实时 · 每周六 00:00（北京时间）结算'}</span>` +
        `<select class="weekly-period-select" onchange="selectWeeklyPeriod(this.value)">` +
        renderWeeklyPeriodOptions(periodData.periods || [], selectedStart) + `</select></div>`
      if (!records.length) {
        html += `<div class="empty"><div class="icon">${isHost ? '🎤' : '💎'}</div>本周期暂无可核算票数</div>`
      } else {
        records.forEach((item, index) => {
          const display = item.display || item[isHost ? 'recipient_key' : 'sender_key'] || '?'
          // 神秘人解析出真名的，用括号带出来（和日榜一致）
          const realName = String(item.real_name || '').trim()
          const nameHtml = realName && realName !== display
            ? `${escapeHtml(display)}<span class="rank-real-name">（${escapeHtml(realName)}）</span>`
            : escapeHtml(display)
          html += `<div class="event weekly-rank-card"><span class="weekly-rank-no">#${Number(item.rank || index + 1)}</span>` +
            `<div class="weekly-rank-main"><strong>${isHost ? '' : levelBadgeHtml(item.consume_level)}${nameHtml}</strong>` +
            `<em>${Number(item.tickets || 0).toLocaleString('zh-CN')} 票</em>` +
            (isHost ? renderHostLargeDetails(item.large_details || []) : '') +
            `</div></div>`
        })
      }
      html += rankMoreHtml(
        isHost ? data.hosts_truncated : data.visitors_truncated,
        isHost ? data.host_total : data.visitor_total,
        'weekly'
      )
      container.innerHTML = html
      container.dataset.weeklyTag = weeklyTag
    })
    .catch(error => {
      if (requestSeq !== weeklyLoadSeq || currentView !== expectedView || analyticsRoomId() !== roomId) return
      container.dataset.weeklyTag = ''
      container.innerHTML = `<div class="empty"><div class="icon">⚠️</div>${escapeHtml(error.message)}</div>`
    })
}

function renderGiftAssignments() {
  const container = document.getElementById('events')
  const roomId = analyticsRoomId()
  if (!roomId) {
    giftAssignmentLoadSeq++
    container.innerHTML = '<div class="empty"><div class="icon">📝</div>请先监听并选择一个直播间</div>'
    return
  }
  const requestSeq = ++giftAssignmentLoadSeq
  container.innerHTML = '<div class="empty"><div class="icon">⏳</div>正在加载最近7天礼物...</div>'
  fetch('/api/admin/gift_assignments/' + encodeURIComponent(roomId))
    .then(r => r.json())
    .then(data => {
      if (requestSeq !== giftAssignmentLoadSeq || currentView !== 'giftAssignments' || analyticsRoomId() !== roomId) return
      if (!data.success) throw new Error(data.error || '归属修正列表加载失败')
      giftAssignmentCache = data.records || []
      giftAssignmentSearch = ''  // 新一次加载重置搜索
      // 头部(汇总 + 搜索框 + 切换)只渲染一次；下面的列表随搜索单独重绘，
      // 这样输入框不会因整块重绘而丢焦点。
      container.innerHTML =
        `<div class="assignment-summary"><b>📝 礼物归属修正</b>` +
        `<span>仅改变主持日榜和主持周榜；游客榜始终按原送礼人计算。原始礼物记录不会覆盖。</span></div>` +
        assignmentSearchBarHtml() +
        `<div id="giftAssignmentList"></div>`
      paintGiftAssignmentList()
    })
    .catch(error => {
      if (requestSeq !== giftAssignmentLoadSeq || currentView !== 'giftAssignments' || analyticsRoomId() !== roomId) return
      container.innerHTML = `<div class="empty"><div class="icon">⚠️</div>${escapeHtml(error.message)}</div>`
    })
}

function assignmentSearchBarHtml() {
  const on = k => (giftAssignmentSearchBy === k ? ' active' : '')
  return `<div class="assignment-search">` +
    `<input id="assignmentSearchInput" class="assignment-search-input" ` +
      `value="${escapeHtml(giftAssignmentSearch)}" placeholder="搜名字过滤…" ` +
      `oninput="onGiftAssignmentSearch(this.value)">` +
    `<div class="assignment-search-toggle">` +
      `<button id="asBySender" class="as-toggle-btn${on('sender')}" onclick="setGiftAssignmentSearchBy('sender')">刷的人</button>` +
      `<button id="asByRecipient" class="as-toggle-btn${on('recipient')}" onclick="setGiftAssignmentSearchBy('recipient')">收礼主持</button>` +
    `</div></div>`
}

function onGiftAssignmentSearch(value) {
  giftAssignmentSearch = value
  paintGiftAssignmentList()
}

function setGiftAssignmentSearchBy(by) {
  giftAssignmentSearchBy = by === 'recipient' ? 'recipient' : 'sender'
  const s = document.getElementById('asBySender')
  const r = document.getElementById('asByRecipient')
  if (s) s.className = 'as-toggle-btn' + (giftAssignmentSearchBy === 'sender' ? ' active' : '')
  if (r) r.className = 'as-toggle-btn' + (giftAssignmentSearchBy === 'recipient' ? ' active' : '')
  paintGiftAssignmentList()
}

function giftAssignmentMatches(item) {
  const term = giftAssignmentSearch.trim().toLowerCase()
  if (!term) return true
  const hay = giftAssignmentSearchBy === 'recipient'
    ? String(item.adjusted_recipient_name || item.original_recipient_name || '')
    : String(item.display || '')
  return hay.toLowerCase().includes(term)
}

function paintGiftAssignmentList() {
  const list = document.getElementById('giftAssignmentList')
  if (!list) return
  if (!giftAssignmentCache.length) {
    list.innerHTML = '<div class="empty"><div class="icon">🎁</div>本厅最近7天暂无礼物</div>'
    return
  }
  let html = ''
  let shown = 0
  // 用原始下标渲染每张卡，保证 saveGiftAssignment(index) 仍对得上缓存。
  giftAssignmentCache.forEach((item, index) => {
    if (!giftAssignmentMatches(item)) return
    shown++
    html += giftAssignmentCardHtml(item, index)
  })
  if (!shown) {
    const label = giftAssignmentSearchBy === 'recipient' ? '收礼主持' : '刷的人'
    list.innerHTML = `<div class="empty"><div class="icon">🔍</div>没有${label}匹配「${escapeHtml(giftAssignmentSearch.trim())}」的礼物</div>`
    return
  }
  list.innerHTML = html
}

function giftAssignmentCardHtml(item, index) {
  const ticketText = item.ticket_count == null
    ? '票数待补'
    : `${Number(item.ticket_count).toLocaleString('zh-CN')} 票`
  const recipientKey = item.adjusted_recipient_key || item.original_recipient_key || ''
  const recipientName = item.adjusted_recipient_name || item.original_recipient_name || ''
  return `<div class="event assignment-card"><div class="assignment-title"><strong>${escapeHtml(item.content || item.gift_id || '礼物')}</strong>` +
    `<em>${ticketText}</em></div>` +
    `<div class="assignment-meta">${escapeHtml(item.display || '?')} · 原收礼 ${escapeHtml(item.original_recipient_name || '大头/房间')} · ${formatDateTime(item.timestamp)}</div>` +
    `<div class="assignment-form"><label>主持标识<input id="assignment-key-${index}" value="${escapeHtml(recipientKey)}" placeholder="例如 host-sec-uid"></label>` +
    `<label>主持昵称<input id="assignment-name-${index}" value="${escapeHtml(recipientName)}" placeholder="例如 主持A"></label>` +
    `<label class="assignment-note">备注<input id="assignment-note-${index}" value="${escapeHtml(item.note || '')}" placeholder="必填，例如：刷错给大头"></label></div>` +
    `<div class="assignment-actions"><span id="assignment-status-${index}"></span><button id="assignment-save-${index}" onclick="saveGiftAssignment(${index})">保存归属</button></div></div>`
}

function saveGiftAssignment(index) {
  const item = giftAssignmentCache[index]
  if (!item) return Promise.resolve(false)
  const recipientKey = String(document.getElementById('assignment-key-' + index)?.value || '').trim()
  const recipientName = String(document.getElementById('assignment-name-' + index)?.value || '').trim()
  const note = String(document.getElementById('assignment-note-' + index)?.value || '').trim()
  const status = document.getElementById('assignment-status-' + index)
  const button = document.getElementById('assignment-save-' + index)
  if (!recipientKey || !recipientName || !note) {
    if (status) status.textContent = '主持标识、昵称和备注都不能为空'
    return Promise.resolve(false)
  }
  if (button) button.disabled = true
  if (status) status.textContent = '保存中...'
  return fetch('/api/admin/gift_assignments/' + encodeURIComponent(item.id), {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      recipient_key: recipientKey,
      recipient_name: recipientName,
      note: note
    })
  }).then(r => r.json()).then(data => {
    if (!data.success) throw new Error(data.error || '保存失败')
    giftAssignmentCache[index] = Object.assign({}, item, {
      adjusted_recipient_key: recipientKey,
      adjusted_recipient_name: recipientName,
      note: note
    })
    if (status) status.textContent = '已保存；主持日榜和未锁定周榜已按新归属计算'
    if (button) button.disabled = false
    return true
  }).catch(error => {
    if (status) status.textContent = error.message
    if (button) button.disabled = false
    return false
  })
}

// ========== 公屏模式 ==========

// 冻结阅读状态按房间隔离：frozen=用户已上翻，unseenCount=冻结后新增的去重事件数。
function feedViewState(roomId) {
  if (!feedViewStateByRoom[roomId]) {
    feedViewStateByRoom[roomId] = {frozen: false, unseenCount: 0}
  }
  return feedViewStateByRoom[roomId]
}

function resetFeedViewState(roomId) {
  if (!roomId) return
  feedViewStateByRoom[roomId] = {frozen: false, unseenCount: 0}
}

function isFeedFrozen(roomId) {
  const state = feedViewStateByRoom[roomId]
  return Boolean(state && state.frozen)
}

function isFeedAtBottom(container) {
  return container.scrollTop + container.clientHeight >= container.scrollHeight - FEED_BOTTOM_GAP
}

function updateFeedNewMessageButton() {
  const button = document.getElementById('feedNewMessageBtn')
  if (!button) return
  const state = selectedRoomId ? feedViewStateByRoom[selectedRoomId] : null
  if (currentView !== 'feed' || !state || !state.frozen || state.unseenCount <= 0) {
    button.style.display = 'none'
    return
  }
  button.textContent = '↓ ' + (state.unseenCount > 999 ? '999+' : state.unseenCount) + ' 条新消息'
  button.style.display = ''
}

// 冻结时把新增条数计入未读并刷新按钮，返回 true 表示这批消息已被冻结拦下。
// 实时事件、SSE init 重放、/api/feed 响应三条路径共用这一个出口。
function absorbWhileFrozen(roomId, added) {
  if (!isFeedFrozen(roomId)) return false
  feedViewState(roomId).unseenCount += added
  updateFeedNewMessageButton()
  return true
}

// 解除冻结：直接渲染该房最新 500 条并回到底部，未读数清零。
function jumpToLatestFeed(roomId) {
  const targetRoom = roomId || selectedRoomId
  if (!targetRoom) return
  resetFeedViewState(targetRoom)
  renderFeed(feedByRoom[targetRoom] || [], targetRoom)
  updateFeedNewMessageButton()
}

function onFeedScroll() {
  const container = document.getElementById('feedContainer')
  if (!container) return
  const roomId = container.dataset.roomId
  // 只有正在显示的房间才受滚动位置影响，别的厅不能被这次滚动改状态。
  if (!roomId || roomId !== String(selectedRoomId || '')) return
  if (isFeedAtBottom(container)) {
    if (isFeedFrozen(roomId)) jumpToLatestFeed(roomId)
    return
  }
  const state = feedViewState(roomId)
  if (state.frozen) return
  // 一离开底部就冻结，未读数从这一刻重新计。
  state.frozen = true
  state.unseenCount = 0
  updateFeedNewMessageButton()
}

// 历史下拉和房间筛选器这两个容器本身不会被重绘，各绑一次委托即可。
function bindDelegatedControls() {
  const dropdown = document.getElementById('historyDropdown')
  if (dropdown && !dropdown.dataset.clickBound) {
    dropdown.dataset.clickBound = '1'
    dropdown.addEventListener('click', event => {
      const item = event.target.closest('.item')
      if (item && item.dataset.rid) showRoom(item.dataset.rid)
    })
  }
  const selector = document.getElementById('roomSelector')
  if (selector && !selector.dataset.clickBound) {
    selector.dataset.clickBound = '1'
    selector.addEventListener('click', event => {
      const stop = event.target.closest('.room-stop-btn')
      if (stop) {
        event.stopPropagation()
        stopTemporaryRoom(stop.dataset.stopRid)
        return
      }
      const hide = event.target.closest('.room-hide-btn')
      if (hide) {
        event.stopPropagation()
        hideRoom(hide.dataset.hideRid)
        return
      }
      const unwatch = event.target.closest('.silence-unwatch-btn')
      if (unwatch) {
        event.stopPropagation()
        toggleSilenceWatch(unwatch.dataset.unwatchId)
        return
      }
      const bell = event.target.closest('[data-silence-id]')
      if (bell) {
        event.stopPropagation()
        // 铃铛只做一件事：打开这个厅的面板。订阅、退订、看记录都在面板里。
        // 早先是「已订阅就开记录、没订阅就直接订上」——后者零反馈，
        // 随手一点就订上了自己都不知道，实跑当天就踩了。
        openSilencePanel(bell.dataset.silenceId)
        return
      }
      const button = event.target.closest('.room-filter-btn')
      if (button) selectRoom(button.dataset.rid)
    })
  }
}

function bindFeedViewControls() {
  const container = document.getElementById('feedContainer')
  if (container && !container.dataset.scrollBound) {
    container.dataset.scrollBound = '1'
    container.addEventListener('scroll', onFeedScroll)
  }
  const button = document.getElementById('feedNewMessageBtn')
  if (button && !button.dataset.clickBound) {
    button.dataset.clickBound = '1'
    button.addEventListener('click', () => jumpToLatestFeed())
  }
}

// 该厅缓存里最新一条的时间戳；空缓存返回 0（表示要拉全量）。
// 必须按厅取：切厅时新厅缓存是空的，绝不能把上一个厅的时间戳带过去，
// 那会让新厅只拉到那一刻之后的几条、前面全缺。
function feedSinceFor(roomId) {
  const cached = feedByRoom[roomId] || []
  let newest = 0
  cached.forEach(ev => {
    const ts = Number(ev && ev.timestamp) || 0
    if (ts > newest) newest = ts
  })
  return newest
}

function loadFeed() {
  const container = document.getElementById('feedContainer')
  const roomId = selectedRoomId || Object.keys(currentRooms)[0] || null
  if (!roomId) {
    feedLoadSeq++
    container.innerHTML = '<div class="feed-empty"><div class="icon">🖥</div>开始监听后自动显示<br><span style="font-size:11px;color:#666">公屏时间线</span></div>'
    container.dataset.roomId = ''
    updateFeedNewMessageButton()
    return
  }

  // 冻结阅读时手动刷新公屏也只更新缓存，当前画面和 scrollTop 一律不动。
  const frozen = isFeedFrozen(roomId)
  const cached = feedByRoom[roomId] || []
  if (!frozen) {
    if (cached.length > 0) {
      renderFeed(cached, roomId, container.dataset.roomId === roomId)
    } else {
      // 先清掉上一个房间留下的 DOM，避免请求回来前看起来像“串厅”。
      container.innerHTML = '<div class="feed-empty"><div class="icon">🖥</div>正在加载该厅公屏...</div>'
      container.dataset.roomId = String(roomId)
    }
  }
  updateFeedNewMessageButton()

  const requestSeq = ++feedLoadSeq
  // 缓存里已经有内容就只拉增量。每次拉全量 200 条实测 15.7 KB，
  // 每 3 秒一次就是 452 MB/天/人——比它替换掉的弹幕推送还贵二三十倍。
  // 边界重叠的那几条由 mergeRoomFeed 按 feedEventKey 去重，多拉无害。
  const sinceTs = feedSinceFor(roomId)
  const url = '/api/feed/' + encodeURIComponent(roomId) + '?limit=' + FEED_MAX_ITEMS +
    (sinceTs ? '&since=' + sinceTs : '')
  fetch(url)
    .then(r => r.json())
    .then(data => {
      if (!data.success) throw new Error(data.error || '公屏加载失败')
      // 房间已停止时丢弃迟到响应；仍在监听的非当前房则只更新它自己的缓存。
      if (!currentRooms[roomId]) return
      const added = mergeRoomFeed(roomId, data.events || [])
      if (requestSeq !== feedLoadSeq || currentView !== 'feed' || selectedRoomId !== roomId) return
      if (absorbWhileFrozen(roomId, added)) return
      renderFeed(
        feedByRoom[roomId] || [],
        roomId,
        container.dataset.roomId === roomId
      )
    })
    .catch(err => {
      console.warn('[FEED] loadFeed error:', err)
      if (requestSeq !== feedLoadSeq || currentView !== 'feed' || selectedRoomId !== roomId) return
      // 接口失败保留当前 DOM、缓存和冻结状态，只在完全没内容时提示重试。
      if (isFeedFrozen(roomId)) return
      if (!feedByRoom[roomId] || feedByRoom[roomId].length === 0) {
        container.innerHTML = '<div class="feed-empty"><div class="icon">⚠️</div>该厅公屏加载失败，点击“公屏”重试</div>'
      }
    })
}

// 公屏只留发言和礼物。进场事件量最大又没信息量，不在这张表里就直接丢掉；
// 实时 SSE、init 重放、/api/feed 三条入口都走 normalizeFeedEvent，不必各自过滤。
// 注意：神秘人识别用的是 handleEvent 里的 mystery_enter 分支，不受影响。
const FEED_TYPE_MAP = {
  mystery_chat: 'chat', mystery_gift: 'gift',
  chat: 'chat', gift: 'gift'
}

function normalizeFeedEvent(event, fallbackRoomId) {
  const d = event.data || event
  const type = FEED_TYPE_MAP[event.type || d.type]
  if (!type) return null
  return {
    type,
    room_id: d.room_id || fallbackRoomId || '',
    sec_uid: d.sec_uid || '',
    display: d.display || '?',
    real_name: d.real_name || '',
    // 只按 UID 精确命中的真实身份；未命中时保持 null，不做任何猜测。
    matched_identity: d.matched_identity || null,
    content: type === 'gift' ? (d.gift_name || d.content || '?') : (d.content || ''),
    count: d.count || 1,
    gift_quantity: d.gift_quantity || d.count || 1,
    unit_diamonds: d.unit_diamonds,
    total_diamonds: d.total_diamonds,
    price_known: d.price_known,
    quantity_verified: d.quantity_verified,
    combo_key: d.combo_key || d.event_key || '',
    consume_level: Number(d.consume_level) || 0,
    recipient_name: d.recipient_name || '',
    timestamp: d.timestamp || Math.floor(Date.now() / 1000)
  }
}

function feedEventKey(ev) {
  if (ev.type === 'gift' && ev.combo_key) {
    return ['gift-combo', ev.room_id, ev.combo_key].join('|')
  }
  // 聊天优先用后端给的 event_key，跟礼物一个路子。
  // 不能按时间戳算：实时 SSE 事件的 timestamp 是浏览器 Date.now() 补的，
  // /api/feed 返回的却是服务器接收秒，两边差多少取决于时钟偏移和网络延迟。
  // 同一条消息因此被算成两条，公屏就重复了——这跟抖音重不重推无关，
  // 一次刷新和实时消息撞上就会发生。
  if (ev.type === 'chat' && ev.combo_key) {
    return ['chat', ev.room_id, ev.combo_key].join('|')
  }
  // 兜底：老缓存和没带键的事件仍按「显示名+秒级时间+内容」，
  // 不掺 sec_uid（匿名房同一人 sec_uid 每次进出都变）。
  if (ev.type === 'chat') {
    return ['chat', ev.room_id, ev.display, ev.timestamp, ev.content].join('|')
  }
  return [ev.type, ev.timestamp, ev.sec_uid, ev.display, ev.content, ev.count].join('|')
}

// 单条实时事件的快路径：时间不早于末条且从未出现过，直接尾部追加。
// 慢路径要把整份缓存 normalize + 建 Map + 全量排序，每条消息都做一遍太贵。
// 返回 null 表示没走成，需要回落慢路径（乱序、重复、礼物连击更新等）。
function appendNewestFeedEvent(roomId, event) {
  const cached = feedByRoom[roomId]
  if (!cached || cached.length === 0) return null
  const normalized = normalizeFeedEvent(event, roomId)
  if (!normalized) return null
  normalized.room_id = roomId
  const last = cached[cached.length - 1]
  if ((Number(normalized.timestamp) || 0) < (Number(last.timestamp) || 0)) return null
  const key = feedEventKey(normalized)
  if (cached.some(item => feedEventKey(item) === key)) return null
  cached.push(normalized)
  if (cached.length > FEED_MAX_ITEMS) cached.splice(0, cached.length - FEED_MAX_ITEMS)
  return 1
}

// 返回“相对合并前缓存真正新增的事件条数”，供冻结时的未读计数使用（去重后不重复计）。
function mergeRoomFeed(roomId, events, replace = false) {
  if (!replace && events && events.length === 1) {
    const fastAdded = appendNewestFeedEvent(roomId, events[0])
    if (fastAdded !== null) return fastAdded
  }
  const merged = replace ? [] : (feedByRoom[roomId] || []).slice()
  const byKey = new Map()
  const previousKeys = new Set()

  // replace 会整体换掉缓存，所以未读基准要单独按合并前的缓存记一份键。
  ;(feedByRoom[roomId] || []).forEach(ev => {
    const normalized = normalizeFeedEvent(ev, roomId)
    if (!normalized) return
    normalized.room_id = roomId
    previousKeys.add(feedEventKey(normalized))
  })

  merged.forEach(ev => {
    const normalized = normalizeFeedEvent(ev, roomId)
    if (!normalized) return
    normalized.room_id = roomId
    byKey.set(feedEventKey(normalized), normalized)
  })

  ;(events || []).forEach(event => {
    const normalized = normalizeFeedEvent(event, roomId)
    if (!normalized) return
    // 缓存桶本身就是隔离边界，接口或 SSE 中的脏 room_id 不得带进别的厅。
    normalized.room_id = roomId
    const key = feedEventKey(normalized)
    const existing = byKey.get(key)
    if (!existing) {
      byKey.set(key, normalized)
      return
    }
    if (normalized.type === 'gift' && normalized.combo_key) {
      byKey.set(key, normalized)
      return
    }
    // 同一事件优先保留已经还原出的demo_004。
    const existingResolved = existing.real_name && existing.real_name !== existing.display
    const incomingResolved = normalized.real_name && normalized.real_name !== normalized.display
    if (!existingResolved && incomingResolved) existing.real_name = normalized.real_name
    if (!existing.matched_identity && normalized.matched_identity) {
      existing.matched_identity = normalized.matched_identity
    }
  })

  const ordered = Array.from(byKey.values())
    .sort((a, b) => (Number(a.timestamp) || 0) - (Number(b.timestamp) || 0))
  // 先按裁剪前的全量算新增，避免冻结时新消息多于上限被少计。
  const added = ordered.reduce(
    (count, ev) => count + (previousKeys.has(feedEventKey(ev)) ? 0 : 1), 0
  )
  feedByRoom[roomId] = ordered.slice(-FEED_MAX_ITEMS)
  return added
}

function replaceRoomFeed(roomId, events) {
  return mergeRoomFeed(roomId, events, true)
}

function isMaskedDisplayName(name) {
  // 跟后端 is_real_mystery_user 用同一套定义，前端不另立标准，
  // 否则两边对「谁是神秘人」的判断会分叉。
  const s = String(name || '').trim()
  return (s.startsWith('神秘人') && s.length > 3) ||
         (s.startsWith('dou') && s.length > 5)
}

function feedDisplayName(ev) {
  const display = ev.display || '?'
  // 只给神秘人和匿名用户揭真名。普通用户改个昵称也会让「真名 ≠ 显示名」，
  // 那是曾用名不是马甲，公示出来纯属噪音（线上出现过 Moi-Nixxx（真实：Moi-Nixxxx）
  // 这种只差一个字符的误显示，就是同一个人自己改了名）。
  // 公屏有两个数据源：实时推送带 is_regular，从库里拉的那条不带，
  // 所以主判据用显示名形态，两个来源都覆盖得到。
  const masked = isMaskedDisplayName(display) || ev.is_regular === false
  if (!masked) return escapeHtml(display)
  // 本条消息自己解析出的真名优先；否则用 UID 精确命中的身份库真名。
  const realName = (ev.real_name && ev.real_name !== display)
    ? ev.real_name
    : (ev.matched_identity?.real_name || '')
  if (realName && realName !== display) {
    return `${escapeHtml(display)}（真实：${escapeHtml(realName)}）`
  }
  return escapeHtml(display)
}

// isNew 只给增量追加的那一条：整屏重绘时不该让所有条目重放淡入动画。
function feedItemHtml(ev, isNew = false) {
  const timeStr = formatTime(ev.timestamp)
  const name = levelBadgeHtml(ev.consume_level) + feedDisplayName(ev)
  const typeClass = ev.type === 'chat' ? 'feed-chat' : 'feed-gift'
  let icon, bodyHtml
  if (ev.type === 'chat') {
    icon = '💬'
    bodyHtml = `<span class="feed-name">${name}</span><span class="feed-content">: ${renderEmoji(escapeHtml(ev.content || ''))}</span>`
  } else {
    icon = '🎁'
    const giftName = renderEmoji(escapeHtml(ev.content || '?'))
    // 收礼人一直在数据里（SSE 与 /api/feed 两条路径都下发），只是以前
    // 没画出来，公屏上看不出这份礼物是送给谁的。空值时整段不显示，
    // 不留一个孤零零的箭头——送给大头的礼物服务端会填厅名，只有老数据
    // 可能为空。
    const recipient = String(ev.recipient_name || '').trim()
    const toHtml = recipient
      ? ` <span class="feed-recipient">→ ${renderEmoji(escapeHtml(recipient))}</span>`
      : ''
    bodyHtml = `<span class="feed-name">${name}</span> 送出 <span class="feed-count">${giftName} ${giftValueText(ev)}</span>${toHtml}`
  }
  return `<div class="feed-item ${typeClass}${isNew ? ' feed-new' : ''}" data-room="${escapeHtml(ev.room_id || '')}" data-ts="${ev.timestamp}">` +
    `<span class="feed-icon">${icon}</span>` +
    `<div class="feed-body">${bodyHtml}</div>` +
    `<span class="feed-time">${timeStr}</span></div>`
}

function renderFeed(events, roomId = selectedRoomId || '', preservePosition = false) {
  const container = document.getElementById('feedContainer')
  const sameRoom = container.dataset.roomId === String(roomId || '')
  const previousScrollTop = container.scrollTop
  const wasFollowing = isFeedAtBottom(container)
  if (!events || events.length === 0) {
    container.innerHTML = '<div class="feed-empty"><div class="icon">🖥</div>暂无互动记录</div>'
    container.dataset.roomId = String(roomId || '')
    return
  }
  // 注意别直接 .map(feedItemHtml)：map 会把下标当成 isNew 传进去。
  container.innerHTML = events.slice(-FEED_MAX_ITEMS).map(ev => feedItemHtml(ev)).join('')
  container.dataset.roomId = String(roomId || '')
  if (preservePosition && sameRoom && !wasFollowing) {
    const maxScrollTop = Math.max(0, container.scrollHeight - container.clientHeight)
    container.scrollTop = Math.min(previousScrollTop, maxScrollTop)
  } else {
    container.scrollTop = container.scrollHeight
  }
}

function appendFeedItem(event, fallbackRoomId) {
  try {
  const container = document.getElementById('feedContainer')
  if (!container) return

  const ev = normalizeFeedEvent(event, fallbackRoomId)
  if (!ev || !ev.room_id) return
  const roomId = fallbackRoomId || ev.room_id
  ev.room_id = roomId
  const eventKey = feedEventKey(ev)
  const added = mergeRoomFeed(roomId, [ev])
  const cached = feedByRoom[roomId] || []

  // 非当前房只缓存；切换房间时再从该房缓存完整渲染。
  if (currentView !== 'feed' || selectedRoomId !== roomId) return

  const state = feedViewState(roomId)
  // 用户已经翻离底部：即使这一帧还没收到 scroll 事件，也按冻结阅读处理。
  if (!state.frozen && !isFeedAtBottom(container)) {
    state.frozen = true
    state.unseenCount = 0
  }
  // 冻结阅读：只进缓存并累计未读，当前 DOM 和 scrollTop 一律不动。
  if (absorbWhileFrozen(roomId, added)) return

  // API / SSE 重叠投递的同一事件只显示一次。
  if (added === 0) return

  // 事件乱序到达时（新事件不是缓存里最后一条）才整屏重绘保排序；
  // 正常时间递增的情况只插一个节点，避免满 500 条后每条消息都全量重绘。
  if (cached.length === 0 || feedEventKey(cached[cached.length - 1]) !== eventKey) {
    renderFeed(cached, roomId)
    return
  }

  // 移除空状态占位
  if (container.querySelector('.feed-empty')) {
    container.innerHTML = ''
  }

  container.insertAdjacentHTML('beforeend', feedItemHtml(ev, true))
  container.dataset.roomId = String(roomId)

  // 超过上限时丢弃最早的
  while (container.children.length > FEED_MAX_ITEMS) {
    container.removeChild(container.firstChild)
  }

  container.scrollTop = container.scrollHeight
  } catch(e) { console.warn('[FEED] appendFeedItem error:', e) }
}

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  })
}

function formatDateTime(ts) {
  if (!ts) return ''
  return new Date(Number(ts) * 1000).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false
  })
}

// 点击名字展开/收起截断
function toggleName(secUid) {
  const card = document.querySelector(`[data-su="${secUid}"]`)
  if (card) {
    const nameEl = card.querySelector('.name-text')
    if (nameEl) nameEl.classList.toggle('exp')
  }
}

const listenerInput = document.getElementById('input')
if (listenerInput) {
  // 回车提交
  listenerInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') connect()
  })
  // 输入时动态切换按钮文字
  listenerInput.addEventListener('keyup', resetBtnText)
  listenerInput.addEventListener('blur', resetBtnText)

  // ========== 搜索历史下拉 ==========
  listenerInput.addEventListener('focus', function() {
    loadRoomHistory()
  })
  listenerInput.addEventListener('blur', function() {
    // 延迟隐藏，给点击 dropdown 留时间
    setTimeout(() => {
      const dropdown = document.getElementById('historyDropdown')
      if (dropdown) dropdown.style.display = 'none'
    }, 200)
  })
}

// 定时刷新活跃房间列表
setInterval(function () { if (pageIsVisible()) refreshRooms() }, 5000)
// 只读本机数据库、不碰抖音，所以刷得比后台同步更勤没有额外代价；
// 后台一拿到新数据，页面最多 2 秒就跟上。
setInterval(function refreshOnlinePage() {
  if (!pageIsVisible()) return
  if (currentView === 'all' && selectedRoomId) renderOnlineAudience(false)
  if (currentView === 'mystery' && selectedRoomId) renderOnlineAudience(true)
  if (currentView === 'daily' && selectedRoomId) renderDailyRanks()
}, 2000)
setInterval(function refreshAdminBoards() {
  if (!pageIsVisible()) return
  if ((currentView === 'hostWeekly' || currentView === 'visitorWeekly') && selectedRoomId) {
    renderWeeklyRanks(currentView)
  }
}, 30000)
// 普通观众的弹幕不再实时推送（后端 _PUSH_REGULAR_CHAT=False），公屏改为
// 停留在该视图时定时拉取；切走就不发请求。冻结阅读由 loadFeed 内部处理，
// 向上翻页时不会被打断。
// 挂在后台的标签页照样每 3 秒发一次请求，纯浪费。document.hidden 为真时
// 一律跳过；切回前台再立刻补一次，别让人对着过期画面。
function pageIsVisible() {
  return typeof document.hidden === 'undefined' ? true : !document.hidden
}

function refreshFeedPage() {
  if (!pageIsVisible()) return
  if (currentView === 'feed' && selectedRoomId) loadFeed()
}
setInterval(refreshFeedPage, 3000)

document.addEventListener('visibilitychange', function onVisible() {
  if (!pageIsVisible()) return
  refreshFeedPage()
  refreshRooms()
})

function refreshRooms() {
  return fetch('/api/status')
    .then(r => r.json())
    .then(data => {
      if (data.count > 0) {
        document.getElementById('statsText').textContent = `${data.count}/${data.max}房`
      } else {
        document.getElementById('statsText').textContent = '0房'
      }
      // 一个厅会在常驻与临时之间来回变（默认名单改了，或换了个号开的厅），
      // 而叉号是「停止监听」还是「收起」全看这个标记，不同步就会点错。
      // douyin_id 同理：静默铃铛靠它匹配订阅，没有就画不出来。
      // 只对齐页面上已有的厅：开厅是 startListening / 自动重连的事。
      const active = data.active || []
      let changed = false
      active.forEach(r => {
        const room = currentRooms[r.room_id]
        if (!room) return
        const temporary = r.is_temporary || false
        if (room.is_temporary !== temporary) {
          room.is_temporary = temporary
          changed = true
        }
        const douyinId = r.douyin_id || ''
        if (room.douyin_id !== douyinId) {
          room.douyin_id = douyinId
          changed = true
        }
      })
      if (changed) updateRoomSelector()
      loadSilenceAlerts()
    })
}

function stopRoom(roomId) {
  // 关闭SSE
  if (eventSources[roomId]) {
    eventSources[roomId].close()
    delete eventSources[roomId]
  }
  if (disconnectTimers[roomId]) {
    clearTimeout(disconnectTimers[roomId])
    delete disconnectTimers[roomId]
  }
  // 移除该房间的神秘人
  Object.keys(mysteries).forEach(secUid => {
    if (mysteries[secUid].room_id === roomId) {
      delete mysteries[secUid]
    }
  })
  delete feedByRoom[roomId]
  delete feedViewStateByRoom[roomId]
  delete onlineByRoom[roomId]
  delete currentRooms[roomId]
  if (selectedRoomId === roomId) {
    const remaining = Object.keys(currentRooms)
    selectedRoomId = remaining.length > 0 ? remaining[0] : null
    // 停掉的是当前房：接手的厅按“切厅”处理，未读数和冻结状态都不许带过去。
    resetFeedViewState(selectedRoomId)
    if (currentView === 'feed') loadFeed()
  }
  updateFeedNewMessageButton()
  updateRoomSelector()
  updateHeaderState()
  renderMysteries()
  return fetch('/api/stop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({room_id: roomId})
  }).then(() => {
    if (Object.keys(currentRooms).length === 0) {
      const btn = document.getElementById('btn')
      if (btn) {
        btn.textContent = '🔍 监听'
        btn.className = ''
      }
      lastRoomId = null
      setStatus('未连接', 'gray')
    }
    return refreshRooms()
  })
}

function stopAll() {
  // 关闭所有SSE
  Object.keys(eventSources).forEach(rid => {
    eventSources[rid].close()
  })
  eventSources = {}
  disconnectTimers = {}
  Object.keys(mysteries).forEach(k => delete mysteries[k])
  Object.keys(feedByRoom).forEach(k => delete feedByRoom[k])
  Object.keys(feedViewStateByRoom).forEach(k => delete feedViewStateByRoom[k])
  Object.keys(onlineByRoom).forEach(k => delete onlineByRoom[k])
  Object.keys(currentRooms).forEach(k => delete currentRooms[k])
  selectedRoomId = null
  if (currentView === 'feed') loadFeed()
  updateFeedNewMessageButton()
  updateRoomSelector()
  updateHeaderState()
  const btn = document.getElementById('btn')
  if (btn) {
    btn.textContent = '🔍 监听'
    btn.className = ''
  }
  lastRoomId = null
  setStatus('未连接', 'gray')
  document.getElementById('events').innerHTML = '<div class="empty"><div class="icon">🎯</div>已停止</div>'
  return fetch('/api/stop_all', {method: 'POST'}).then(() => refreshRooms())
}

// 页面加载时：绑定内容折叠、公屏滚动/回到最新 + 加载抖音表情映射 + 检测是否已有监听中的房间
bindDataPanelControls()
bindFeedViewControls()
bindDelegatedControls()
// 面板上的点击：关闭 / 看记录 / 开关监测
document.getElementById('silencePanel')?.addEventListener('click', event => {
  if (event.target.closest('[data-silence-close]')) return closeSilencePanel()
  const records = event.target.closest('[data-silence-records]')
  if (records) {
    const douyinId = records.dataset.silenceRecords
    const rid = Object.keys(currentRooms).find(
      k => currentRooms[k] && currentRooms[k].douyin_id === douyinId)
    closeSilencePanel()
    if (rid) {
      // 先选厅（标题和 selectedRoomId 跟着走），再由 switchMode 统一
      // 处理面板显隐和按钮状态。直接设 currentView 会绕过面板切换，
      // 从公屏进来时记录会渲染进隐藏的 #events、屏幕上还留着公屏。
      selectedRoomId = rid
      switchMode('silence')
    }
    return
  }
  const toggle = event.target.closest('[data-silence-toggle]')
  if (toggle) toggleSilenceWatch(toggle.dataset.silenceToggle)
})
loadEmojiMap()
// 静默提醒依赖浏览器通知权限，页面一加载就问一次（拒绝过的浏览器不会再弹）。
if (isAdminUser() && typeof Notification !== 'undefined'
    && Notification.permission === 'default') {
  Notification.requestPermission()
}
loadSilenceWatches().then(loadSilenceAlerts)
;(function autoReconnectOnLoad() {
  fetch('/api/status')
    .then(r => r.json())
    .then(data => {
      if (data.count > 0) {
        data.active.forEach(r => {
          currentRooms[r.room_id] = {nickname: r.nickname || r.room_id,
                                     is_temporary: r.is_temporary || false,
                                     douyin_id: r.douyin_id || ''}
          connectSSE(r.room_id)
        })
        updateRoomSelector()
        updateHeaderState()
        if (data.active.length > 0) selectRoom(data.active[0].room_id)
        lastRoomId = data.active[0].room_id
        const btn = document.getElementById('btn')
        if (btn) {
          btn.textContent = '停止'
          btn.className = 'stop-btn'
        }
        setStatus('监听中', 'green')
        document.getElementById('events').innerHTML = '<div class="empty"><div class="icon">🎯</div>正在同步当前在线神秘人...</div>'
      }
    })
    .catch(() => {})
})()

/* ═══ Anime.js 动画增强 ═══ */
const { animate, stagger, spring } = anime;

/* ---- 状态圆点呼吸 ---- */
(function dotPulse(){
  const dot = document.getElementById('dot');
  let anim = null;
  const obs = new MutationObserver(() => {
    if(anim) { anim.pause(); anim = null; }
    if(dot.classList.contains('green')){
      anim = animate(dot, {
        boxShadow: ['0 0 4px 2px rgba(52,199,89,0.3), inset 0 1px 2px rgba(255,255,255,.3)', '0 0 12px 4px rgba(52,199,89,0.5), inset 0 1px 2px rgba(255,255,255,.3)'],
        duration: 1500,
        loop: true,
        ease: 'inOutSine',
      });
    } else if(dot.classList.contains('red')){
      anim = animate(dot, {
        boxShadow: ['0 0 4px 2px rgba(255,59,48,0.3), inset 0 1px 2px rgba(255,255,255,.3)', '0 0 12px 4px rgba(255,59,48,0.5), inset 0 1px 2px rgba(255,255,255,.3)'],
        duration: 1200,
        loop: true,
        ease: 'inOutSine',
      });
    } else {
      dot.style.boxShadow = 'inset 0 1px 2px rgba(255,255,255,.15)';
    }
  });
  obs.observe(dot, { attributes: true, attributeFilter: ['class'] });
})();

/* ---- 按钮监听光晕 ---- */
(function btnGlow(){
  const btn = document.getElementById('btn');
  if (!btn) return;
  let glow = null;
  const obs = new MutationObserver(() => {
    if(glow) { glow.pause(); glow = null; }
    if(btn.classList.contains('stop-btn')){
      btn.style.transition = 'box-shadow .3s';
      glow = animate(btn, {
        boxShadow: ['0 0 0 0 rgba(255,107,53,0)', '0 0 12px 3px rgba(255,107,53,0.25)'],
        duration: 2000,
        loop: true,
        ease: 'inOutSine',
      });
    } else {
      btn.style.boxShadow = 'none';
    }
  });
  obs.observe(btn, { attributes: true, attributeFilter: ['class'] });
})();

/* ---- Hook renderMysteries 添加卡片动画 ---- */
const origRender = renderMysteries;
renderMysteries = function(){
  origRender();
  const cards = document.querySelectorAll('.events > .event');
  cards.forEach(c => { c.style.opacity = '0'; c.style.transform = 'translateY(8px)'; });
  animate(cards, {
    opacity: [0,1],
    translateY: [8,0],
    duration: 400,
    delay: stagger(60),
    ease: 'outCubic',
  });
};

/* ---- 模式切换卡片动画 ---- */
const origSwitch = switchMode;
switchMode = function(mode){
  origSwitch(mode);
  // 切换后卡片已有新内容，动画在 render 函数里已触发
};

// 页面加载时预搜索历史缓存
refreshHistoryCache();
