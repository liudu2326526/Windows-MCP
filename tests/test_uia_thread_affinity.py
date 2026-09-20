"""UI Automation clients stay inside the COM apartment that created them."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sys
import threading

import pytest


if sys.platform != "win32":
    pytest.skip("Windows UI Automation is required", allow_module_level=True)

from windows_mcp.uia.core import _AutomationClient  # noqa: E402


def test_automation_client_is_thread_local(monkeypatch: pytest.MonkeyPatch) -> None:
    created_on: list[int] = []
    monkeypatch.setattr(_AutomationClient, "_thread_local", threading.local())
    monkeypatch.setattr(
        _AutomationClient,
        "__init__",
        lambda self: created_on.append(threading.get_ident()),
    )

    main_client = _AutomationClient.instance()
    assert _AutomationClient.instance() is main_client

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker_client = executor.submit(_AutomationClient.instance).result()
        same_worker_client = executor.submit(_AutomationClient.instance).result()

    assert worker_client is same_worker_client
    assert worker_client is not main_client
    assert len(created_on) == 2
