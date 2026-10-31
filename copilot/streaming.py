"""run 事件总线：执行线程 → SSE 订阅者（进程内，无中间件）。

为什么不用 Redis/Channel Layer：部署形态是单进程 + SQLite（见开发方案 §3.2），
加一个 broker 的运维成本远大于收益。代价写在明处：**多进程部署时 SSE 只能
拿到本进程的事件**——所以前端必须保留轮询降级（panel.js 里就是这样做的）。

队列有上限：订阅端（浏览器）掉线或太慢时宁可丢 token 事件，也绝不能阻塞
执行线程——否则一次断网就会把 2 个 worker 里的 1 个永久占住。
"""

import json
import queue
import threading

MAX_EVENTS = 300


class Channel:
    def __init__(self):
        self.queue = queue.Queue(maxsize=MAX_EVENTS)
        self.closed = False

    def put(self, event, data):
        if self.closed:
            return
        try:
            self.queue.put_nowait((event, data))
        except queue.Full:
            pass      # 订阅端跟不上：丢事件，执行照常

    def close(self):
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass


_LOCK = threading.Lock()
_CHANNELS = {}


def channel(run_id):
    key = str(run_id)
    with _LOCK:
        found = _CHANNELS.get(key)
        if found is None:
            found = Channel()
            _CHANNELS[key] = found
        return found


def publish(run_id, event, data):
    """推一条事件；没有订阅者时事件留在队列里，等订阅者连上再读。"""
    ch = channel(run_id)
    ch.put(event, data)
    if event in ('done', 'error'):
        ch.close()
        with _LOCK:
            _CHANNELS.pop(str(run_id), None)


def subscribe(run_id):
    """订阅一个 run 的事件；返回 (channel, unsubscribe)。"""
    return channel(run_id), lambda: None


def sse(event, data):
    """按 SSE 协议编码：事件名 + JSON 数据（多行内容由 json 转义掉）。"""
    body = json.dumps(data, ensure_ascii=False, default=str)
    return f'event: {event}\ndata: {body}\n\n'
