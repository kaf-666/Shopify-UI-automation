"""PDP 尺码模型识别与统一选项抽象。

Resolver 先在主购买表单关联范围内识别语义化 Size Group，再通过对应
adapter 解析 radio。所有 public operation 都重新检测当前 DOM，不缓存
ElementHandle/JSHandle，因而可安全处理 Theme/SPB rerender 与模型切换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


SIZE_MODEL_01 = "SIZE_MODEL_01"
SIZE_MODEL_02 = "SIZE_MODEL_02"
SIZE_MODEL_03 = "SIZE_MODEL_03"
DEFAULT_FREE_SIZE_MARKER = "free custom size"


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
    def _has_disabled_token(locator, tokens: tuple[str, ...]) -> bool:
        if not tokens:
            return False
        classes = set(str(locator.get_attribute("class") or "").lower().split())
        return any(token in classes for token in tokens)

    def _is_available(self, radio, group: SizeGroup, value: str) -> bool:
        if not value or not radio.is_visible() or radio.is_disabled():
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
        return True

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
            options.append(
                SizeOption(
                    value=normalized_value,
                    display_text=display_text or normalized_value,
                    available=self._is_available(radio, group, normalized_value),
                    selected=radio.is_checked(),
                    model=group.model,
                    custom_size=self._normalized(normalized_value) == custom_marker,
                    locator=radio,
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

    def select(self, value: str) -> str:
        for option in self.available_options():
            if option.value != value:
                continue
            if option.custom_size:
                raise RuntimeError("Automatic Free Custom Size selection is not allowed")
            option.locator.check()
            if not option.locator.is_checked():
                raise RuntimeError(f"Size selection did not take effect: {value}")
            return value
        raise LookupError(f"Size option unavailable: {value}")

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
        """等待当前模型出现 option；模型未知时只等待候选 Group 注入。"""
        group = self.detect()
        if group is not None:
            wait_selector = str(
                group.config.get("wait_option_selector") or group.option_selector
            )
            group.locator.locator(wait_selector).first.wait_for(
                state="visible", timeout=timeout_ms
            )
            return
        selectors = [
            str(config.get("group_selector") or "")
            for config in self._model_configs()
            if config.get("group_selector")
        ]
        if not selectors:
            raise SizeGroupNotFoundError("No Size Group detectors configured")
        self.page.locator(", ".join(selectors)).first.wait_for(
            state="attached", timeout=timeout_ms
        )
