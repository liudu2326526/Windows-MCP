"""Batch contracts with a deterministic desktop and the real MCP wire adapter."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest
from fastmcp import Client, FastMCP

from windows_mcp.desktop.views import DesktopState, Status, Window
from windows_mcp.tree.views import BoundingBox, Center, TreeElementNode, TreeState


@pytest.fixture
def batch(monkeypatch):
    """Load real tool/analytics modules without loading unrelated Windows tools."""
    if sys.platform == "win32":
        return importlib.import_module("windows_mcp.tools.batch")

    source = Path(__file__).resolve().parents[1] / "src" / "windows_mcp"
    for name in ("infrastructure", "tools"):
        package = ModuleType(f"windows_mcp.{name}")
        package.__path__ = [str(source / name)]
        monkeypatch.setitem(sys.modules, package.__name__, package)

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    load("windows_mcp.infrastructure.config", source / "infrastructure" / "config.py")
    analytics = load(
        "windows_mcp.infrastructure.analytics", source / "infrastructure" / "analytics.py"
    )
    sys.modules["windows_mcp.infrastructure"].with_analytics = analytics.with_analytics
    load("windows_mcp.tools.input", source / "tools" / "input.py")
    return load("windows_mcp.tools.batch", source / "tools" / "batch.py")


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, **kwargs):
        def register(function):
            self.tools[name] = function
            return function

        return register


class FakeDesktop:
    """Models a current foreground window and separately retained Snapshot identities."""

    def __init__(self):
        self.interaction_lock = threading.RLock()
        self.calls = []
        self.resolutions = []
        self.foreground = (10, 20)
        self.after_input = None
        self.on_observe = None
        self.input_started = threading.Event()
        self.desktop_state = DesktopState(
            active_desktop={"name": "Desktop 1"},
            all_desktops=[],
            active_window=Window(
                name="Settings",
                is_browser=False,
                depth=0,
                status=Status.NORMAL,
                bounding_box=BoundingBox(0, 0, 100, 100, 100, 100),
                handle=10,
                process_id=20,
            ),
            windows=[],
            snapshot_id="snap-1",
            foreground_handle=10,
            foreground_process_id=20,
            tree_state=TreeState(
                interactive_nodes=[
                    TreeElementNode(
                        name="Name",
                        control_type="Edit",
                        window_name="Settings",
                        bounding_box=BoundingBox(0, 0, 40, 20, 40, 20),
                        center=Center(20, 10),
                        metadata={"value": "old", "runtime_id": [10, 42]},
                    )
                ]
            ),
        )
        self.snapshots = {"snap-1": self.desktop_state}
        self.pins = set()

    def get_foreground_identity(self):
        return self.foreground

    def require_snapshot(self, snapshot_id):
        state = self.snapshots.get(snapshot_id)
        if state is None or self.foreground != (
            state.foreground_handle,
            state.foreground_process_id,
        ):
            raise ValueError("STALE_STATE: captured window is not foreground")
        return state

    def pin_snapshot(self, snapshot_id):
        self.require_snapshot(snapshot_id)
        self.pins.add(snapshot_id)

    def unpin_snapshot(self, snapshot_id):
        self.pins.discard(snapshot_id)

    def resolve_snapshot_target(self, snapshot_id, **selector):
        state = self.require_snapshot(snapshot_id)
        self.resolutions.append((snapshot_id, selector))
        nodes = state.tree_state.interactive_nodes
        if selector["label"] is not None:
            nodes = nodes[selector["label"] : selector["label"] + 1]
        else:
            nodes = [node for node in nodes if node.name == selector["name"]]
        if len(nodes) != 1:
            raise ValueError("TARGET_NOT_FOUND: expected one target")
        node = nodes[0]
        for name in ("name", "window_name", "control_type"):
            if selector[name] is not None and selector[name] != getattr(node, name):
                raise ValueError("TARGET_MISMATCH: target guard changed")
        return node.center.x, node.center.y

    def _input(self, name, value):
        self.calls.append((name, value))
        self.input_started.set()
        if self.after_input is not None:
            self.after_input(name)

    def click(self, loc, **kwargs):
        self._input("click", list(loc))

    def type(self, loc, **kwargs):
        self._input("type", [*loc, kwargs["text"]])

    def shortcut(self, shortcut):
        self._input("shortcut", shortcut)

    def multi_edit(self, locs):
        self._input("multi_edit", locs)

    def multi_select(self, press_ctrl, locs):
        self._input("multi_select", locs)

    def get_state(self, **kwargs):
        self.calls.append(("observe", kwargs))
        if self.on_observe is not None:
            self.on_observe()
        return self.desktop_state


@pytest.fixture
def desktop():
    return FakeDesktop()


def tools(batch, desktop):
    mcp = FakeMCP()
    batch.register(mcp, get_desktop=lambda: desktop, get_analytics=lambda: None)
    return mcp.tools


async def call(function, **arguments):
    result = function(**arguments)
    return await result if inspect.isawaitable(result) else result


def payload(result):
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    if isinstance(result, str):
        return json.loads(result)
    return json.loads(next(item.text for item in result.content if item.type == "text"))


def click(**extra):
    return {"action": "click", "args": {"target": {"loc": [1, 2]}}, **extra}


def shortcut():
    return {"action": "shortcut", "args": {"shortcut": "ctrl+s"}}


def value_verification(value="expected"):
    return {
        "condition": "value_equals",
        "target": {"name": "Name", "window_name": "Settings", "control_type": "Edit"},
        "value": value,
    }


@pytest.mark.asyncio
async def test_serial_inputs_return_compact_result_without_input_text(batch, desktop):
    secret = "private-form-value-1873"
    result = await call(
        tools(batch, desktop)["RunBatch"],
        steps=[
            click(),
            {"action": "type", "args": {"target": {"loc": [1, 2]}, "text": secret}},
            shortcut(),
        ],
        snapshot_id="snap-1",
    )
    data = payload(result)
    assert data["status"] == "completed"
    assert data["completed_actions"] == 3
    assert [name for name, _ in desktop.calls] == ["click", "type", "shortcut"]
    assert secret not in json.dumps(data)
    assert not result.is_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"action": "unknown", "args": {}},
        {"action": "click", "args": {"target": {"loc": [True, 2]}}},
        {"action": "click", "args": {"target": {"loc": [1, 2]}, "clicks": -1}},
        {"action": "scroll", "args": {"target": {"loc": [1, 2]}, "direction": "left"}},
        {"action": "type", "args": {"target": {"loc": [1, 2]}}},
        {"action": "shortcut", "args": {"shortcut": "ctrl+s", "unexpected": 1}},
        click(verify={"condition": "value_equals"}),
        click(wait_for={"condition": "text_exists"}),
    ],
)
async def test_all_step_parameters_are_validated_before_any_input(batch, desktop, invalid):
    with pytest.raises(ValueError):
        await call(tools(batch, desktop)["RunBatch"], steps=[click(), invalid], snapshot_id="snap-1")
    assert desktop.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, 121, True, float("nan"), float("inf")])
async def test_invalid_timeout_never_reaches_desktop(batch, desktop, timeout):
    with pytest.raises(ValueError, match="timeout"):
        await call(tools(batch, desktop)["RunBatch"], steps=[click()], timeout=timeout)
    assert desktop.calls == []


@pytest.mark.asyncio
async def test_label_requires_explicit_snapshot_before_observation(batch, desktop):
    with pytest.raises(ValueError, match="snapshot"):
        await call(
            tools(batch, desktop)["RunBatch"],
            steps=[{"action": "click", "args": {"target": {"label": 0}}}],
        )
    assert desktop.calls == []


@pytest.mark.asyncio
async def test_label_is_relocated_with_identity_guards(batch, desktop):
    result = await call(
        tools(batch, desktop)["RunBatch"],
        steps=[{"action": "click", "args": {"target": {"label": 0, "name": "Name"}}}],
        snapshot_id="snap-1",
    )
    assert payload(result)["status"] == "completed"
    assert desktop.calls == [("click", [20, 10])]
    assert desktop.resolutions[0][0] == "snap-1"
    assert desktop.resolutions[0][1]["name"] == "Name"
    assert desktop.pins == set()


@pytest.mark.asyncio
async def test_foreground_switch_prevents_later_input_even_when_stop_is_false(batch, desktop):
    desktop.after_input = lambda _: setattr(desktop, "foreground", (99, 100))
    result = await call(
        tools(batch, desktop)["RunBatch"],
        steps=[click(), shortcut(), click()],
        snapshot_id="snap-1",
        stop_on_error=False,
    )
    data = payload(result)
    assert data["status"] == "partial"
    assert data["error_code"] == "STALE_STATE"
    assert data["completed_actions"] == 1
    assert desktop.calls == [("click", [1, 2])]
    assert result.is_error
    assert data["final_observation"]["foreground_handle"] == 99


@pytest.mark.asyncio
async def test_wait_observation_does_not_rebind_existing_labels(batch, desktop):
    original = desktop.desktop_state
    replacement = replace(original.tree_state.interactive_nodes[0], name="Other", center=Center(80, 90))
    desktop.on_observe = lambda: setattr(
        desktop,
        "desktop_state",
        replace(original, snapshot_id="snap-2", tree_state=TreeState(interactive_nodes=[replacement])),
    )
    result = await call(
        tools(batch, desktop)["RunBatch"],
        steps=[
            {
                "action": "click",
                "args": {"target": {"label": 0}},
                "wait_for": {"condition": "active_window", "text": "Settings"},
            }
        ],
        snapshot_id="snap-1",
    )
    assert payload(result)["status"] == "completed"
    assert desktop.resolutions[0][0] == "snap-1"
    assert desktop.calls[-1] == ("click", [20, 10])


@pytest.mark.asyncio
async def test_postcondition_failure_reports_completed_action_and_stops(batch, desktop):
    secret = "failed-input-value-9127"
    result = await call(
        tools(batch, desktop)["RunBatch"],
        steps=[
            {
                "action": "type",
                "args": {"target": {"loc": [1, 2]}, "text": secret},
                "verify": value_verification(),
            },
            shortcut(),
        ],
        snapshot_id="snap-1",
    )
    data = payload(result)
    assert data["status"] == "partial"
    assert data["error_code"] == "VERIFY_FAILED"
    assert data["completed_actions"] == 1
    assert data["steps"][0]["action_completed"] is True
    assert data["steps"][0]["side_effects_possible"] is True
    assert data["verified"] is False
    assert [name for name, _ in desktop.calls] == ["type", "observe"]
    assert secret not in json.dumps(data)
    assert result.is_error
    assert desktop.pins == set()


@pytest.mark.asyncio
async def test_provider_exception_may_have_side_effects_without_completed_action(batch, desktop):
    def fail(_):
        desktop.get_foreground_identity = lambda: (_ for _ in ()).throw(
            RuntimeError("foreground-provider-secret")
        )
        raise RuntimeError("input-provider-secret")

    desktop.after_input = fail
    result = await call(
        tools(batch, desktop)["RunBatch"], steps=[click(), shortcut()], snapshot_id="snap-1"
    )
    data = payload(result)
    assert data["status"] == "partial"
    assert data["completed_actions"] == 0
    assert data["steps"][0]["action_completed"] is False
    assert data["steps"][0]["side_effects_possible"] is True
    assert desktop.calls == [("click", [1, 2])]
    assert "provider-secret" not in json.dumps(data)


@pytest.mark.asyncio
async def test_timeout_after_native_call_prevents_next_action(batch, desktop, monkeypatch):
    clock = SimpleNamespace(value=10.0)
    monkeypatch.setattr(batch, "time", SimpleNamespace(monotonic=lambda: clock.value))
    desktop.after_input = lambda _: setattr(clock, "value", 11.0)
    result = await call(
        tools(batch, desktop)["RunBatch"],
        steps=[click(), shortcut()],
        snapshot_id="snap-1",
        timeout=0.5,
        stop_on_error=False,
    )
    data = payload(result)
    assert data["status"] == "partial"
    assert data["error_code"] == "TIMEOUT"
    assert data["completed_actions"] == 1
    assert desktop.calls == [("click", [1, 2])]


@pytest.mark.asyncio
async def test_cancel_wakes_wait_and_cleans_execution_registry(batch, desktop):
    registered = tools(batch, desktop)
    task = asyncio.create_task(
        call(
            registered["RunBatch"],
            steps=[click(), {"action": "wait", "args": {"duration": 60}}, shortcut()],
            snapshot_id="snap-1",
            execution_id="cancel-test",
            timeout=90,
        )
    )
    try:
        assert await asyncio.to_thread(desktop.input_started.wait, 2)
        cancellation = await call(registered["CancelBatch"], execution_id="cancel-test")
        assert payload(cancellation)["status"] == "cancellation_requested"
        result = await asyncio.wait_for(task, 2)
    finally:
        if not task.done():
            await call(registered["CancelBatch"], execution_id="cancel-test")
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    data = payload(result)
    assert data["status"] == "partial"
    assert data["error_code"] == "CANCELLED"
    assert data["completed_actions"] == 1
    assert desktop.calls == [("click", [1, 2])]
    assert "cancel-test" not in batch._CANCEL_EVENTS
    assert payload(await call(registered["CancelBatch"], execution_id="cancel-test"))["status"] == "not_found"


@pytest.mark.asyncio
async def test_wait_only_batch_has_no_completed_input_actions(batch, desktop):
    result = await call(tools(batch, desktop)["RunBatch"], steps=[{"action": "wait", "args": {"duration": 0}}])
    assert payload(result)["completed_actions"] == 0
    assert desktop.calls == []


@pytest.mark.asyncio
async def test_json_string_steps_preserve_compatibility(batch, desktop):
    result = await call(
        tools(batch, desktop)["RunBatch"], steps=json.dumps([click()]), snapshot_id="snap-1"
    )
    assert payload(result)["status"] == "completed"


@pytest.fixture
def server(batch, desktop):
    server = FastMCP("batch-contract-tests")
    batch.register(server, get_desktop=lambda: desktop, get_analytics=lambda: None)
    return server


@pytest.mark.asyncio
async def test_wire_schema_exposes_action_variants_and_hides_context(server):
    async with Client(server) as client:
        definitions = {tool.name: tool for tool in await client.list_tools()}
    schema = definitions["RunBatch"].input_schema
    assert "ctx" not in schema["properties"]
    variants = schema["properties"]["steps"]["anyOf"]
    array_schema = next(item for item in variants if item.get("type") == "array")
    assert "items" in array_schema
    text = json.dumps(array_schema)
    for field in ("click", "shortcut", "multi_edit", "target", "text", "wait_for", "verify"):
        assert field in text
    assert "execution_id" in definitions["CancelBatch"].input_schema["properties"]


@pytest.mark.asyncio
async def test_wire_stale_snapshot_is_error_without_input(server, desktop):
    async with Client(server) as client:
        result = await client.call_tool(
            "RunBatch", {"steps": [click()], "snapshot_id": "expired"}, raise_on_error=False
        )
    assert result.is_error
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["error_code"] == "STALE_STATE"
    assert desktop.calls == []


@pytest.mark.asyncio
async def test_wire_partial_verification_failure_is_error_with_structured_details(server, desktop):
    async with Client(server) as client:
        result = await client.call_tool(
            "RunBatch",
            {"steps": [click(verify=value_verification())], "snapshot_id": "snap-1"},
            raise_on_error=False,
        )
    assert result.is_error
    assert result.structured_content["status"] == "partial"
    assert result.structured_content["completed_actions"] == 1
    assert result.structured_content["error_code"] == "VERIFY_FAILED"
