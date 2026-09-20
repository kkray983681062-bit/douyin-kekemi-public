"""统一注入安全响应头。

原本一个都没有。最实际的风险是**点击劫持**：别人把 kekemi 用 iframe 套进
自己的页面，诱导已登录的 super_admin 点下去，就能触发「停止监听」「改角色」
这类操作 —— 而停监听期间缺失的互动记录是补不回来的。

CSP 同时是纵深防御：万一哪天有个 XSS 漏过转义，它能挡住外连和内联脚本。
"""

# 页面只加载自己的资源；字体走 Google Fonts（登录页在用）。
# frame-ancestors 'none' 是点击劫持的正解，比 X-Frame-Options 更严格，
# 但老浏览器只认后者，所以两个都发。
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    # 头像是抖音实时接口给的 CDN 地址，域名在 p3/p6/p9/p11/p26… 之间轮换，
    # 枚举白名单必然漏、漏一个就一片破图。放开到任意 HTTPS 图片：
    # 仍然禁掉明文 http 与其它协议，而 CSP 在这里的主要价值本来就是
    # frame-ancestors（防点击劫持）和 script-src，不是图片来源。
    "img-src 'self' data: https:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])

HEADERS = {
    'X-Frame-Options': 'DENY',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Content-Security-Policy': CONTENT_SECURITY_POLICY,
    # 浏览器记住「这个站只走 HTTPS」；Railway 本来就只提供 HTTPS。
    'Strict-Transport-Security': 'max-age=31536000',
}


def apply_security_headers(response):
    for name, value in HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def install(app):
    app.after_request(apply_security_headers)
    return app
