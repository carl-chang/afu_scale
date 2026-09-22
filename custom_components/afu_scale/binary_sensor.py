"""AFU 体脂秤"测量中"二进制传感器"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import AfuScaleCoordinator


class AfuMeasuringBinarySensor(BinarySensorEntity):
    """测量中：收到体重数据即开，无数据 15 秒后自动关。"""

    def __init__(self, coordinator: AfuScaleCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_measuring"
        self._attr_name = "AFU 体脂秤测量中"
        self._attr_device_class = BinarySensorDeviceClass.MOTION
        self._attr_should_poll = False
        self._attr_icon = "mdi:scale-bathroom"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.address)},
            name="AFU 体脂秤",
            manufacturer="沃莱科技",
            model="AFU-WL-TZ-A1",
        )

    @callback
    def async_update_state(self, value: bool) -> None:
        self._attr_is_on = value
        self.async_write_ha_state()


class AfuChargingBinarySensor(BinarySensorEntity, RestoreEntity):
    """充电中：电量字节 bit7 置位即为充电。"""

    def __init__(self, coordinator: AfuScaleCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_charging"
        self._attr_name = "AFU 体脂秤充电中"
        self._attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
        self._attr_should_poll = False
        self._attr_icon = "mdi:battery-charging"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.address)},
            name="AFU 体脂秤",
            manufacturer="沃莱科技",
            model="AFU-WL-TZ-A1",
        )

    async def async_added_to_hass(self) -> None:
        """重启后恢复充电状态。"""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == "on"

    @callback
    def async_update_state(self, value: bool) -> None:
        self._attr_is_on = value
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AfuScaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    entity = AfuMeasuringBinarySensor(coordinator)
    charging = AfuChargingBinarySensor(coordinator)
    async_add_entities([entity, charging])
    coordinator.measuring_entity = entity
    coordinator.charging_entity = charging
    if coordinator.measuring:
        entity.async_update_state(True)
