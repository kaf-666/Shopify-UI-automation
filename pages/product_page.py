"""商品详情（PDP）页面对象。

颜色 / 尺码选择基于真实表单状态（radio.checked）判定。尺码通过
SizeOptionResolver 先识别 Group/Model，再归一化 option；class 仅参与
MODEL_02 的不可用状态识别，不作为 selected state。提供加购按钮定位
与真实加购点击。
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect

from pages.base_page import BasePage
from pages.size_option_resolver import (
    DEFAULT_FREE_SIZE_MARKER,
    SizeGroupNotFoundError,
    SizeOptionResolver,
)

FREE_SIZE_MARKER = DEFAULT_FREE_SIZE_MARKER
# A short per-node probe keeps readiness bounded while allowing a busy
# storefront to resolve a freshly hydrated Locator under CI load.
READINESS_SNAPSHOT_PROBE_TIMEOUT_MS = 250
READINESS_POLL_QUANTUM_MS = 250


class PurchaseAreaReadinessError(TimeoutError):
    """购买区未在 bounded timeout 内进入稳定可测试状态。"""


class ProductPage(BasePage):
    """商品详情页对象：标题/价格/图集读取、颜色与尺码选择、真实加购。"""

    PAGE_NAME = "product"

    def open(self) -> None:
        super().open(ready_selector="title")

    # ------------------------------------------------------------------- 读取
    def title(self):
        """返回商品标题定位器。"""
        return self.locator("title").filter(visible=True).first

    def get_title(self) -> str:
        """返回商品标题文本。"""
        title = self.title()
        title.wait_for(state="visible", timeout=15_000)
        return title.inner_text().strip()

    def price(self):
        """返回价格定位器。"""
        return self.locator("price").filter(visible=True).first

    def get_price(self) -> str:
        """返回价格文本。"""
        price = self.price()
        price.wait_for(state="visible", timeout=15_000)
        return price.inner_text().strip()

    def gallery(self):
        """返回图集定位器。"""
        return self.locator("gallery").first

    def purchase_area(self):
        """返回主商品购买表单根节点。"""
        return self.locator("purchase_area").first

    # ------------------------------------------------------------------- 选项
    def color_group(self):
        """返回颜色选项组定位器（fieldset[name=Color]）。"""
        return self.locator("color").first

    def color_options(self):
        """返回颜色 radio 选项定位器集合。"""
        return self.color_group().locator('input[type="radio"]')

    def _size_resolver(self) -> SizeOptionResolver:
        """创建无 DOM 缓存的实时尺码解析器。"""
        return SizeOptionResolver(self.page, self.page_config(), self.purchase_area)

    def add_to_cart_button(self):
        """返回加购按钮定位器。"""
        return self.locator("add_to_cart").first

    def add_to_cart(self) -> None:
        """通过真实 UI 点击加购按钮（本方法不做抽屉断言）。"""
        button = self.add_to_cart_button()
        if not button.is_visible():
            raise RuntimeError("Add To Cart button is not visible")
        if not button.is_enabled():
            raise RuntimeError("Add To Cart button is not enabled")
        button.click()

    @staticmethod
    def _radio_value(radio) -> str:
        """读取 radio 的选项值：优先 value 属性，空时回退关联 label / 父容器文本。"""
        v = radio.get_attribute("value")
        if v and v.strip():
            return v.strip()
        return (
            radio.evaluate(
                """el => {
                    const label = el.closest('label');
                    if (label && label.textContent) return label.textContent.trim();
                    const p = el.parentElement;
                    return p ? p.textContent.trim() : '';
                }"""
            )
            or ""
        ).strip()

    @staticmethod
    def _actionable_control(
        control, timeout_ms: Optional[int] = READINESS_SNAPSHOT_PROBE_TIMEOUT_MS
    ) -> bool:
        """Return whether a real user can operate the associated control."""
        if control is None:
            return False
        try:
            # Locator construction/count checks are intentionally avoided
            # here.  Callers already obtained the control from a live radio;
            # one visibility query is sufficient for labels and native
            # controls, while radio disabled state is checked separately.
            return bool(control.is_visible(timeout=timeout_ms))
        except Exception:
            return False

    @staticmethod
    def _normalized(value: Optional[str]) -> str:
        return " ".join(str(value or "").split()).strip().lower()

    @classmethod
    def _has_disabled_token(
        cls,
        locator,
        tokens: tuple[str, ...],
        timeout_ms: Optional[int] = READINESS_SNAPSHOT_PROBE_TIMEOUT_MS,
    ) -> bool:
        if locator is None or not tokens:
            return False
        try:
            classes = set(
                cls._normalized(
                    locator.get_attribute(
                        "class", timeout=timeout_ms
                    )
                ).split()
            )
        except Exception:
            # A single transient DOM replacement must not collapse the whole
            # option group to color_count=0; the next readiness poll resolves
            # a fresh locator and rechecks the node.
            return False
        return any(token in classes for token in tokens)

    def _associated_label(
        self,
        radio,
        timeout_ms: Optional[int] = READINESS_SNAPSHOT_PROBE_TIMEOUT_MS,
    ):
        """Resolve an explicit or wrapping label for a radio option."""
        wrapping = radio.locator("xpath=ancestor::label[1]").first
        if wrapping.count():
            return wrapping
        radio_id = str(radio.get_attribute("id", timeout=timeout_ms) or "").strip()
        if radio_id:
            escaped_id = radio_id.replace("\\", "\\\\").replace('"', '\\"')
            explicit = self.page.locator(f'label[for="{escaped_id}"]').first
            if explicit.count():
                return explicit
        return None

    def _variant_input_control(self, radio):
        """Keep the legacy variant-input click as an optional fallback."""
        return radio.locator(
            "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
            "' variant-input ')][1]"
        ).first

    def _option_control(
        self,
        radio,
        config: Optional[dict] = None,
        timeout_ms: Optional[int] = READINESS_SNAPSHOT_PROBE_TIMEOUT_MS,
    ):
        """Return the real user-facing control for a radio option.

        Custom Shopify option widgets often hide the semantic radio and bind
        the interaction to a visible label.  The strategy is deliberately
        small and configuration-driven; visible native radios remain fully
        supported, while ``variant-input`` is only a backwards-compatible
        fallback for existing themes.
        """
        config = config if isinstance(config, dict) else {}
        strategy = str(
            config.get("strategy") or config.get("control_strategy") or ""
        ).strip().lower()

        if strategy in {"radio", "native"}:
            return (
                radio
                if self._actionable_control(radio, timeout_ms=timeout_ms)
                else None
            )

        if strategy in {"variant_input", "variant-container"}:
            control = self._variant_input_control(radio)
            return (
                control
                if self._actionable_control(control, timeout_ms=timeout_ms)
                else None
            )

        if strategy in {"associated_label", "label", "label_for"}:
            label = self._associated_label(radio, timeout_ms=timeout_ms)
            if self._actionable_control(label, timeout_ms=timeout_ms):
                return label
            # A configured label strategy must not make a visible native
            # radio unusable when a theme omits the label association.
            return (
                radio
                if self._actionable_control(radio, timeout_ms=timeout_ms)
                else None
            )

        # Default behavior preserves the existing visible-radio path and its
        # variant-input fallback, then supports hidden radios generically.
        if self._actionable_control(radio, timeout_ms=timeout_ms):
            variant = self._variant_input_control(radio)
            if self._actionable_control(variant, timeout_ms=timeout_ms):
                return variant
            return radio
        label = self._associated_label(radio, timeout_ms=timeout_ms)
        return (
            label
            if self._actionable_control(label, timeout_ms=timeout_ms)
            else None
        )

    def _color_option_control_config(self) -> dict:
        config = self.page_config().get("color_option_control") or {}
        return config if isinstance(config, dict) else {}

    def _color_disabled_class_tokens(self) -> tuple[str, ...]:
        config = self._color_option_control_config()
        configured = config.get("disabled_class_tokens")
        values = configured or ("disabled", "sold-out", "unavailable")
        return tuple(
            self._normalized(token) for token in values if self._normalized(token)
        )

    def _color_option_control_if_available(
        self,
        radio,
        control_config: Optional[dict] = None,
        timeout_ms: Optional[int] = READINESS_SNAPSHOT_PROBE_TIMEOUT_MS,
    ):
        """Return one actionable color control, or ``None``."""
        try:
            if radio.is_disabled(timeout=timeout_ms):
                return None
            if (
                self._normalized(
                    radio.get_attribute("aria-disabled", timeout=timeout_ms)
                )
                == "true"
            ):
                return None
        except Exception:
            return None
        disabled_tokens = self._color_disabled_class_tokens()
        if self._has_disabled_token(
            radio, disabled_tokens, timeout_ms=timeout_ms
        ):
            return None
        parent = radio.locator("xpath=parent::*[1]").first
        if parent.count() and self._has_disabled_token(
            parent, disabled_tokens, timeout_ms=timeout_ms
        ):
            return None
        control = self._option_control(
            radio, control_config, timeout_ms=timeout_ms
        )
        if not self._actionable_control(control, timeout_ms=timeout_ms):
            return None
        try:
            if (
                self._normalized(
                    control.get_attribute("aria-disabled", timeout=timeout_ms)
                )
                == "true"
            ):
                return None
        except Exception:
            return None
        if self._has_disabled_token(control, disabled_tokens, timeout_ms=timeout_ms):
            return None
        return control

    def _color_option_available(
        self,
        radio,
        control_config: Optional[dict] = None,
        timeout_ms: Optional[int] = READINESS_SNAPSHOT_PROBE_TIMEOUT_MS,
    ) -> bool:
        """Check one color option without enumerating its siblings."""
        return (
            self._color_option_control_if_available(
                radio, control_config, timeout_ms=timeout_ms
            )
            is not None
        )

    def available_options(self, options, control_config: Optional[dict] = None):
        """Return ``[(value, radio)]`` for semantically available options.

        A hidden radio is still available when its associated visible control
        is actionable.  The radio's disabled state remains authoritative.
        """
        result = []
        total = options.count()
        for i in range(total):
            radio = options.nth(i)
            control = self._color_option_control_if_available(
                radio, control_config, timeout_ms=None
            )
            if control is None:
                continue
            value = self._radio_value(radio)
            if not value or not control:
                continue
            result.append((value, radio))
        return result

    def available_color_count(self) -> int:
        """Return the number of available color options."""
        return len(
            self.available_options(
                self.color_options(), self._color_option_control_config()
            )
        )

    def has_actionable_color(self, deadline: Optional[float] = None) -> bool:
        """Return whether at least one color can be operated right now.

        Readiness deliberately short-circuits on the first actionable option.
        Full color enumeration remains available through ``available_options``
        for actual variant selection and post-readiness business behavior.
        """
        options = self.color_options()
        try:
            total = options.count()
        except Exception:
            return False
        for index in range(total):
            if deadline is not None and time.monotonic() >= deadline:
                return False
            probe_timeout = READINESS_SNAPSHOT_PROBE_TIMEOUT_MS
            if deadline is not None:
                probe_timeout = min(
                    probe_timeout,
                    max(1, int((deadline - time.monotonic()) * 1000)),
                )
            if self._color_option_available(
                options.nth(index),
                self._color_option_control_config(),
                timeout_ms=probe_timeout,
            ):
                return True
        return False

    def available_size_count(self) -> int:
        """返回可用尺码数；兼容计入 Free Custom Size 的历史语义。"""
        try:
            return len(self._size_resolver().available_options())
        except SizeGroupNotFoundError:
            return 0

    @staticmethod
    def _safe_state(check, default=False):
        try:
            return check()
        except Exception:
            return default

    @staticmethod
    def _empty_readiness_snapshot() -> dict:
        return {
            "purchase_area_count": 0,
            "purchase_area_attached": False,
            "title_visible": False,
            "color_count": 0,
            "color_ready": False,
            "size_count": 0,
            "size_model": None,
            "size_group_detected": False,
            "size_option_total": 0,
            "normal_size_available": 0,
            "custom_size_present": False,
            "selected_size": None,
            "candidate_group_count": 0,
            "atc_count": 0,
            "atc_probe_skipped": True,
            "atc_visible": False,
            "atc_enabled": False,
            "readiness_cost_ms": {},
        }

    def _readiness_remaining_ms(self) -> Optional[int]:
        deadline = getattr(self, "_readiness_deadline", None)
        if deadline is None:
            return None
        return max(0, int((deadline - time.monotonic()) * 1000))

    def _readiness_probe_timeout_ms(self) -> int:
        remaining_ms = self._readiness_remaining_ms()
        if remaining_ms is None:
            return READINESS_SNAPSHOT_PROBE_TIMEOUT_MS
        return max(1, min(READINESS_SNAPSHOT_PROBE_TIMEOUT_MS, remaining_ms))

    def _readiness_snapshot(self) -> dict:
        """Read a cheap, bounded, live-DOM readiness snapshot.

        The purchase root is the structural gate.  Until it is attached we
        intentionally do not run full size/color semantic enumeration, which
        prevents a mixed snapshot assembled across a theme rerender.  Business
        option enumeration remains available through the normal resolver APIs.
        """
        root = self.purchase_area()
        title = self.title()
        atc = self.add_to_cart_button()
        costs = {}

        def timed(name, check, default):
            started = time.monotonic()
            value = self._safe_state(check, default)
            costs[name] = max(0, int((time.monotonic() - started) * 1000))
            return value

        probe_timeout = self._readiness_probe_timeout_ms()
        purchase_count = int(timed("root_check_ms", root.count, 0))
        purchase_attached = purchase_count > 0
        title_visible = bool(
            timed(
                "title_check_ms",
                lambda: title.is_visible(timeout=probe_timeout),
                False,
            )
        )

        if not purchase_attached:
            # Keep this branch structural and cheap.  In particular, do not
            # resolve color options or size associations while the purchase
            # form is absent; those reads can observe a later DOM generation.
            candidate_group_count = int(
                timed(
                    "candidate_group_count_ms",
                    lambda: self._size_resolver().candidate_group_count(
                        deadline=(
                            getattr(self, "_readiness_deadline", None)
                        )
                    ),
                    0,
                )
            )
            return {
                **self._empty_readiness_snapshot(),
                "purchase_area_count": purchase_count,
                "title_visible": title_visible,
                "candidate_group_count": candidate_group_count,
                "readiness_cost_ms": costs,
            }

        deadline = getattr(self, "_readiness_deadline", None)
        size = timed(
            "size_check_ms",
            lambda: self._size_resolver().readiness_snapshot(deadline=deadline),
            {},
        )
        color_ready = bool(
            timed(
                "color_check_ms",
                lambda: self.has_actionable_color(deadline=deadline),
                False,
            )
        )
        atc_count = int(timed("atc_count_ms", atc.count, 0))
        atc_visible = False
        atc_enabled = False
        if atc_count > 0:
            atc_visible = bool(
                timed(
                    "atc_visible_check_ms",
                    lambda: atc.is_visible(
                        timeout=self._readiness_probe_timeout_ms()
                    ),
                    False,
                )
            )
            atc_enabled = bool(
                timed(
                    "atc_enabled_check_ms",
                    lambda: atc.is_enabled(
                        timeout=self._readiness_probe_timeout_ms()
                    ),
                    False,
                )
            )
        return {
            "purchase_area_count": purchase_count,
            "purchase_area_attached": True,
            "title_visible": title_visible,
            # Readiness is an existence contract; business selection retains
            # the complete available_options enumeration and exact counts.
            "color_count": 1 if color_ready else 0,
            "color_ready": color_ready,
            "size_count": int(size.get("size_option_available", 0)),
            "size_model": size.get("size_model"),
            "size_group_detected": bool(size.get("size_group_detected", False)),
            "size_option_total": int(size.get("size_option_total", 0)),
            "normal_size_available": int(size.get("normal_size_available", 0)),
            "custom_size_present": bool(size.get("custom_size_present", False)),
            "selected_size": size.get("selected_size"),
            "candidate_group_count": int(size.get("candidate_group_count", 0)),
            "atc_count": atc_count,
            "atc_probe_skipped": False,
            "atc_visible": atc_visible,
            "atc_enabled": atc_enabled,
            "readiness_cost_ms": costs,
        }

    @staticmethod
    def _missing_readiness_conditions(snapshot: dict) -> list[str]:
        """Return failed gates from the canonical purchase-readiness contract."""
        conditions = (
            ("purchase_area", bool(snapshot["purchase_area_attached"])),
            ("title", bool(snapshot["title_visible"])),
            ("color", snapshot["color_count"] > 0),
            ("size", snapshot["size_count"] > 0),
            ("atc_visible", bool(snapshot["atc_visible"])),
            ("atc_enabled", bool(snapshot["atc_enabled"])),
        )
        return [name for name, ready in conditions if not ready]

    @classmethod
    def _snapshot_ready(cls, snapshot: dict) -> bool:
        return not cls._missing_readiness_conditions(snapshot)

    @staticmethod
    def _emit_readiness_diagnostic(
        snapshot: dict,
        started: float,
        diagnostics_hook: Optional[Callable[[dict], None]],
    ) -> None:
        """Emit one safe snapshot without allowing diagnostics to affect flow."""
        if diagnostics_hook is None:
            return
        safe_snapshot = {
            "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
            "poll_count": int(snapshot.get("poll_count", 0)),
            "deadline_ms": int(snapshot.get("deadline_ms", 0)),
            "last_poll_duration_ms": int(snapshot.get("last_poll_duration_ms", 0)),
            "max_poll_duration_ms": int(snapshot.get("max_poll_duration_ms", 0)),
            "purchase_area_count": int(snapshot.get("purchase_area_count", 0)),
            "purchase_area_attached": bool(snapshot["purchase_area_attached"]),
            "title_visible": bool(snapshot["title_visible"]),
            "color_count": int(snapshot["color_count"]),
            "color_ready": bool(snapshot.get("color_ready", snapshot["color_count"] > 0)),
            "size_count": int(snapshot["size_count"]),
            "size_group_detected": bool(snapshot["size_group_detected"]),
            "size_option_total": int(snapshot["size_option_total"]),
            "normal_size_available": int(snapshot["normal_size_available"]),
            "candidate_group_count": int(snapshot.get("candidate_group_count", 0)),
            "selected_size": snapshot["selected_size"],
            "atc_count": int(snapshot.get("atc_count", 0)),
            "atc_probe_skipped": bool(snapshot.get("atc_probe_skipped", False)),
            "atc_visible": bool(snapshot["atc_visible"]),
            "atc_enabled": bool(snapshot["atc_enabled"]),
            "readiness_cost_ms": dict(snapshot.get("readiness_cost_ms") or {}),
        }
        try:
            diagnostics_hook(safe_snapshot)
        except Exception:
            # Instrumentation must never change the readiness result.
            pass

    def _wait_for_missing_readiness_condition(self, snapshot: dict, timeout_ms: int) -> None:
        if not snapshot["purchase_area_attached"]:
            self.purchase_area().wait_for(state="attached", timeout=timeout_ms)
        elif not snapshot["title_visible"]:
            self.title().wait_for(state="visible", timeout=timeout_ms)
        elif snapshot["color_count"] == 0:
            self._wait_for_color_available(timeout_ms)
        elif snapshot["size_count"] == 0:
            self._size_resolver().wait_for_available(timeout_ms)
        elif not snapshot["atc_visible"]:
            self.add_to_cart_button().wait_for(state="visible", timeout=timeout_ms)
        elif not snapshot["atc_enabled"]:
            try:
                expect(self.add_to_cart_button()).to_be_enabled(timeout=timeout_ms)
            except AssertionError as exc:
                # Playwright's expect API reports a bounded enabled-state
                # timeout as AssertionError rather than TimeoutError.  Map it
                # to the same poll-expired path so the hard deadline remains
                # authoritative.
                raise PlaywrightTimeoutError("Add To Cart did not enable") from exc

    def _wait_for_color_available(self, timeout_ms: int) -> None:
        """Wait for a semantic color option, including hidden-radio widgets."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.has_actionable_color(deadline=deadline):
                return
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                self.color_options().first.wait_for(
                    state="attached", timeout=min(
                        remaining_ms, READINESS_POLL_QUANTUM_MS
                    )
                )
            except PlaywrightTimeoutError:
                pass
            # The bounded locator wait above is the polling quantum.  Do not
            # append a fixed sleep: doing so could consume the same remaining
            # budget twice and let one condition wait cross the hard deadline.
        raise PlaywrightTimeoutError("No actionable color option became available")

    def wait_purchase_ready(
        self,
        timeout_ms: int = 15_000,
        diagnostics_hook: Optional[Callable[[dict], None]] = None,
    ) -> tuple[int, int, bool]:
        """Wait for purchase readiness inside one hard wall-clock deadline.

        Every poll uses live locators and a bounded readiness snapshot.  The
        poll quantum is deliberately independent of diagnostics so enabling
        structured logging cannot change production timeout semantics.
        """
        started = time.monotonic()
        timeout_ms = max(0, int(timeout_ms))
        deadline = started + timeout_ms / 1000
        poll_count = 0
        last_poll_duration_ms = 0
        max_poll_duration_ms = 0
        initial = self._empty_readiness_snapshot()
        final = initial
        had_deadline = hasattr(self, "_readiness_deadline")
        previous_deadline = getattr(self, "_readiness_deadline", None)
        self._readiness_deadline = deadline
        try:
            while True:
                remaining_ms = int(max(0, (deadline - time.monotonic()) * 1000))
                if remaining_ms <= 0:
                    break

                poll_started = time.monotonic()
                snapshot = self._readiness_snapshot()
                poll_count += 1
                last_poll_duration_ms = int(
                    max(0, (time.monotonic() - poll_started) * 1000)
                )
                max_poll_duration_ms = max(
                    max_poll_duration_ms, last_poll_duration_ms
                )
                snapshot = dict(snapshot)
                snapshot.update(
                    {
                        "poll_count": poll_count,
                        "elapsed_ms": int(
                            max(0, (time.monotonic() - started) * 1000)
                        ),
                        "deadline_ms": timeout_ms,
                        "last_poll_duration_ms": last_poll_duration_ms,
                        "max_poll_duration_ms": max_poll_duration_ms,
                    }
                )
                if poll_count == 1:
                    initial = snapshot
                final = snapshot
                self._emit_readiness_diagnostic(
                    snapshot, started, diagnostics_hook
                )

                # A poll that finishes after the deadline is not a legal PASS,
                # even if its final DOM read happens to look ready.
                if time.monotonic() >= deadline:
                    break
                if self._snapshot_ready(snapshot):
                    return snapshot["color_count"], snapshot["size_count"], True

                remaining_ms = int(
                    max(0, (deadline - time.monotonic()) * 1000)
                )
                if remaining_ms <= 0:
                    break
                try:
                    self._wait_for_missing_readiness_condition(
                        snapshot,
                        min(READINESS_POLL_QUANTUM_MS, remaining_ms),
                    )
                except PlaywrightTimeoutError:
                    pass
        finally:
            if had_deadline:
                self._readiness_deadline = previous_deadline
            else:
                try:
                    del self._readiness_deadline
                except AttributeError:
                    pass

        failing_conditions = self._missing_readiness_conditions(final)
        raise PurchaseAreaReadinessError(
            "purchase_area_attached="
            f"{final['purchase_area_attached']} "
            f"purchase_area_count={final.get('purchase_area_count', 0)} "
            f"purchase_area_initial={initial['purchase_area_attached']} "
            f"purchase_area_final={final['purchase_area_attached']} "
            f"title_visible_initial={initial['title_visible']} "
            f"title_visible_final={final['title_visible']} "
            f"size_count_initial={initial['size_count']} "
            f"size_count_final={final['size_count']} "
            f"color_count_initial={initial['color_count']} "
            f"color_count_final={final['color_count']} "
            f"size_model={final['size_model'] or 'UNKNOWN'} "
            f"size_group_detected={final['size_group_detected']} "
            f"size_option_total={final['size_option_total']} "
            f"normal_size_available={final['normal_size_available']} "
            f"custom_size_present={final['custom_size_present']} "
            f"selected_size={final['selected_size'] or 'NONE'} "
            f"candidate_group_count={final['candidate_group_count']} "
            f"atc_visible_initial={initial['atc_visible']} "
            f"atc_visible_final={final['atc_visible']} "
            f"atc_count_initial={initial.get('atc_count', 0)} "
            f"atc_count_final={final.get('atc_count', 0)} "
            f"atc_enabled_initial={initial['atc_enabled']} "
            f"atc_enabled_final={final['atc_enabled']} "
            f"atc_visible={final['atc_visible']} "
            f"atc_enabled={final['atc_enabled']} "
            f"failing_conditions={','.join(failing_conditions) or 'NONE'} "
            f"readiness_timeout_ms={timeout_ms} "
            f"poll_count={poll_count} "
            f"elapsed_ms={int(max(0, (time.monotonic() - started) * 1000))} "
            f"deadline_ms={timeout_ms} "
            f"last_poll_duration_ms={last_poll_duration_ms} "
            f"max_poll_duration_ms={max_poll_duration_ms}"
        )

    def _find_option(self, options, value: str, missing_msg: str):
        """按值查找可用选项，找不到抛出明确异常。"""
        for v, radio in self.available_options(
            options, self._color_option_control_config()
        ):
            if v == value:
                return radio
        raise LookupError(missing_msg)

    def first_available_color(self) -> str:
        """返回第一个可见可用且当前未选中的颜色选项值。"""
        for v, radio in self.available_options(
            self.color_options(), self._color_option_control_config()
        ):
            if not radio.is_checked():
                return v
        raise RuntimeError("No available color option to select")

    def first_available_size(self) -> str:
        """返回第一个可见可用的普通尺码值（排除 Free Custom Size）。"""
        try:
            return self._size_resolver().first_available_value()
        except SizeGroupNotFoundError as exc:
            raise RuntimeError("No available normal size option to select") from exc

    def select_color(self, value: Optional[str] = None) -> str:
        """选择颜色。

        未指定 value 时自动选择第一个可见可用且未选中的颜色。
        主题可以把点击绑定在 radio 的可见关联控件上；隐藏 radio
        不直接执行 check/JS click，而是走真实用户控件点击，
        再用 radio.checked 验证选择生效。
        """
        if value is None:
            value = self.first_available_color()
        radio = self._find_option(
            self.color_options(), value, f"Color option not found: {value}"
        )
        control = self._option_control(radio, self._color_option_control_config())
        if control is None:
            raise RuntimeError(f"Color option has no actionable control: {value}")
        control.click()
        if not radio.is_checked():
            raise RuntimeError(f"Color selection did not take effect: {value}")
        return value

    def select_size(self, value: Optional[str] = None) -> str:
        """选择一个可用的普通尺码。

        未指定 value 时自动选择第一个可见且可用的普通尺码，
        并排除 Free Custom Size。
        """
        if value is None:
            value = self.first_available_size()
        return self._size_resolver().select(value)

    def get_selected_color(self) -> Optional[str]:
        """返回当前选中的颜色值（基于 radio.checked 真实表单状态）。"""
        for i in range(self.color_options().count()):
            radio = self.color_options().nth(i)
            if radio.is_checked():
                return self._radio_value(radio)
        return None

    def get_selected_size(self) -> Optional[str]:
        """返回当前选中的尺码值（基于 radio.checked 真实表单状态）。"""
        try:
            return self._size_resolver().selected_value()
        except SizeGroupNotFoundError:
            return None
