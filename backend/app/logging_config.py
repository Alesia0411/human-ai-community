# 文件职责：在服务日志过滤 WebSocket 一次性票据，避免查询参数泄露登录能力。
"""Uvicorn logs websocket URLs even with access_log=False; strip ticket queries."""

import logging
import re


# RedactSocketTicket：日志过滤器：将 WebSocket URL 中票据替换为脱敏值。
class RedactSocketTicket(logging.Filter):
    # filter：处理日志消息或参数中的票据，并允许脱敏后的记录继续输出。
    def filter(self, record):
        message = record.getMessage()
        if "/ws?" in message:
            record.msg = re.sub(r"/ws\?[^\s\"']+", "/ws?[redacted]", message)
            record.args = ()
        return True


# install：把票据脱敏过滤器挂到相关服务日志入口。
def install():
    for name in ("uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactSocketTicket) for f in logger.filters):
            logger.addFilter(RedactSocketTicket())
