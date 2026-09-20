from __future__ import annotations

from collections import OrderedDict
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

if sys.platform == "win32":
    from windows_mcp.desktop import service
    from windows_mcp.desktop.service import Desktop
    from windows_mcp.desktop.views import (
        DesktopState,
        ForegroundWindowIdentity,
        Status,
        Window,
    )
    from windows_mcp.tree.service import Tree
    from windows_mcp.tree.views import BoundingBox, Center, TreeElementNode, TreeState
else:
    pytestmark = pytest.mark.skip(reason="Snapshot identity tests require Windows UI Automation")


def _node(
    *,
    center: tuple[int, int] = (10, 20),
    runtime_id: list[int] | None = None,
    automation_id: str | None = None,
    window_handle: int = 100,
) -> TreeElementNode:
    x, y = center
    metadata = {
        "window_handle": window_handle,
        "window_process_id": 200,
        "control_type_id": 50000,
        "is_enabled": True,
        "is_offscreen": False,
    }
    if runtime_id is not None:
        metadata["runtime_id"] = runtime_id
    if automation_id is not None:
        metadata["automation_id"] = automation_id
    return TreeElementNode(
        bounding_box=BoundingBox(
            left=x - 5,
            top=y - 5,
            right=x + 5,
            bottom=y + 5,
            width=10,
            height=10,
        ),
        center=Center(x=x, y=y),
        name="Save",
        control_type="Button",
        window_name="Editor",
        metadata=metadata,
    )


def _state(snapshot_id: str, node: TreeElementNode | None = None) -> DesktopState:
    return DesktopState(
        active_desktop={},
        all_desktops=[],
        active_window=None,
        windows=[],
        tree_state=TreeState(interactive_nodes=[] if node is None else [node]),
        snapshot_id=snapshot_id,
        foreground_handle=100,
        foreground_root_handle=100,
        foreground_process_id=200,
    )


def _desktop(state: DesktopState) -> Desktop:
    desktop = Desktop.__new__(Desktop)
    desktop.desktop_state = state
    desktop._snapshots = OrderedDict([(state.snapshot_id, state)])
    desktop._snapshot_pins = {}
    desktop._state_generation = 0
    desktop.get_foreground_identity = lambda: (100, 200)

    def get_foreground_window_identity() -> ForegroundWindowIdentity:
        raw_handle, process_id = desktop.get_foreground_identity()
        return ForegroundWindowIdentity(
            raw_handle=raw_handle,
            root_handle=state.foreground_root_handle or raw_handle,
            process_id=process_id,
            title="Editor",
            process_name="editor.exe",
        )

    desktop.get_foreground_window_identity = get_foreground_window_identity
    return desktop


@pytest.mark.parametrize("identity", [(101, 200), (100, 201)])
def test_require_snapshot_rejects_changed_foreground(identity: tuple[int, int]) -> None:
    state = _state("snap-1")
    desktop = _desktop(state)
    desktop.get_foreground_identity = lambda: identity

    with pytest.raises(ValueError, match="STALE_STATE"):
        desktop.require_snapshot("snap-1")


def test_require_snapshot_rejects_snapshot_without_stable_foreground() -> None:
    state = _state("snap-1")
    state.foreground_handle = None
    state.foreground_root_handle = None
    state.foreground_process_id = None
    desktop = _desktop(state)

    with pytest.raises(ValueError, match="stable foreground identity"):
        desktop.require_snapshot("snap-1")


def test_require_snapshot_rejects_changed_uia_root() -> None:
    desktop = _desktop(_state("snap-1"))
    desktop.get_foreground_window_identity = lambda: ForegroundWindowIdentity(
        raw_handle=100,
        root_handle=101,
        process_id=200,
        title="Editor",
        process_name="editor.exe",
    )

    with pytest.raises(ValueError, match="STALE_STATE"):
        desktop.require_snapshot("snap-1")


def test_require_screenshot_snapshot_uses_raw_identity_without_uia_lookup() -> None:
    state = _state("snap-1")
    state.foreground_root_handle = None
    desktop = _desktop(state)
    desktop.get_foreground_window_identity = MagicMock(
        side_effect=AssertionError("screenshot fencing must stay on the Win32 fast path")
    )

    assert desktop.require_snapshot("snap-1") is state
    desktop.get_foreground_window_identity.assert_not_called()


def test_observation_can_capture_without_foreground_identity() -> None:
    desktop = _desktop(_state("snap-1"))
    desktop.get_foreground_identity = MagicMock(side_effect=ValueError("focus transition"))

    assert desktop._capture_foreground_identity() is None


