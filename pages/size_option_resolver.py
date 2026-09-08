"""PDP 尺码模型识别与统一选项抽象。

Resolver 先在主购买表单关联范围内识别语义化 Size Group，再通过对应
adapter 解析 radio。所有 public operation 都重新检测当前 DOM，不缓存
ElementHandle/JSHandle，因而可安全处理 Theme/SPB rerender 与模型切换。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


SIZE_MODEL_01 = "SIZE_MODEL_01"
SIZE_MODEL_02 = "SIZE_MODEL_02"
SIZE_MODEL_03 = "SIZE_MODEL_03"
DEFAULT_FREE_SIZE_MARKER = "free custom size"
SIZE_SELECTION_CONVERGENCE_TIMEOUT_MS = 1_000
SIZE_SELECTION_POLL_INTERVAL_MS = 50
SIZE_SELECTION_SETTLE_MS = 100
SIZE_SELECTION_MAX_ATTEMPTS = 2


class SizeGroupNotFoundError(LookupError):
    """当前主购买区没有可识别的 Size Group。"""


@dataclass(frozen=True)
class SizeOption:
    """不同 PDP DOM 模型归一化后的单个尺码选项。"""

    value: str
    display_text: str
    available: bool
    selected: bool
    model: str
    custom_size: bool
    locator: Any = field(repr=False, compare=False)
    control_locator: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class SizeGroup:
    """一次实时检测得到的 Size Group；仅在当前 operation 内使用。"""

    model: str
    locator: Any = field(repr=False, compare=False)
    option_selector: str
    disabled_class_tokens: tuple[str, ...]
    custom_size_value: str
    config: dict = field(repr=False, compare=False)


class SizeOptionResolver:
    """配置驱动的 Size Group detector 与 radio adapter。"""

    def __init__(
        self,
        page,
        product_config: dict,
        purchase_area_factory: Callable[[], Any],
    ) -> None:
        self.page = page
        self.product_config = product_config
        self.purchase_area_factory = purchase_area_factory

    # --------------------------------------------------------------- 配置
    def _resolver_config(self) -> dict:
        config = self.product_config.get("size_resolver") or {}
        if not isinstance(config, dict):
            return {}
        return config

    def _model_configs(self) -> list[dict]:
        models = self._resolver_config().get("models") or []
        return [model for model in models if isinstance(model, dict)]

    def _custom_measurement_config(self) -> dict:
        config = self._resolver_config().get("custom_measurement") or {}
        return config if isinstance(config, dict) else {}

    @staticmethod
    def _normalized(value: Optional[str]) -> str:
        return " ".join(str(value or "").split()).strip().lower()

    # --------------------------------------------------------------- 检测
    @staticmethod
    def _attribute_matches(group, expected: dict) -> bool:
        for name, expected_value in expected.items():
            actual = group.get_attribute(str(name))
            if SizeOptionResolver._normalized(actual) != SizeOptionResolver._normalized(
                str(expected_value)
            ):
                return False
        return True

    def _group_name(self, group) -> str:
        aria_label = group.get_attribute("aria-label")
        if aria_label:
            return aria_label.strip()

        labelled_by = group.get_attribute("aria-labelledby")
        if labelled_by:
            labels = []
            for element_id in labelled_by.split():
                label = self.page.locator(f'#{element_id}').first
                if label.count():
                    labels.append(label.inner_text().strip())
            if labels:
                return " ".join(labels)

        legend = group.locator(":scope > legend").first
        if legend.count():
            return legend.inner_text().strip()
        return str(group.get_attribute("name") or "").strip()

    def _associated_with_purchase(self, group, option_selector: str) -> bool:
        purchase = self.purchase_area_factory()
        if purchase.count() == 0:
            return False
        purchase_id = str(purchase.get_attribute("id") or "")

        ancestor_form = group.locator("xpath=ancestor::form[1]").first
        if ancestor_form.count():
            if not purchase_id or ancestor_form.get_attribute("id") == purchase_id:
                return True

        if not purchase_id:
            return False
        options = group.locator(option_selector)
        for index in range(options.count()):
            option = options.nth(index)
            if option.get_attribute("form") == purchase_id:
                return True
            option_form = option.locator("xpath=ancestor::form[1]").first
            if option_form.count() and option_form.get_attribute("id") == purchase_id:
                return True
        return False

    def candidate_group_count(self) -> int:
        """返回配置 selector 命中的候选组数量（仅用于 failure diagnostics）。"""
        total = 0
        for config in self._model_configs():
            selector = str(config.get("group_selector") or "")
            if selector:
                total += self.page.locator(selector).count()
        return total

    def detect(self) -> Optional[SizeGroup]:
        """实时检测与主购买表单关联的 Size Group。"""
        for config in self._model_configs():
            model = str(config.get("id") or "")
            group_selector = str(config.get("group_selector") or "")
            option_selector = str(config.get("option_selector") or "")
            if not model or not group_selector or not option_selector:
                continue

            groups = self.page.locator(group_selector)
            for index in range(groups.count()):
                group = groups.nth(index)
                expected_attributes = config.get("required_attributes") or {}
                if not self._attribute_matches(group, expected_attributes):
                    continue
                expected_name = self._normalized(config.get("expected_name"))
                if expected_name and self._normalized(self._group_name(group)) != expected_name:
                    continue
                if not self._associated_with_purchase(group, option_selector):
                    continue
                return SizeGroup(
                    model=model,
                    locator=group,
                    option_selector=option_selector,
                    disabled_class_tokens=tuple(
                        self._normalized(token)
                        for token in (config.get("disabled_class_tokens") or [])
                        if self._normalized(token)
                    ),
                    custom_size_value=str(
                        config.get("custom_size_value")
                        or self._custom_measurement_config().get("trigger_value")
                        or DEFAULT_FREE_SIZE_MARKER
                    ),
                    config=config,
                )
        return None

    def require_group(self) -> SizeGroup:
        group = self.detect()
        if group is None:
            raise SizeGroupNotFoundError("No recognized Size Group in main purchase scope")
        return group

    # --------------------------------------------------------------- 选项
    def _display_text(self, radio, value: str) -> str:
        if value:
            return value
        radio_id = str(radio.get_attribute("id") or "")
        if radio_id:
            explicit = self.page.locator(f'label[for="{radio_id}"]').first
            if explicit.count():
                return explicit.inner_text().strip()
        wrapping = radio.locator("xpath=ancestor::label[1]").first
        if wrapping.count():
            return wrapping.inner_text().strip()
        sibling = radio.locator("xpath=following-sibling::label[1]").first
        if sibling.count():
            return sibling.inner_text().strip()
        return ""

    @staticmethod
    def _actionable_control(control) -> bool:
        """Return whether a real user can operate the option control."""
        if control is None:
            return False
        try:
            # The radio's disabled state is checked separately.  For labels
            # and native controls, visibility is the useful actionability
            # signal and avoids repeated cross-process count/enabled calls.
            return bool(control.is_visible())
        except Exception:
            return False

    def _associated_label(self, radio):
        """Resolve an explicit or wrapping label for a radio option."""
        wrapping = radio.locator("xpath=ancestor::label[1]").first
        if wrapping.count():
            return wrapping
        radio_id = str(radio.get_attribute("id") or "").strip()
        if radio_id:
            escaped_id = radio_id.replace("\\", "\\\\").replace('"', '\\"')
            explicit = self.page.locator(f'label[for="{escaped_id}"]').first
            if explicit.count():
                return explicit
        return None

    def _option_control(self, radio, config: dict):
        """Return the visible control used to operate a Size radio."""
        config = config if isinstance(config, dict) else {}
        strategy = str(config.get("control_strategy") or "").strip().lower()

        if strategy in {"radio", "native"}:
            return radio if self._actionable_control(radio) else None

        if strategy in {"associated_label", "label", "label_for"}:
            label = self._associated_label(radio)
            if self._actionable_control(label):
                return label
            return radio if self._actionable_control(radio) else None

        # Visible native radio inputs remain the default path.  Hidden radios
        # fall back to their explicit/wrapping label without requiring a
        # theme-specific wrapper class.
        if self._actionable_control(radio):
            return radio
        label = self._associated_label(radio)
        return label if self._actionable_control(label) else None

    @staticmethod
    def _has_disabled_token(locator, tokens: tuple[str, ...]) -> bool:
        if not tokens:
            return False
        classes = set(str(locator.get_attribute("class") or "").lower().split())
        return any(token in classes for token in tokens)

    def _is_available(
        self, radio, group: SizeGroup, value: str, control=None
    ) -> bool:
        if not value or radio.is_disabled():
            return False
        if self._normalized(radio.get_attribute("aria-disabled")) == "true":
            return False
        if self._has_disabled_token(radio, group.disabled_class_tokens):
            return False
        parent = radio.locator("xpath=parent::*[1]").first
        if parent.count() and self._has_disabled_token(
            parent, group.disabled_class_tokens
        ):
            return False
        control = control if control is not None else self._option_control(
            radio, group.config
        )
        if not self._actionable_control(control):
            return False
        if self._normalized(control.get_attribute("aria-disabled")) == "true":
            return False
        return not self._has_disabled_token(control, group.disabled_class_tokens)

    def _options_for(self, group: SizeGroup) -> list[SizeOption]:
        options = []
        radios = group.locator.locator(group.option_selector)
        custom_marker = self._normalized(group.custom_size_value)
        for index in range(radios.count()):
            radio = radios.nth(index)
            value = str(radio.get_attribute("value") or "").strip()
            display_text = self._display_text(radio, value).strip()
            normalized_value = value or display_text
            if not normalized_value:
                continue
            control = self._option_control(radio, group.config)
            options.append(
                SizeOption(
                    value=normalized_value,
                    display_text=display_text or normalized_value,
                    available=self._is_available(
                        radio, group, normalized_value, control
                    ),
                    selected=radio.is_checked(),
                    model=group.model,
                    custom_size=self._normalized(normalized_value) == custom_marker,
                    locator=radio,
                    control_locator=control,
                )
            )
        return options

    def options(self) -> list[SizeOption]:
        """实时返回当前组的全部非空选项，包括不可用与 Custom Size。"""
        return self._options_for(self.require_group())

    def available_options(self) -> list[SizeOption]:
        """实时返回可见、可用的选项；保留 Custom Size 以兼容 count 语义。"""
        return [option for option in self.options() if option.available]

    def normal_available_options(self) -> list[SizeOption]:
        return [option for option in self.available_options() if not option.custom_size]

    def first_available_value(self) -> str:
        normal = self.normal_available_options()
        for option in normal:
            if not option.selected:
                return option.value
        if normal:
            return normal[0].value
        raise RuntimeError("No available normal size option to select")

    def _current_selection_state(self, requested_value: str) -> dict[str, Any]:
        """Read selection metadata from the current DOM only.

        This helper deliberately returns aggregate state instead of exposing
        markup or locator details.  A theme may replace the complete size
        group between any two reads, so callers treat every result as a
        snapshot and re-detect on the next poll.
        """
        state: dict[str, Any] = {
            "model": None,
            "candidate_group_count": 0,
            "option_total": 0,
            "available_count": 0,
            "selected_current_dom": None,
            "target_option_present_current_dom": False,
            "target_option_available_current_dom": False,
            "group_present": False,
        }
        try:
            state["candidate_group_count"] = self.candidate_group_count()
        except Exception:
            pass

        try:
            group = self.detect()
            if group is None:
                return state
            state["group_present"] = True
            state["model"] = group.model
            options = self._options_for(group)
            state["option_total"] = len(options)
            state["available_count"] = sum(option.available for option in options)
            state["selected_current_dom"] = next(
                (option.value for option in options if option.selected), None
            )
            target = next(
                (option for option in options if option.value == requested_value),
                None,
            )
            state["target_option_present_current_dom"] = target is not None
            state["target_option_available_current_dom"] = bool(
                target is not None and target.available
            )
        except Exception:
            # A transient detach during a theme replacement is a normal
            # bounded-poll condition.  Keep the last safe aggregate shape.
            pass
        return state

    def _current_selectable_option(self, value: str) -> Optional[SizeOption]:
        """Return the requested option after re-parsing the current DOM."""
        try:
            options = self.options()
        except Exception:
            return None
        for option in options:
            if option.value != value:
                continue
            if option.custom_size:
                raise RuntimeError(
                    "Automatic Free Custom Size selection is not allowed"
                )
            if option.available and self._actionable_control(option.control_locator):
                return option
            return None
        return None

    def _wait_for_current_selectable_option(
        self, value: str, timeout_ms: int
    ) -> Optional[SizeOption]:
        """Boundedly wait for a fresh, actionable requested option."""
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        while True:
            option = self._current_selectable_option(value)
            if option is not None:
                return option
            if time.monotonic() >= deadline:
                return None
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                self.page.wait_for_timeout(
                    min(SIZE_SELECTION_POLL_INTERVAL_MS, remaining_ms)
                )
            except Exception:
                return None

    @staticmethod
    def _merge_selection_state(
        diagnostics: dict[str, Any], state: dict[str, Any]
    ) -> None:
        for key in (
            "model",
            "candidate_group_count",
            "option_total",
            "available_count",
            "selected_current_dom",
            "target_option_present_current_dom",
            "target_option_available_current_dom",
        ):
            diagnostics[key] = state.get(key)

    def _wait_selected_value(
        self,
        value: str,
        diagnostics: dict[str, Any],
        timeout_ms: int = SIZE_SELECTION_CONVERGENCE_TIMEOUT_MS,
    ) -> dict[str, Any]:
        """Wait until fresh DOM snapshots converge on the requested value.

        Two consecutive matching snapshots, with a short 100 ms settling
        window, prevent an old checked radio from being accepted just before
        a theme asynchronously replaces its group.  Every snapshot detects
        the group and parses its options again; no element identity is used
        as a business contract.
        """
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        started = time.monotonic()
        matching_snapshots = 0
        previous_group_present: Optional[bool] = None
        previous_option_total: Optional[int] = None
        final_state: dict[str, Any] = {}

        while True:
            state = self._current_selection_state(value)
            final_state = state
            self._merge_selection_state(diagnostics, state)

            if previous_group_present is not None and (
                previous_group_present != state["group_present"]
            ):
                diagnostics["rerender_observed"] = True
            if previous_option_total is not None and (
                previous_option_total != state["option_total"]
            ):
                diagnostics["rerender_observed"] = True
            previous_group_present = state["group_present"]
            previous_option_total = state["option_total"]

            immediate = diagnostics.get("checked_immediately_after_click")
            current = state["selected_current_dom"]
            if immediate is not None and (
                (immediate and current != value)
                or (not immediate and current == value)
            ):
                # This is a safe state-transition signal that a current DOM
                # read differs from the immediate post-click read.  It does
                # not rely on comparing element objects.
                diagnostics["rerender_observed"] = True

            elapsed_ms = int((time.monotonic() - started) * 1000)
            diagnostics["elapsed_ms"] = elapsed_ms
            if current == value:
                matching_snapshots += 1
            else:
                matching_snapshots = 0

            if (
                current == value
                and matching_snapshots >= 2
                and elapsed_ms >= min(SIZE_SELECTION_SETTLE_MS, timeout_ms)
            ):
                return final_state

            if time.monotonic() >= deadline:
                return final_state
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                self.page.wait_for_timeout(
                    min(SIZE_SELECTION_POLL_INTERVAL_MS, remaining_ms)
                )
            except Exception:
                return final_state

    @staticmethod
    def _selection_failure_message(diagnostics: dict[str, Any]) -> str:
        def display(value: Any, fallback: str = "NONE") -> Any:
            return fallback if value is None or value == "" else value

        return (
            "Size selection did not converge: "
            f"requested_size={diagnostics['requested_size']} "
            f"model={display(diagnostics.get('model'), 'UNKNOWN')} "
            f"candidate_group_count={diagnostics.get('candidate_group_count', 0)} "
            f"option_total={diagnostics.get('option_total', 0)} "
            f"available_count={diagnostics.get('available_count', 0)} "
            f"selected_before={display(diagnostics.get('selected_before'))} "
            "checked_immediately_after_click="
            f"{diagnostics.get('checked_immediately_after_click')} "
            f"selected_current_dom={display(diagnostics.get('selected_current_dom'))} "
            "target_option_present_current_dom="
            f"{diagnostics.get('target_option_present_current_dom', False)} "
            "target_option_available_current_dom="
            f"{diagnostics.get('target_option_available_current_dom', False)} "
            f"elapsed_ms={diagnostics.get('elapsed_ms', 0)} "
            f"rerender_observed={diagnostics.get('rerender_observed', False)} "
            f"attempts={diagnostics.get('attempts', 0)}"
        )

    def select(self, value: str) -> str:
        selection_started = time.monotonic()
        initial_state = self._current_selection_state(value)
        initial_options = self.options()
        requested = next(
            (option for option in initial_options if option.value == value), None
        )
        if requested is not None and requested.custom_size:
            raise RuntimeError("Automatic Free Custom Size selection is not allowed")
        if requested is None or not requested.available:
            raise LookupError(f"Size option unavailable: {value}")
        if not self._actionable_control(requested.control_locator):
            raise RuntimeError(f"Size option has no actionable control: {value}")

        diagnostics: dict[str, Any] = {
            "requested_size": value,
            "model": initial_state.get("model") or requested.model,
            "candidate_group_count": initial_state.get("candidate_group_count", 0),
            "option_total": initial_state.get("option_total", len(initial_options)),
            "available_count": initial_state.get(
                "available_count", sum(option.available for option in initial_options)
            ),
            "selected_before": initial_state.get("selected_current_dom")
            or next(
                (option.value for option in initial_options if option.selected), None
            ),
            "checked_immediately_after_click": None,
            "selected_current_dom": initial_state.get("selected_current_dom"),
            "target_option_present_current_dom": initial_state.get(
                "target_option_present_current_dom", True
            ),
            "target_option_available_current_dom": initial_state.get(
                "target_option_available_current_dom", requested.available
            ),
            "elapsed_ms": 0,
            "rerender_observed": False,
            "attempts": 0,
        }

        for attempt in range(1, SIZE_SELECTION_MAX_ATTEMPTS + 1):
            option = self._wait_for_current_selectable_option(
                value,
                SIZE_SELECTION_CONVERGENCE_TIMEOUT_MS,
            )
            if option is None:
                break

            diagnostics["attempts"] = attempt
            diagnostics["model"] = option.model
            try:
                option.control_locator.click()
            except Exception:
                # A detached control is handled like any other non-converging
                # current-DOM state; the next attempt must re-detect it.
                if attempt >= SIZE_SELECTION_MAX_ATTEMPTS:
                    break
                continue

            try:
                diagnostics["checked_immediately_after_click"] = bool(
                    option.locator.is_checked()
                )
            except Exception:
                diagnostics["checked_immediately_after_click"] = None

            state = self._wait_selected_value(
                value,
                diagnostics,
                SIZE_SELECTION_CONVERGENCE_TIMEOUT_MS,
            )
            if state.get("selected_current_dom") == value:
                return value

        final_state = self._current_selection_state(value)
        self._merge_selection_state(diagnostics, final_state)
        diagnostics["elapsed_ms"] = int(
            (time.monotonic() - selection_started) * 1000
        )
        raise RuntimeError(self._selection_failure_message(diagnostics))

    def selected_value(self) -> Optional[str]:
        for option in self.options():
            if option.selected:
                return option.value
        return None

    # --------------------------------------------------------- Custom metadata
    def measurement_fields(self) -> list[Any]:
        group = self.detect()
        if group is None:
            return []
        config = self._custom_measurement_config()
        selector = str(config.get("field_selector") or "input[type='text']")
        expected_names = {
            self._normalized(name) for name in (config.get("field_names") or [])
        }
        root = group.locator.locator("xpath=parent::*[1]")
        fields = root.locator(selector)
        matched = []
        for index in range(fields.count()):
            field = fields.nth(index)
            signature = self._normalized(
                " ".join(
                    str(field.get_attribute(name) or "")
                    for name in ("name", "id", "placeholder", "aria-label")
                )
            )
            if any(name in signature for name in expected_names):
                matched.append(field)
        return matched

    def snapshot(self) -> dict:
        """返回 failure-safe metadata，不输出 DOM/表单值。"""
        group = self.detect()
        if group is None:
            return {
                "size_model": None,
                "size_group_detected": False,
                "size_option_total": 0,
                "size_option_available": 0,
                "normal_size_available": 0,
                "custom_size_present": False,
                "custom_measurement_model": None,
                "selected_size": None,
                "candidate_group_count": self.candidate_group_count(),
                "measurement_field_count": 0,
            }
        options = self._options_for(group)
        available = [option for option in options if option.available]
        custom_present = any(option.custom_size for option in options)
        custom_model = str(
            self._custom_measurement_config().get("id") or SIZE_MODEL_03
        )
        return {
            "size_model": group.model,
            "size_group_detected": True,
            "size_option_total": len(options),
            "size_option_available": len(available),
            "normal_size_available": len(
                [option for option in available if not option.custom_size]
            ),
            "custom_size_present": custom_present,
            "custom_measurement_model": custom_model if custom_present else None,
            "selected_size": next(
                (option.value for option in options if option.selected), None
            ),
            "candidate_group_count": self.candidate_group_count(),
            "measurement_field_count": len(self.measurement_fields()),
        }

    # --------------------------------------------------------------- 等待
    def wait_for_available(self, timeout_ms: int) -> None:
        """Wait for an available option, including hidden-radio controls."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            group = self.detect()
            if group is not None:
                if self.available_options():
                    return
                wait_selector = str(
                    group.config.get("wait_option_selector") or group.option_selector
                )
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                try:
                    group.locator.locator(wait_selector).first.wait_for(
                        state="attached", timeout=min(remaining_ms, 250)
                    )
                except PlaywrightTimeoutError:
                    pass
            else:
                selectors = [
                    str(config.get("group_selector") or "")
                    for config in self._model_configs()
                    if config.get("group_selector")
                ]
                if not selectors:
                    raise SizeGroupNotFoundError("No Size Group detectors configured")
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                try:
                    self.page.locator(", ".join(selectors)).first.wait_for(
                        state="attached", timeout=min(remaining_ms, 250)
                    )
                except PlaywrightTimeoutError:
                    pass
            self.page.wait_for_timeout(50)
        raise PlaywrightTimeoutError("No actionable Size option became available")
