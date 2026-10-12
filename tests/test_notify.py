"""通知模块测试：webhook 组装 / 失败降级 / 未配置跳过 / 消息模板。"""

import json

from src.utils import notify


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_send_skips_without_url(monkeypatch):
    monkeypatch.setattr(notify, "get_webhook_url", lambda: None)
    assert notify.send_markdown("hi") is False


def test_send_success_payload(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=10.0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({"errcode": 0, "errmsg": "ok"})

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    ok = notify.send_markdown("hello", webhook_url="https://example.com/hook")
    assert ok is True
    assert captured["url"] == "https://example.com/hook"
    assert captured["body"]["msgtype"] == "markdown"
    assert captured["body"]["markdown"]["content"] == "hello"


def test_send_network_failure_retries_once_then_false(monkeypatch):
    calls = {"n": 0}

    def boom(req, timeout=10.0):
        calls["n"] += 1
        raise OSError("network down")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.send_markdown("x", webhook_url="https://example.com") is False
    assert calls["n"] == 2  # 重试 1 次


def test_send_business_error_not_retried(monkeypatch):
    calls = {"n": 0}

    def fake(req, timeout=10.0):
        calls["n"] += 1
        return _FakeResp({"errcode": 93000, "errmsg": "invalid key"})

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake)
    assert notify.send_markdown("x", webhook_url="https://example.com") is False
    assert calls["n"] == 1


def test_templates_contain_key_fields():
    summary = notify.build_daily_summary(
        date_str="2026-10-12", lines=["数据更新：8 品种", "组合收益 +1.23%"]
    )
    assert "2026-10-12" in summary
    assert "组合收益" in summary
    alert = notify.build_failure_alert(
        date_str="2026-10-12", step="数据更新", error="akshare 超时"
    )
    assert "失败" in alert
    assert "akshare" in alert
