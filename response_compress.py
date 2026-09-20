"""响应 gzip 压缩：把出网流量降到约六分之一。

背景：日榜响应实测 35.2 KB，前端每 2 秒刷一次，等于每个查看者 ~17.6 KB/s。
50 人同时看约 750 GB/月（Railway 出网 $0.05/GB ≈ $37/月）；压缩后约 $5/月。

只用标准库，不引依赖。

**必须挡住流式响应**：公屏走 SSE（text/event-stream），是一条不会结束的流。
压缩要先读完整个 body，对无限流意味着永远读不完——会直接把公屏挂死。
"""

import gzip

from flask import request

# 值得压的类型。二进制（图片、字体、已压缩包）压了只会更大。
COMPRESSIBLE_TYPES = frozenset({
    'application/json',
    'application/javascript',
    'text/javascript',
    'text/html',
    'text/css',
    'text/plain',
    'text/xml',
    'application/xml',
    'image/svg+xml',
})

# 小响应压了不划算：gzip 头本身就有二十来字节，且多花一次 CPU。
MIN_SIZE_BYTES = 500

COMPRESS_LEVEL = 6  # 6 是体积与 CPU 的常规折中


def _should_compress(response):
    # 流式响应绝不能碰——读它的 body 会把 SSE 无限流缓冲住。
    if response.direct_passthrough or response.is_streamed:
        return False
    if response.status_code < 200 or response.status_code >= 300:
        return False
    if response.headers.get('Content-Encoding'):
        return False
    content_type = (response.content_type or '').split(';')[0].strip().lower()
    if content_type not in COMPRESSIBLE_TYPES:
        return False
    if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
        return False
    return True


def compress_response(response):
    if not _should_compress(response):
        return response
    data = response.get_data()
    if len(data) < MIN_SIZE_BYTES:
        return response
    compressed = gzip.compress(data, COMPRESS_LEVEL)
    if len(compressed) >= len(data):
        return response          # 压不动就原样发
    response.set_data(compressed)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Length'] = str(len(compressed))
    # 中间缓存必须按 Accept-Encoding 区分，否则会把压缩版发给不支持的客户端。
    response.headers.add('Vary', 'Accept-Encoding')
    return response


def install(app):
    app.after_request(compress_response)
    return app
