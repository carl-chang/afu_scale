"""AFU 体脂秤传感器实体"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import AfuScaleCoordinator

SENSOR_DEFS: dict[str, dict] = {
    "weight": {
        "name": "体重",
        "unit": "kg",
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 2,
    },
    "impedance": {
        "name": "电阻抗",
        "unit": "Ω",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 0,
    },
    "stable": {
        "name": "称重稳定",
        "unit": None,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 0,
    },
    "bmi": {
        "name": "BMI",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "body_fat": {
        "name": "体脂率",
        "unit": "%",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "water": {
        "name": "水分率",
        "unit": "%",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "muscle": {
        "name": "肌肉量",
        "unit": "kg",
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "protein": {
        "name": "蛋白质率",
        "unit": "%",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "bone": {
        "name": "骨量",
        "unit": "kg",
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 2,
    },
    "battery": {
        "name": "电量",
        "unit": "%",
        "device_class": SensorDeviceClass.BATTERY,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 0,
        "category": EntityCategory.DIAGNOSTIC,
    },
}


class AfuSensor(SensorEntity, RestoreEntity):
    """AFU 体脂秤传感器基类"""

    def __init__(self, coordinator: AfuScaleCoordinator, key: str) -> None:
        self._coordinator = coordinator
        self._key = key
        self._def = SENSOR_DEFS[key]
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_{key}"
        self._attr_name = f"AFU 体脂秤{self._def['name']}"
        self._attr_should_poll = False
        self._attr_native_unit_of_measurement = self._def.get("unit")
        if self._def.get("device_class"):
            self._attr_device_class = self._def["device_class"]
        if self._def.get("state_class"):
            self._attr_state_class = self._def["state_class"]
        if "precision" in self._def:
            self._attr_suggested_display_precision = self._def["precision"]
        if self._def.get("category"):
            self._attr_entity_category = self._def["category"]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.address)},
            name="AFU 体脂秤",
            manufacturer="沃莱科技",
            model="AFU-WL-TZ-A1",
        )

    async def async_added_to_hass(self) -> None:
        """重启后恢复上一次的测量值。"""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in ("unknown", "unavailable", ""):
                self._attr_native_value = last_state.state

    @callback
    def async_update_state(self, value) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class AfuTimestampSensor(AfuSensor):
    """记录最近一次测量时间"""

    def __init__(self, coordinator: AfuScaleCoordinator) -> None:
        super().__init__(coordinator, "weight")
        self._key = "timestamp"
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_timestamp"
        self._attr_name = "AFU 体脂秤最近测量时间"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_native_unit_of_measurement = None
        self._attr_state_class = None
        self._attr_suggested_display_precision = None

    async def async_added_to_hass(self) -> None:
        """重启后恢复上次测量时间（字符串转 datetime）。"""
        await super().async_added_to_hass()
        if self._attr_native_value is not None:
            if parsed := dt_util.parse_datetime(str(self._attr_native_value)):
                self._attr_native_value = parsed


class AfuRawDataSensor(AfuSensor):
    """诊断用：显示最近一条 0xFFB2 原始报文（十六进制），便于核对报文格式。"""

    def __init__(self, coordinator: AfuScaleCoordinator) -> None:
        super().__init__(coordinator, "weight")
        self._key = "raw_data"
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_raw_data"
        self._attr_name = "AFU 体脂秤原始报文"
        self._attr_device_class = None
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_native_unit_of_measurement = None
        self._attr_state_class = None
        self._attr_suggested_display_precision = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AfuScaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [AfuSensor(coordinator, key) for key in SENSOR_DEFS]
    entities.append(AfuTimestampSensor(coordinator))
    raw_data = AfuRawDataSensor(coordinator)
    entities.append(raw_data)
    async_add_entities(entities)
    coordinator.raw_data_entity = raw_data
    for entity in entities:
        coordinator.register_entity(entity._key, entity)
