"""配置加载测试：本地覆盖合并 / webhook URL 解析优先级。"""

from src.utils.config_loader import _deep_merge, get_webhook_url, load_config


def test_deep_merge_keeps_unrelated_and_overrides():
    base = {"a": 1, "notify": {"enabled": True, "on_success": True}, "x": {"y": 1}}
    override = {"notify": {"wecom_webhook_url": "u"}, "x": 5}
    out = _deep_merge(base, override)
    assert out["notify"]["enabled"] is True  # 未覆盖的保留
    assert out["notify"]["wecom_webhook_url"] == "u"  # 新增键合并
    assert out["x"] == 5  # 非 dict 直接覆盖
    assert out["a"] == 1


def test_load_config_has_v14_sections():
    cfg = load_config()
    assert "storage" in cfg
    assert "notify" in cfg


def test_webhook_env_wins(monkeypatch):
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://env")
    assert get_webhook_url({"notify": {"wecom_webhook_url": "https://cfg"}}) == "https://env"


def test_webhook_from_cfg(monkeypatch):
    monkeypatch.delenv("WECOM_WEBHOOK_URL", raising=False)
    assert get_webhook_url({"notify": {"wecom_webhook_url": "https://cfg"}}) == "https://cfg"


def test_webhook_missing_returns_none(monkeypatch):
    monkeypatch.delenv("WECOM_WEBHOOK_URL", raising=False)
    assert get_webhook_url({}) is None
    assert get_webhook_url({"notify": {}}) is None
