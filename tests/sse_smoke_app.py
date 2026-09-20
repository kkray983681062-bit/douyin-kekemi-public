"""SSE 多订阅者冒烟入口（仅容器内验证用，不对外部署）。

gunicorn 加载本模块：注入一个假监听器，每 0.5 秒广播一条事件。
两个并发 SSE 客户端连同一个厅，都收到相同事件序列即通过——
这是「多人同看一个厅」在真实 gunicorn gthread 环境下的端到端证明。
"""
import threading
import time

import web_listener

_listener = web_listener.RoomListener.__new__(web_listener.RoomListener)
_listener.room_id = 'smoke'
_listener.nickname = '冒烟厅'
_listener.sec_uid = ''
_listener.running = True
_listener.is_private = False
_listener.mystery_count = 0
_listener.recent_mysteries = []
web_listener._init_listener_subscribers(_listener)
web_listener.listeners['smoke'] = _listener


def _pump():
    index = 0
    while True:
        time.sleep(0.5)
        index += 1
        web_listener.publish_event(_listener, {
            'type': 'chat',
            'data': {'room_id': 'smoke', 'display': '冒烟', 'content': f'第{index}条',
                     'timestamp': int(time.time())},
        })


threading.Thread(target=_pump, daemon=True).start()

app = web_listener.app
