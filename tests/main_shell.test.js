const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const ROOT = path.join(__dirname, '..')
const read = relativePath => fs.readFileSync(path.join(ROOT, relativePath), 'utf8')

test('main shell layers the approved right-side artwork below the unchanged data container', () => {
  const html = read('templates/index.html')
  const css = read('static/style.css')

  const decorAt = html.indexOf('class="kekemi-decor"')
  const containerAt = html.indexOf('class="container"')
  assert.ok(decorAt >= 0 && decorAt < containerAt)
  assert.match(html, /kekemi-moon\.webp/)
  assert.match(html, /kekemi-water\.webp/)
  assert.match(html, /kekemi-butterfly\.webp/)
  assert.match(html, /class="kekemi-decor-cluster"[\s\S]*kekemi-moon\.webp[\s\S]*kekemi-butterfly\.webp[\s\S]*<\/div>/)
  assert.match(html, />克克咪</)
  assert.match(html, />KE·KE·MI</)
  assert.match(html, /aria-hidden="true"/)

  assert.match(css, /\.kekemi-decor\s*\{[^}]*position:\s*fixed[^}]*pointer-events:\s*none/s)
  assert.match(css, /\.kekemi-decor-butterfly\s*\{[^}]*left:\s*50%[^}]*top:\s*7[0-9]%/s)
  assert.match(css, /\.kekemi-decor-water\s*\{[^}]*top:\s*33%/s)
  assert.match(css, /\.container\s*\{max-width:600px;margin:0 auto;position:relative;z-index:0\}/)
  assert.match(css, /@media\s*\(max-width:\s*480px\)[\s\S]*\.kekemi-decor/)
  assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*animation:\s*none/)
})

test('main shell has one compact collapse row and no anonymous-room banner UI', () => {
  const html = read('templates/index.html')
  const css = read('static/style.css')
  const app = read('static/app.js')

  assert.match(html, /id="dataPanel"/)
  assert.match(html, /id="dataPanelBody"/)
  assert.match(html, /id="dataPanelToggle"/)
  assert.match(html, /id="dataPanelTitle"/)
  assert.match(css, /\.data-panel-toggle-row\s*\{[^}]*justify-content:\s*center/s)
  assert.match(css, /\.data-panel-toggle-row\s*\{[^}]*max-width:\s*430px/s)
  assert.match(css, /\.header-main\s*\{[^}]*max-width:\s*430px/s)
  assert.match(css, /\.data-panel\.is-collapsed\s+\.data-panel-body/)

  assert.doesNotMatch(html, /anonymousBanner|anonymousMsg|anonymous-banner/)
  assert.doesNotMatch(app, /ANONYMOUS_ROOM_BANNER|showAnonymousBanner|anonymousBanner|anonymousMsg/)
  assert.doesNotMatch(css, /anonymous-banner/)
  assert.doesNotMatch(html + app, /当前直播间为匿名模式|继续同步在线名单并记录公屏互动/)
})

test('main stylesheet cache key advances for the new shell assets', () => {
  const html = read('templates/index.html')
  // 新外壳（430px 顶栏 + 装饰层）随 style.css?v=22 上线；
  // 断言下限而非精确值，后续正常 bump 版本号时本测试不应跟着碎。
  const match = html.match(/\/static\/style\.css\?v=(\d+)/)
  assert.ok(match, 'index.html 应引用带 ?v= 的 style.css')
  assert.ok(Number(match[1]) >= 22, `style.css 缓存键应不低于 v22，实际 v${match[1]}`)
})