def test_live_foreground_identity_checks_raw_handle_before_and_after_uia_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = _desktop(_state("snap-1"))
    identities = iter([(100, 200), (101, 200)])
    desktop.get_foreground_identity = lambda: next(identities)
    root = MagicMock(NativeWindowHandle=100, ProcessId=200, Name="Editor")
    desktop.get_window_from_element_handle = MagicMock(return_value=root)
    monkeypatch.setattr(service.win32gui, "IsWindow", lambda _handle: True)

    with pytest.raises(ValueError, match="changed while reading"):
        Desktop.get_foreground_window_identity(desktop)


def test_live_foreground_identity_returns_title_process_and_uia_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = _desktop(_state("snap-1"))
    root = MagicMock(NativeWindowHandle=55, ProcessId=200, Name="Editor - project.txt")
    desktop.get_window_from_element_handle = MagicMock(return_value=root)
    monkeypatch.setattr(service.win32gui, "IsWindow", lambda _handle: True)
    process = MagicMock()
    process.name.return_value = "editor.exe"
    monkeypatch.setattr(service, "Process", lambda _pid: process)

    identity = Desktop.get_foreground_window_identity(desktop)

    assert identity == ForegroundWindowIdentity(
        raw_handle=100,
        root_handle=55,
        process_id=200,
        title="Editor - project.txt",
        process_name="editor.exe",
    )


def test_snapshot_identity_rejects_a_b_a_foreground_switch() -> None:
    a = ForegroundWindowIdentity(100, 55, 200, "Editor", "editor.exe")
    b = ForegroundWindowIdentity(101, 56, 201, "Settings", "settings.exe")

    stable = Desktop._stable_snapshot_identity(
        [a, b, a],
        uia_root_handle=55,
        uia_root_process_id=200,
    )

    assert stable is None


@pytest.mark.parametrize(
    ("root_handle", "root_process_id"),
    [(56, 200), (55, 201)],
)
def test_snapshot_identity_rejects_captured_uia_root_mismatch(
    root_handle: int,
    root_process_id: int,
) -> None:
    identity = ForegroundWindowIdentity(100, 55, 200, "Editor", "editor.exe")

    stable = Desktop._stable_snapshot_identity(
        [identity, identity, identity],
        uia_root_handle=root_handle,
        uia_root_process_id=root_process_id,
    )

    assert stable is None


def test_snapshot_identity_requires_the_foreground_root_in_a_uia_capture() -> None:
    identity = ForegroundWindowIdentity(100, 55, 200, "Editor", "editor.exe")

    stable = Desktop._stable_snapshot_identity(
        [identity, identity, identity],
        uia_root_handle=None,
        uia_root_process_id=None,
        require_uia_root=True,
    )

    assert stable is None


def test_pinned_snapshot_survives_history_pruning() -> None:
    desktop = _desktop(_state("snap-source"))
    desktop.pin_snapshot("snap-source")

    for index in range(Desktop._SNAPSHOT_HISTORY_LIMIT + 3):
        desktop._remember_snapshot(_state(f"snap-{index}"))

    assert "snap-source" in desktop._snapshots
    assert len(desktop._snapshots) == Desktop._SNAPSHOT_HISTORY_LIMIT + 1

    desktop.unpin_snapshot("snap-source")

    assert "snap-source" not in desktop._snapshots
    assert len(desktop._snapshots) == Desktop._SNAPSHOT_HISTORY_LIMIT


def test_resolve_snapshot_target_uses_fresh_bounds_from_foreground_window() -> None:
    source = _node(runtime_id=[42, 7])
    fresh_target = _node(center=(80, 90), runtime_id=[42, 7])
    background_duplicate = _node(center=(800, 900), runtime_id=[42, 7], window_handle=999)
    desktop = _desktop(_state("snap-1", source))
    desktop.tree = MagicMock()
    desktop.tree.get_state.return_value = TreeState(
        interactive_nodes=[fresh_target, background_duplicate]
    )

    result = desktop.resolve_snapshot_target("snap-1", label=0)

    assert result == (80, 90)
    desktop.tree.get_state.assert_called_once_with(
        active_window_handle=100,
        other_windows_handles=[],
        use_dom=False,
    )


def test_resolve_snapshot_target_rejects_ambiguous_runtime_identity() -> None:
    source = _node(runtime_id=[42, 7])
    desktop = _desktop(_state("snap-1", source))
    desktop.tree = MagicMock()
    desktop.tree.get_state.return_value = TreeState(
        interactive_nodes=[
            _node(center=(80, 90), runtime_id=[42, 7]),
            _node(center=(100, 110), runtime_id=[42, 7]),
        ]
    )

    with pytest.raises(ValueError, match="produced 2 matching elements"):
        desktop.resolve_snapshot_target("snap-1", label=0)


