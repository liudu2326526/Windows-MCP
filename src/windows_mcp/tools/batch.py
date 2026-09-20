"""Bounded foreground Windows UI action batches with snapshot fencing."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
import uuid
from contextlib import nullcontext
from typing import Annotated, Any, Callable, Literal

from fastmcp import Context
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from psutil import Process
from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from windows_mcp.infrastructure import with_analytics
from windows_mcp.tools.input import _matches_wait_condition, _validate_wait_for_args


MAX_STEPS = 32
MAX_TIMEOUT = 120.0
logger = logging.getLogger(__name__)

Point = Annotated[list[StrictInt], Field(min_length=2, max_length=2)]


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


class Target(_Schema):
    """A coordinate or UIA target tied to the batch Snapshot."""

    loc: Point | None = None
    label: StrictInt | None = Field(default=None, ge=0)
    name: str | None = Field(default=None, min_length=1)
    window_name: str | None = Field(default=None, min_length=1)
    control_type: str | None = Field(default=None, min_length=1)
    snapshot_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_selector(self) -> "Target":
        if self.loc is not None and (self.label is not None or self.name is not None):
            raise ValueError("loc cannot be combined with label or name")
        if self.loc is None and self.label is None and self.name is None:
            raise ValueError("target requires loc, label, or name")
        return self


class WindowConstraint(_Schema):
    """Expected window identity for every input action in a batch."""

    title_contains: str | None = Field(default=None, min_length=1)
    process_name: str | None = Field(default=None, min_length=1)
    process_id: StrictInt | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_constraint(self) -> "WindowConstraint":
        if self.title_contains is None and self.process_name is None and self.process_id is None:
            raise ValueError("window requires title_contains, process_name, or process_id")
        return self


class WaitSpec(_Schema):
    condition: Literal[
        "text_exists",
        "active_window",
        "element_exists",
        "element_enabled",
        "focused_element",
    ]
    text: str | None = None
    window_name: str | None = None
    timeout: float = Field(default=10.0, gt=0, le=MAX_TIMEOUT)
    interval: float = Field(default=0.25, gt=0, le=5)
    use_dom: bool = False

    @model_validator(mode="after")
    def validate_condition(self) -> "WaitSpec":
        if self.condition != "value_equals":
            _validate_wait_for_args(
                self.condition, self.text, self.window_name, self.timeout, self.interval
            )
        return self


class VerifySpec(WaitSpec):
    condition: Literal[
        "text_exists",
        "active_window",
        "element_exists",
        "element_enabled",
        "focused_element",
        "value_equals",
    ]
    target: Target | None = None
    value: str | int | float | bool | None = None

    @model_validator(mode="after")
    def validate_value_target(self) -> "VerifySpec":
        if self.condition == "value_equals":
            if self.value is None or self.target is None:
                raise ValueError("value_equals requires target and value")
            if not (self.target.name and self.target.window_name and self.target.control_type):
                raise ValueError("value_equals target requires name, window_name, control_type")
            if self.target.label is not None or self.target.loc is not None:
                raise ValueError("value_equals uses a named target, not a label or coordinates")
        return self


class ClickArgs(_Schema):
    target: Target
    button: Literal["left", "right", "middle"] = "left"
    clicks: StrictInt = Field(default=1, ge=0, le=5)


class TypeArgs(_Schema):
    target: Target
    text: str
    caret_position: Literal["start", "idle", "end"] = "idle"
    clear: bool = False
    press_enter: bool = False


class ScrollArgs(_Schema):
    target: Target
    type: Literal["horizontal", "vertical"] = "vertical"
    direction: Literal["up", "down", "left", "right"] = "down"
    wheel_times: StrictInt = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def validate_direction(self) -> "ScrollArgs":
        valid = {"vertical": {"up", "down"}, "horizontal": {"left", "right"}}
        if self.direction not in valid[self.type]:
            raise ValueError(f"direction {self.direction!r} is invalid for {self.type} scroll")
        return self


class MoveArgs(_Schema):
    target: Target


class DragArgs(_Schema):
    target: Target
    from_loc: Point | None = None
    duration: float | None = Field(default=None, ge=0, le=10)


class ShortcutArgs(_Schema):
    shortcut: str = Field(min_length=1)


class MultiSelectArgs(_Schema):
    targets: list[Target] = Field(min_length=1, max_length=32)
    press_ctrl: bool = True


class EditEntry(_Schema):
    target: Target
    text: str


class MultiEditArgs(_Schema):
    entries: list[EditEntry] = Field(min_length=1, max_length=32)


class WaitArgs(_Schema):
    duration: float = Field(ge=0, le=MAX_TIMEOUT)


class _Step(_Schema):
    wait_for: WaitSpec | None = None
    verify: VerifySpec | None = None


class ClickStep(_Step):
    action: Literal["click"]
    args: ClickArgs


class TypeStep(_Step):
    action: Literal["type"]
    args: TypeArgs


class ScrollStep(_Step):
    action: Literal["scroll"]
    args: ScrollArgs


class MoveStep(_Step):
    action: Literal["move"]
    args: MoveArgs


class DragStep(_Step):
    action: Literal["drag"]
    args: DragArgs


class ShortcutStep(_Step):
    action: Literal["shortcut"]
    args: ShortcutArgs


class MultiSelectStep(_Step):
    action: Literal["multi_select"]
    args: MultiSelectArgs


class MultiEditStep(_Step):
    action: Literal["multi_edit"]
    args: MultiEditArgs


class WaitStep(_Step):
    action: Literal["wait"]
    args: WaitArgs


BatchStep = Annotated[
    ClickStep
    | TypeStep
    | ScrollStep
    | MoveStep
    | DragStep
    | ShortcutStep
    | MultiSelectStep
    | MultiEditStep
    | WaitStep,
    Field(discriminator="action"),
]

_STEPS_ADAPTER = TypeAdapter(list[BatchStep])
_CANCEL_LOCK = threading.Lock()
_CANCEL_EVENTS: dict[str, threading.Event] = {}


class BatchTimeoutError(TimeoutError):
    pass


class BatchCancelledError(RuntimeError):
    pass


def _parse_steps(value: object) -> list[BatchStep]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("steps must be valid JSON") from exc
    try:
        steps = _STEPS_ADAPTER.validate_python(value)
    except ValueError as exc:
        # Validation paths identify the bad field without returning typed text or values.
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            paths = [".".join(map(str, error["loc"])) for error in exc.errors(include_input=False)]
            raise ValueError("Invalid batch fields: " + ", ".join(paths)) from None
        raise
    if not steps:
        raise ValueError("steps must be a non-empty list")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"steps cannot contain more than {MAX_STEPS} actions")
    return steps


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BatchTimeoutError("RunBatch exceeded its total timeout")
    return remaining


def _check_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise BatchCancelledError("RunBatch was cancelled")


def _sleep_interruptibly(
    duration: float,
    deadline: float,
    cancel_event: threading.Event,
) -> None:
    end = time.monotonic() + duration
    while True:
        _check_cancelled(cancel_event)
        _remaining(deadline)
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        if cancel_event.wait(min(remaining, _remaining(deadline), 0.1)):
            _check_cancelled(cancel_event)


def _error_code(error: BaseException) -> str:
    message = str(error)
    for code in (
        "STALE_STATE",
        "TARGET_NOT_FOUND",
        "TARGET_MISMATCH",
        "VERIFY_FAILED",
    ):
        if code in message:
            return code
    if isinstance(error, BatchCancelledError):
        return "CANCELLED"
    if isinstance(error, (BatchTimeoutError, TimeoutError)):
        return "TIMEOUT"
    return "ACTION_FAILED"


def _check_window_constraint(state: Any, constraint: WindowConstraint | None) -> None:
    if constraint is None:
        return
    active = getattr(state, "active_window", None)
    if active is None:
        raise ValueError("TARGET_NOT_FOUND: Snapshot has no active window")
    if constraint.title_contains is not None:
        if constraint.title_contains.casefold() not in active.name.casefold():
            raise ValueError(f"TARGET_MISMATCH: active window is {active.name!r}")
    if constraint.process_id is not None and constraint.process_id != active.process_id:
        raise ValueError("TARGET_MISMATCH: active process id does not match")
    if constraint.process_name is not None:
        try:
            actual_name = Process(active.process_id).name()
        except Exception as exc:
            raise ValueError("TARGET_MISMATCH: active process name is unavailable") from exc
        if constraint.process_name.casefold() != actual_name.casefold():
            raise ValueError("TARGET_MISMATCH: active process name does not match")


def _require_snapshot(desktop: Any, snapshot_id: str, window: WindowConstraint | None) -> Any:
    state = desktop.require_snapshot(snapshot_id)
    _check_window_constraint(state, window)
    return state


def _resolve_target(
    desktop: Any,
    target: Target,
    snapshot_id: str,
    window: WindowConstraint | None,
) -> list[int]:
    target_snapshot_id = target.snapshot_id or snapshot_id
    _require_snapshot(desktop, target_snapshot_id, window)
    if target.loc is not None:
        return list(target.loc)
    resolver = getattr(desktop, "resolve_snapshot_target", None)
    if resolver is None:
        raise ValueError("TARGET_NOT_FOUND: desktop runtime cannot relocate UIA targets")
    x, y = resolver(
        target_snapshot_id,
        label=target.label,
        name=target.name,
        window_name=target.window_name,
        control_type=target.control_type,
    )
    return [x, y]


def _wait_for(
    desktop: Any,
    spec: WaitSpec,
    deadline: float,
    cancel_event: threading.Event,
    snapshot_id: str | None = None,
    window: WindowConstraint | None = None,
) -> str:
    timeout = min(spec.timeout, _remaining(deadline))
    normalized = _validate_wait_for_args(
        spec.condition,
        spec.text,
        spec.window_name,
        timeout,
        spec.interval,
    )
    end = time.monotonic() + timeout
    attempts = 0
    while True:
        _check_cancelled(cancel_event)
        _remaining(deadline)
        if snapshot_id is not None:
            _require_snapshot(desktop, snapshot_id, window)
        attempts += 1
        state = desktop.get_state(
            use_vision=False,
            use_dom=spec.use_dom,
            use_ui_tree=True,
            use_annotation=False,
        )
        _check_cancelled(cancel_event)
        _remaining(deadline)
        if snapshot_id is not None:
            _require_snapshot(desktop, snapshot_id, window)
        matched, _ = _matches_wait_condition(
            state,
            normalized,
            spec.text,
            spec.window_name,
        )
        if matched:
            return f"{normalized} satisfied after {attempts} attempt(s)"
        if time.monotonic() >= end:
            raise BatchTimeoutError(f"timed out waiting for {normalized}")
        _sleep_interruptibly(
            min(spec.interval, end - time.monotonic()),
            deadline,
            cancel_event,
        )


def _verify(
    desktop: Any,
    spec: VerifySpec,
    snapshot_id: str,
    window: WindowConstraint | None,
    deadline: float,
    cancel_event: threading.Event,
) -> str:
    if spec.condition != "value_equals":
        return _wait_for(desktop, spec, deadline, cancel_event, snapshot_id, window)
    if spec.value is None:
        raise ValueError("verify.value is required for value_equals")
    if spec.target is None or spec.target.name is None:
        raise ValueError("value_equals requires verify.target.name")
    if spec.target.window_name is None or spec.target.control_type is None:
        raise ValueError(
            "value_equals requires verify.target.window_name and verify.target.control_type"
        )
    _require_snapshot(desktop, spec.target.snapshot_id or snapshot_id, window)
    state = desktop.get_state(
        use_vision=False,
        use_dom=False,
        use_ui_tree=True,
        use_annotation=False,
    )
    _require_snapshot(desktop, snapshot_id, window)
    _remaining(deadline)
    _check_cancelled(cancel_event)
    tree = getattr(state, "tree_state", None)
    nodes = [] if tree is None else list(tree.interactive_nodes) + list(tree.scrollable_nodes)
    matches = [
        node
        for node in nodes
        if node.name.casefold() == spec.target.name.casefold()
        and node.window_name.casefold() == spec.target.window_name.casefold()
        and node.control_type.casefold() == spec.target.control_type.casefold()
    ]
    if len(matches) != 1 or str(matches[0].metadata.get("value")) != str(spec.value):
        raise ValueError("VERIFY_FAILED: value did not match the unique target")
    return "value matched"


def _execute_input_action(
    desktop: Any,
    step: BatchStep,
    snapshot_id: str,
    window: WindowConstraint | None,
    deadline: float,
    cancel_event: threading.Event,
    progress: dict[str, bool],
) -> str:
    _check_cancelled(cancel_event)
    _remaining(deadline)
    lock = getattr(desktop, "interaction_lock", None)
    guard = lock if lock is not None else nullcontext()
    with guard:
        _check_cancelled(cancel_event)
        _remaining(deadline)
        _require_snapshot(desktop, snapshot_id, window)
        def invoke(method, *positional, **keyword):
            _check_cancelled(cancel_event)
            _remaining(deadline)
            _require_snapshot(desktop, snapshot_id, window)
            progress["side_effects_possible"] = True
            result = method(*positional, **keyword)
            return result

        args = step.args
        if step.action == "click":
            loc = _resolve_target(desktop, args.target, snapshot_id, window)
            invoke(desktop.click, loc, button=args.button, clicks=args.clicks)
            return "clicked"
        if step.action == "type":
            loc = _resolve_target(desktop, args.target, snapshot_id, window)
            invoke(desktop.type,
                loc,
                text=args.text,
                caret_position=args.caret_position,
                clear=args.clear,
                press_enter=args.press_enter,
            )
            return "typed"
        if step.action == "scroll":
            loc = _resolve_target(desktop, args.target, snapshot_id, window)
            result = invoke(desktop.scroll, loc, args.type, args.direction, args.wheel_times)
            if result is not None:
                raise RuntimeError("scroll provider returned a failure")
            return "scrolled"
        if step.action == "move":
            loc = _resolve_target(desktop, args.target, snapshot_id, window)
            invoke(desktop.move, tuple(loc))
            return "moved"
        if step.action == "drag":
            loc = _resolve_target(desktop, args.target, snapshot_id, window)
            result = invoke(desktop.drag, loc, from_loc=args.from_loc, duration=args.duration)
            return f"dragged to {result['end']}"
        if step.action == "shortcut":
            invoke(desktop.shortcut, args.shortcut)
            return "shortcut sent"
        if step.action == "multi_select":
            for target in args.targets:
                loc = _resolve_target(desktop, target, snapshot_id, window)
                invoke(desktop.multi_select, args.press_ctrl, [loc])
            return f"selected {len(args.targets)} targets"
        if step.action == "multi_edit":
            for entry in args.entries:
                loc = _resolve_target(desktop, entry.target, snapshot_id, window)
                invoke(desktop.type, loc, text=entry.text, clear=True)
            return f"edited {len(args.entries)} targets"
    raise AssertionError(f"unhandled action {step.action}")


def _observation(desktop: Any) -> dict[str, Any]:
    state = getattr(desktop, "desktop_state", None)
    active = getattr(state, "active_window", None) if state else None
    observation = {
        "snapshot_id": getattr(state, "snapshot_id", None),
        "active_window": getattr(active, "name", None),
        "active_process_id": getattr(active, "process_id", None),
        "foreground_handle": None,
        "foreground_process_id": None,
    }
    reader = getattr(desktop, "get_foreground_identity", None)
    if reader is not None:
        try:
            handle, process_id = reader()
            observation["foreground_handle"] = handle
            observation["foreground_process_id"] = process_id
        except Exception:
            pass
    return observation


def _failure_payload(
    *,
    execution_id: str,
    results: list[dict[str, Any]],
    desktop: Any,
    started: float,
    verified: bool,
    error_code: str | None = None,
) -> dict[str, Any]:
    if error_code is None:
        error_code = next(
            (result.get("error_code") for result in reversed(results) if result.get("error_code")),
            "ACTION_FAILED",
        )
    completed_actions = sum(result["action_completed"] for result in results)
    side_effects_possible = any(result["side_effects_possible"] for result in results)
    return {
        "version": 1,
        "execution_id": execution_id,
        "status": "partial" if side_effects_possible else "failed",
        "completed_actions": completed_actions,
        "side_effects_possible": side_effects_possible,
        "verified": verified,
        "error_code": error_code,
        "total_ms": int((time.monotonic() - started) * 1000),
        "steps": results,
        "final_observation": _observation(desktop),
    }


def register(mcp: Any, *, get_desktop: Callable[[], Any], get_analytics: Callable[[], Any]) -> None:
    """Register bounded batch execution and cooperative cancellation tools."""

    def execute_batch(
        steps: list[BatchStep] | str,
        snapshot_id: str | None,
        window: WindowConstraint | None,
        execution_id: str,
        timeout: float,
        stop_on_error: bool | str,
        cancel_event: threading.Event,
    ) -> ToolResult:
        return run_batch_sync(
            steps, snapshot_id, window, execution_id, timeout, stop_on_error, cancel_event
        )

    @mcp.tool(
        name="RunBatch",
        description=(
            "Run up to 32 serial foreground UI actions against one Snapshot. Every input step "
            "checks the real foreground HWND/PID; label and name targets are relocated in a fresh "
            "UIA tree before input. Supported actions: click, type, scroll, move, drag, shortcut, "
            "multi_select, multi_edit, and wait. Each step may include wait_for and verify. Shell, "
            "filesystem, registry, process, application launch, and nested batch actions are excluded."
        ),
        annotations=ToolAnnotations(
            title="Run Batch",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "RunBatch-Tool")
    async def run_batch(
        steps: list[BatchStep] | str,
        snapshot_id: str | None = None,
        window: WindowConstraint | None = None,
        execution_id: str | None = None,
        timeout: float = 30.0,
        stop_on_error: bool | str = True,
        ctx: Context = None,
    ) -> ToolResult:
        cancel_event = threading.Event()
        run_id = execution_id or uuid.uuid4().hex
        try:
            return await asyncio.to_thread(
                execute_batch, steps, snapshot_id, window, run_id, timeout,
                stop_on_error, cancel_event,
            )
        except asyncio.CancelledError:
            cancel_event.set()
            raise

    def run_batch_sync(
        steps: list[BatchStep] | str,
        snapshot_id: str | None,
        window: WindowConstraint | None,
        run_id: str,
        timeout: float,
        stop_on_error: bool | str,
        cancel_event: threading.Event,
    ) -> ToolResult:
        parsed_steps = _parse_steps(steps)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
        ):
            raise ValueError("timeout must be a finite number")
        if timeout <= 0 or timeout > MAX_TIMEOUT:
            raise ValueError(f"timeout must be greater than 0 and at most {MAX_TIMEOUT:g} seconds")
        if isinstance(stop_on_error, str):
            normalized_stop = stop_on_error.strip().casefold()
            if normalized_stop not in {"true", "false"}:
                raise ValueError("stop_on_error must be true or false")
            stop = normalized_stop == "true"
        elif isinstance(stop_on_error, bool):
            stop = stop_on_error
        else:
            raise ValueError("stop_on_error must be true or false")

        if not run_id.strip() or len(run_id) > 128:
            raise ValueError("execution_id must contain 1 to 128 characters")
        if window is not None:
            window = WindowConstraint.model_validate(window)
        targets: list[Target] = []
        for step in parsed_steps:
            if hasattr(step.args, "target"):
                targets.append(step.args.target)
            elif step.action == "multi_select":
                targets.extend(step.args.targets)
            elif step.action == "multi_edit":
                targets.extend(entry.target for entry in step.args.entries)
            if step.verify and step.verify.target:
                targets.append(step.verify.target)
        if any(target.label is not None and not (target.snapshot_id or snapshot_id) for target in targets):
            raise ValueError("label targets require an explicit snapshot_id")
        with _CANCEL_LOCK:
            if run_id in _CANCEL_EVENTS:
                raise ValueError(f"execution_id {run_id!r} is already running")
            _CANCEL_EVENTS[run_id] = cancel_event

        started = time.monotonic()
        deadline = started + timeout
        results: list[dict[str, Any]] = []
        verified_steps = 0
        pins: list[str] = []
        desktop = None
        try:
            try:
                desktop = get_desktop()
                has_input = any(
                    step.action != "wait" or step.verify is not None for step in parsed_steps
                )
                if has_input and snapshot_id is None:
                    state = desktop.get_state(
                        use_vision=False,
                        use_dom=False,
                        use_ui_tree=True,
                        use_annotation=False,
                    )
                    snapshot_id = state.snapshot_id
                if has_input and not snapshot_id:
                    raise ValueError("STALE_STATE: RunBatch could not establish a Snapshot")
                if snapshot_id is not None:
                    _require_snapshot(desktop, snapshot_id, window)
                    source_ids = {snapshot_id} | {
                        target.snapshot_id for target in targets if target.snapshot_id
                    }
                    for source_id in source_ids:
                        _require_snapshot(desktop, source_id, window)
                        desktop.pin_snapshot(source_id)
                        pins.append(source_id)
            except Exception as exc:
                payload = _failure_payload(
                    execution_id=run_id,
                    results=results,
                    desktop=desktop,
                    started=started,
                    verified=False,
                    error_code=_error_code(exc),
                )
                return ToolResult(
                    content=json.dumps(payload, ensure_ascii=False),
                    structured_content=payload,
                    is_error=True,
                )

            for index, step in enumerate(parsed_steps):
                step_started = time.monotonic()
                progress = {"action_completed": False, "side_effects_possible": False}
                try:
                    _check_cancelled(cancel_event)
                    _remaining(deadline)
                    wait_detail = None
                    if step.wait_for is not None:
                        wait_detail = _wait_for(
                            desktop,
                            step.wait_for,
                            deadline,
                            cancel_event,
                            snapshot_id,
                            window,
                        )
                    if step.action == "wait":
                        _sleep_interruptibly(
                            step.args.duration,
                            deadline,
                            cancel_event,
                        )
                        detail = "waited"
                    else:
                        assert snapshot_id is not None
                        detail = _execute_input_action(
                            desktop,
                            step,
                            snapshot_id,
                            window,
                            deadline,
                            cancel_event,
                            progress,
                        )
                        progress["action_completed"] = True
                    _remaining(deadline)
                    _check_cancelled(cancel_event)
                    verify_detail = None
                    if step.verify is not None:
                        assert snapshot_id is not None
                        verify_detail = _verify(
                            desktop,
                            step.verify,
                            snapshot_id,
                            window,
                            deadline,
                            cancel_event,
                        )
                        verified_steps += 1
                    results.append(
                        {
                            "index": index,
                            "action": step.action,
                            "status": "succeeded",
                            "elapsed_ms": int((time.monotonic() - step_started) * 1000),
                            "detail": detail,
                            "wait": wait_detail,
                            "verify": verify_detail,
                            **progress,
                        }
                    )
                except Exception as exc:
                    code = _error_code(exc)
                    results.append(
                        {
                            "index": index,
                            "action": step.action,
                            "status": "failed",
                            "elapsed_ms": int((time.monotonic() - step_started) * 1000),
                            "error_code": code,
                            "error": code,
                            **progress,
                        }
                    )
                    if stop or code in {"TIMEOUT", "CANCELLED", "STALE_STATE"}:
                        break

            failed = any(result["status"] == "failed" for result in results)
            if failed:
                payload = _failure_payload(
                    execution_id=run_id,
                    results=results,
                    desktop=desktop,
                    started=started,
                    verified=False,
                )
                return ToolResult(
                    content=json.dumps(payload, ensure_ascii=False),
                    structured_content=payload,
                    is_error=True,
                )

            total_ms = int((time.monotonic() - started) * 1000)
            payload = {
                "version": 1,
                "execution_id": run_id,
                "status": "completed",
                "completed_actions": sum(result["action_completed"] for result in results),
                "side_effects_possible": any(result["side_effects_possible"] for result in results),
                "verified": verified_steps > 0 and parsed_steps[-1].verify is not None,
                "error_code": None,
                "total_ms": total_ms,
                "steps": results,
                "final_observation": _observation(desktop),
            }
            logger.info(
                "RunBatch: status=completed steps=%d verified=%d total_ms=%d",
                len(results),
                verified_steps,
                total_ms,
            )
            return ToolResult(content=json.dumps(payload, ensure_ascii=False), structured_content=payload)
        finally:
            if desktop is not None:
                for source_id in pins:
                    desktop.unpin_snapshot(source_id)
            with _CANCEL_LOCK:
                _CANCEL_EVENTS.pop(run_id, None)

    @mcp.tool(
        name="CancelBatch",
        description=(
            "Request cooperative cancellation of an active RunBatch by its explicit execution_id. "
            "The batch stops between UIA calls and during local waits; an in-progress native input "
            "call must return before cancellation can take effect."
        ),
        annotations=ToolAnnotations(
            title="Cancel Batch",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "CancelBatch-Tool")
    def cancel_batch(execution_id: str, ctx: Context = None) -> str:
        with _CANCEL_LOCK:
            event = _CANCEL_EVENTS.get(execution_id)
            if event is None:
                status = "not_found"
            else:
                event.set()
                status = "cancellation_requested"
        return json.dumps(
            {"version": 1, "execution_id": execution_id, "status": status},
            ensure_ascii=False,
        )
