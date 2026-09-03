"""AFU 体脂秤 BLE 连接协调器。

负责：
  - 通过 HA 蓝牙集成（含 ESP32 蓝牙代理）获取可连接设备
  - 主动连接体脂秤，订阅 0xFFB2 通知
  - 解析 0xAC 报文（体重/稳定/阻抗），并计算 BIA 指标
  - 连接断开后自动重连

"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    CONNECT_ATTEMPTS,
    CONNECT_TIMEOUT,
    DOMAIN,
    HANDSHAKE,
    NOTIFY_CHAR_UUID,
    PACKET_MAGIC,
    RECONNECT_DELAY,
    SERVICE_UUID,
    STABLE_FLAG,
    WRITE_CHAR_UUID,
)

if TYPE_CHECKING:
    from .sensor import AfuSensor

_LOGGER = logging.getLogger(__name__)


def parse_packet(data: bytes):
    """解析 0xAC 体重报文，返回 (weight_kg, is_stable, impedance, battery, charging) 或 None。

      逆向自官方 App（com.afu.afudevicemanager / aijk_hardware_sdk）：
      原始 20 字节报文 = IC V27 分帧（[18] 报文类型、[19] 校验和），实测示例：
        ac 29 80 68 f4 56 02 00 05 40 00 64 00 00 00 00 00 29 d5 1b
      [0]  0xAC 魔数
      [1]  device_type
      [3]  体重高位 (-0x68 偏移，0x68 为状态位)
      [4:6] 体重中低位
      [6]  0x02 表示测量/锁定稳定（IC 协议中即 mode 字节）
      [7]  mode==1 时的电量字节（bit7=充电中，低 7 位=电量%）
      [8:10] 阻抗 (Big Endian)
      [11] mode==2 时的电量字节（4 字节 support_funs 之后，bit7=充电中，低 7 位=电量%）
    """
    if len(data) < 10 or data[0] != PACKET_MAGIC:
        return None
    raw_weight = (data[3] - 0x68) * 65536 + data[4] * 256 + data[5]
    if raw_weight < 0:
        return None
    weight_kg = raw_weight / 1000.0
    is_stable = data[6] == STABLE_FLAG
    impedance = (data[8] << 8) | data[9]
    # 过滤无效读数：体重<=0（称重结束）或阻抗过低（人已离开）
    if weight_kg <= 0.0 or impedance < 500.0:
        return None
    battery = None
    charging = False
    # 电量位置取决于 mode 字节：1 在 data[7]，2 在 data[11]
    idx = 11 if data[6] == 0x02 else 7
    if len(data) > idx:
        raw = data[idx] & 0xFF
        level = raw & 0x7F
        # 0x7F/0xFF 为"未知/无电量数据"，超过 100 的取值同样无效
        if 0 < level <= 100:
            battery = level
            charging = bool(raw & 0x80)
    return weight_kg, is_stable, impedance, battery, charging


class AfuScaleCoordinator:
    """管理与体脂秤的 BLE 连接并分发测量数据。"""

    def __init__(self, hass: HomeAssistant, address: str, height_cm: float,
                 sex: str, age: int) -> None:
        self.hass = hass
        self.address = address
        self.height_cm = height_cm
        self.male = sex == "male"
        self.age = age

        self._client: BleakClient | None = None
        self._connect_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._shutdown = False
        self.entities: dict[str, AfuSensor] = {}

        # 测量中状态：收到有效数据包置 True，无数据超时后置 False
        self.measuring = False
        self._last_data_at: float = 0.0
        self._idle_handle: asyncio.TimerHandle | None = None
        self.measuring_entity = None
        self.charging_entity = None
        self.raw_data_entity = None

    def _set_measuring(self, value: bool) -> None:
        if self.measuring == value:
            return
        self.measuring = value
        if self.measuring_entity is not None:
            self.measuring_entity.async_update_state(value)

    def _set_charging(self, value: bool) -> None:
        if self.charging_entity is not None:
            self.charging_entity.async_update_state(value)

    @callback
    def register_entity(self, key: str, entity: AfuSensor) -> None:
        self.entities[key] = entity

    def start(self) -> None:
        self._shutdown = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._shutdown = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _run(self) -> None:
        while not self._shutdown:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("AFU Scale 连接异常: %s", exc)
            if self._shutdown:
                break
            await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_listen(self) -> None:
        async with self._connect_lock:
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if device is None:
                _LOGGER.debug("AFU Scale: 设备未在蓝牙集成中出现，等待重试")
                return
            try:
                client = await establish_connection(
                    BleakClient,
                    device,
                    self.address,
                    timeout=CONNECT_TIMEOUT,
                    max_attempts=CONNECT_ATTEMPTS,
                )
            except BleakError as exc:
                _LOGGER.warning("AFU Scale: 连接失败 %s: %s", self.address, exc)
                return
            self._client = client
            _LOGGER.info("AFU Scale: 已连接 %s", self.address)

        try:
            await self._subscribe(client)
            # 保持连接；断开会话自然结束，回到重连循环
            while client.is_connected and not self._shutdown:
                await asyncio.sleep(5)
        finally:
            self._client = None
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _subscribe(self, client: BleakClient) -> None:
        # 发送握手包（写 0xFFB1，无响应写），触发完整数据推送
        try:
            await client.write_gatt_char(WRITE_CHAR_UUID, HANDSHAKE, response=False)
            _LOGGER.debug("AFU Scale: 已发送握手包")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("AFU Scale: 握手包发送失败（忽略）: %s", exc)

        await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        _LOGGER.debug("AFU Scale: 已订阅 0xFFB2 通知")

    def _on_notify(self, _characteristic, data: bytes) -> None:
        """bleak 通知回调（在事件循环中调用）。"""
        if self.raw_data_entity is not None:
            self.raw_data_entity.async_update_state(data.hex(" "))
        parsed = parse_packet(bytes(data))
        if parsed is None:
            return
        weight_kg, is_stable, impedance, battery, charging = parsed
        _LOGGER.debug(
            "AFU Scale: %.2fkg stable=%s impedance=%.0fΩ battery=%s",
            weight_kg, is_stable, impedance, battery,
        )
        self._set_measuring(True)
        self._last_data_at = time.monotonic()
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        self._idle_handle = self.hass.loop.call_later(15, self._idle_timeout)
        values = self._compute_bia(weight_kg, impedance)
        values["weight"] = weight_kg
        values["stable"] = 1.0 if is_stable else 0.0
        values["impedance"] = impedance
        if battery is not None:
            values["battery"] = battery
            self._set_charging(charging)
        values["timestamp"] = dt_util.utcnow()
        for key, entity in self.entities.items():
            if key in values:
                entity.async_update_state(values[key])

    def _idle_timeout(self) -> None:
        """连续 15 秒无新数据时，把"测量中"置 False。"""
        self._idle_handle = None
        if time.monotonic() - self._last_data_at >= 15:
            self._set_measuring(False)

    def _compute_bia(self, weight_kg: float, impedance: float) -> dict[str, float]:
        """BIA 身体指标计算"""
        if self.height_cm <= 0:
            return {}
        height_m = self.height_cm / 100.0
        bmi = weight_kg / (height_m * height_m)

        if impedance > 0:
            if self.male:
                fat = 0.18 * bmi + 0.012 * self.age + 0.018 * impedance - 3.2
            else:
                fat = 0.26 * bmi + 0.011 * self.age + 0.020 * impedance - 2.5
        else:
            sex_offset = -10.8 if self.male else 0.0
            fat = 1.20 * bmi + 0.23 * self.age + sex_offset - 5.4
        fat = max(5.0, min(55.0, fat))

        water = max(35.0, min(75.0, 69.7 - fat * 0.55))
        bone = max(1.5, min(5.5, weight_kg * (0.047 if self.male else 0.040)))
        protein = max(10.0, min(24.0, 16.0 + (water - 50.0) * 0.12))
        muscle = max(0.0, weight_kg * (1.0 - fat / 100.0) - bone)

        return {
            "bmi": bmi,
            "body_fat": fat,
            "water": water,
            "muscle": muscle,
            "protein": protein,
            "bone": bone,
        }