def test_resolve_uses_uia_window_root_when_foreground_is_a_child_hwnd() -> None:
    source = _node(runtime_id=[42, 7], window_handle=55)
    state = _state("snap-1", source)
    state.active_window = Window(
        name="Editor",
        is_browser=False,
        depth=0,
        status=Status.NORMAL,
        bounding_box=source.bounding_box,
        handle=55,
        process_id=200,
    )
    state.foreground_root_handle = 55
    desktop = _desktop(state)
    desktop.tree = MagicMock()
    desktop.tree.get_state.return_value = TreeState(
        interactive_nodes=[_node(center=(80, 90), runtime_id=[42, 7], window_handle=55)]
    )

    assert desktop.resolve_snapshot_target("snap-1", label=0) == (80, 90)
    desktop.tree.get_state.assert_called_once_with(
        active_window_handle=55,
        other_windows_handles=[],
        use_dom=False,
    )


def test_resolve_snapshot_target_checks_foreground_after_refresh() -> None:
    source = _node(runtime_id=[42, 7])
    desktop = _desktop(_state("snap-1", source))
    identities = iter([(100, 200), (101, 200)])
    desktop.get_foreground_identity = lambda: next(identities)
    desktop.tree = MagicMock()
    desktop.tree.get_state.return_value = TreeState(
        interactive_nodes=[_node(center=(80, 90), runtime_id=[42, 7])]
    )

    with pytest.raises(ValueError, match="STALE_STATE"):
        desktop.resolve_snapshot_target("snap-1", label=0)


def test_resolve_snapshot_target_relocates_unique_automation_id() -> None:
    source = _node(automation_id="save")
    desktop = _desktop(_state("snap-1", source))
    desktop.tree = MagicMock()
    desktop.tree.get_state.return_value = TreeState(
        interactive_nodes=[_node(center=(80, 90), automation_id="save")]
    )

    assert desktop.resolve_snapshot_target("snap-1", label=0) == (80, 90)


def test_resolve_snapshot_target_rejects_moved_weak_identity() -> None:
    source = _node()
    desktop = _desktop(_state("snap-1", source))
    desktop.tree = MagicMock()
    desktop.tree.get_state.return_value = TreeState(
        interactive_nodes=[_node(center=(80, 90))]
    )

    with pytest.raises(ValueError, match="TARGET_NOT_FOUND"):
        desktop.resolve_snapshot_target("snap-1", label=0)


def test_identity_metadata_ignores_mock_values() -> None:
    node = MagicMock()

    assert Tree._identity_metadata(node) == {}


@pytest.mark.parametrize(
    ("enabled", "offscreen"),
    [(1, 0), (0, 1), (True, False), (False, True)],
)
def test_identity_metadata_normalizes_com_bool(enabled: bool | int, offscreen: bool | int) -> None:
    node = SimpleNamespace(CachedIsEnabled=enabled, CachedIsOffscreen=offscreen)

    metadata = Tree._identity_metadata(node)

    assert metadata["is_enabled"] is bool(enabled)
    assert metadata["is_offscreen"] is bool(offscreen)


@pytest.mark.parametrize("invalid", [2, -1, "true", None])
def test_identity_metadata_rejects_non_boolean_cached_flags(invalid: object) -> None:
    node = SimpleNamespace(CachedIsEnabled=invalid, CachedIsOffscreen=invalid)

    assert Tree._identity_metadata(node) == {}


def test_horizontal_scroll_releases_shift_after_wheel_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = _desktop(_state("snap-1"))
    released: list[int] = []
    monkeypatch.setattr(service.uia, "PressKey", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        service.uia,
        "ReleaseKey",
        lambda key, **kwargs: released.append(key),
    )
    monkeypatch.setattr(
        service.uia,
        "WheelDown",
        MagicMock(side_effect=RuntimeError("input failed")),
    )

    with pytest.raises(RuntimeError, match="input failed"):
        desktop.scroll(type="horizontal", direction="right")

    assert released == [service.uia.Keys.VK_SHIFT]


def test_multi_select_releases_control_after_click_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = _desktop(_state("snap-1"))
    released: list[int] = []
    monkeypatch.setattr(service.uia, "PressKey", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        service.uia,
        "ReleaseKey",
        lambda key, **kwargs: released.append(key),
    )
    monkeypatch.setattr(
        service.uia,
        "Click",
        MagicMock(side_effect=RuntimeError("input failed")),
    )

    with pytest.raises(RuntimeError, match="input failed"):
        desktop.multi_select(press_ctrl=True, locs=[(1, 2)])

    assert released == [service.uia.Keys.VK_CONTROL]
