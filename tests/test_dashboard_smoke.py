"""看板冒烟：未安装 streamlit（可选依赖组）时自动跳过，不阻塞主链路。"""

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("streamlit")


def test_dashboard_app_runs_without_exception():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception
