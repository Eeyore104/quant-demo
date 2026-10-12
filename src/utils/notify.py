"""通知通道：企业微信 webhook（v1.4 锁定方案；标准库 urllib，零新增依赖）。

配置（优先级从高到低）：
1. 环境变量 ``WECOM_WEBHOOK_URL``
2. ``config/config.local.yaml`` 的 ``notify.wecom_webhook_url``（不进 git）
3. 缺省：降级为日志（返回 False，不抛异常）

用法：
    from src.utils.notify import build_daily_summary, build_failure_alert, send_markdown
    send_markdown(build_daily_summary(date_str="2026-10-12", lines=[...]))
"""

from __future__ import annotations

import json
import urllib.request

from .config_loader import get_webhook_url
from .logger import get_logger

log = get_logger("notify")


def send_markdown(text: str, webhook_url: str | None = None, *, timeout: float = 10.0) -> bool:
    """发送 markdown 消息到企业微信 webhook；返回是否成功（失败降级为日志）。

    - 未配置 URL：跳过并记日志（返回 False）；
    - 网络失败：重试 1 次后放弃（返回 False）；
    - 业务错误（errcode != 0）：不重试（返回 False）。
    """
    url = webhook_url or get_webhook_url()
    if not url:
        log.info("未配置企业微信 webhook（WECOM_WEBHOOK_URL / config.local.yaml），通知跳过")
        return False

    payload = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": text}}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    for attempt in (1, 2):  # 网络类失败重试 1 次
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001 — 通知失败不阻塞主流程
            if attempt == 2:
                log.warning("企业微信通知发送失败（已重试 1 次）：%s", exc)
                return False
            continue
        if body.get("errcode") == 0:
            log.info("企业微信通知已发送")
            return True
        log.warning("企业微信通知被拒绝（errcode=%s）：%s", body.get("errcode"), body.get("errmsg"))
        return False
    return False


def build_daily_summary(*, date_str: str, lines: list[str], title: str = "每日跑批") -> str:
    """成功摘要消息（企业微信 markdown）。"""
    body = "\n".join(f"> {line}" for line in lines)
    return f"**quant-demo · {title}**（{date_str}）\n{body}"


def build_failure_alert(*, date_str: str, step: str, error: str) -> str:
    """失败告警消息（企业微信 markdown）。"""
    return (
        f"**quant-demo · 跑批失败**（{date_str}）\n"
        f"> 步骤：{step}\n"
        f"> 错误：{error}"
    )
