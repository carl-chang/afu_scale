"""AFU 体脂秤 BLE 连接协调器。

负责：
  - 通过 HA 蓝牙集成（含 ESP32 蓝牙代理）监听体脂秤开机广播
  - 广播唤醒后主动连接体脂秤，订阅 0xFFB2 通知
  - 解析 0xAC 报文（体重/稳定/阻抗），并计算 BIA 指标
  - 测量稳定锁定后主动断开连接，允许体脂秤进入休眠省电
  - 设置静默冷却期，防止秤关机残留广播触发重复重连

"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import time
from typing import TYPE_CHECKING

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
try:
    from homeassistant.components.bluetooth import BluetoothScanningMode
except ImportError:
    from habluetooth import BluetoothScanningMode
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    CONNECT_ATTEMPTS,
    CONNECT_TIMEOUT,
    COOLDOWN_DELAY,
    DISCONNECT_DELAY,
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

        # 蓝牙广播唤醒机制：休眠时不发连接请求，待人踩上秤开机发广播时唤醒
        self._discovered_event = asyncio.Event()
        self._cancel_callback: Callable[[], None] | None = None
        self._fallback_poll = False

        # 延时断开与无数据超时计时器
        self._disconnect_handle: asyncio.TimerHandle | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._last_data_at: float = 0.0

        # 测量中状态
        self.measuring = False
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
        """启动协调器：注册蓝牙广播监听，并启动连接管理协程。"""
        self._shutdown = False
        try:
            self._cancel_callback = bluetooth.async_register_callback(
                self.hass,
                self._on_bluetooth_event,
                {"address": self.address, "connectable": True},
                BluetoothScanningMode.PASSIVE,
            )
            _LOGGER.debug("AFU Scale: 已注册蓝牙广播监听 (%s)", self.address)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("AFU Scale: 注册蓝牙广播监听失败，回退至轮询模式: %s", exc)
            self._fallback_poll = True

        self._task = asyncio.create_task(self._run())

    @callback
    def _on_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """收到体脂秤蓝牙广播回调（人踩上秤开机时触发）。"""
        if not self._shutdown and (self._client is None or not self._client.is_connected):
            _LOGGER.debug("AFU Scale: 捕获到体脂秤广播，唤醒连接任务")
            self._discovered_event.set()

    def _schedule_disconnect(self, delay: float = DISCONNECT_DELAY, reason: str = "测量锁定完成") -> None:
        """在指定延迟后主动断开连接，允许体脂秤进入休眠。"""
        if self._disconnect_handle is not None:
            return
        _LOGGER.debug("AFU Scale: 将在 %.1f 秒后断开连接 (%s)", delay, reason)
        self._disconnect_handle = self.hass.loop.call_later(
            delay,
            lambda: self.hass.async_create_task(self._disconnect_client(reason)),
        )

    async def _disconnect_client(self, reason: str = "") -> None:
        """断开 BLE 客户端连接。"""
        self._disconnect_handle = None
        if self._client and self._client.is_connected:
            _LOGGER.info("AFU Scale: %s，主动断开连接以允许体脂秤休眠", reason)
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("AFU Scale: 断开连接异常: %s", exc)

    async def stop(self) -> None:
        """停止协调器并释放所有资源。"""
        self._shutdown = True
        if self._cancel_callback:
            self._cancel_callback()
            self._cancel_callback = None
        self._discovered_event.set()
        if self._idle_handle:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self._disconnect_handle:
            self._disconnect_handle.cancel()
            self._disconnect_handle = None
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
        """主循环：广播唤醒 -> 连接与测量 -> 测完断开休眠 -> 冷却防重连。"""
        while not self._shutdown:
            try:
                # 若支持广播监听，则等待体脂秤开机广播唤醒；避免休眠期持续每 30 秒轮询
                if not self._fallback_poll:
                    await self._discovered_event.wait()
                    self._discovered_event.clear()
                    if self._shutdown:
                        break

                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("AFU Scale 连接异常: %s", exc)

            if self._shutdown:
                break

            # 测量完成并断开后，增加静默冷却时间，防止体脂秤关机延迟期间残留广播触发重复连接
            await asyncio.sleep(COOLDOWN_DELAY)
            self._discovered_event.clear()

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
            # 等待测量完成并断开连接；测完锁定后会调用 disconnect 退出循环
            while client.is_connected and not self._shutdown:
                await asyncio.sleep(1)
        finally:
            if self._disconnect_handle is not None:
                self._disconnect_handle.cancel()
                self._disconnect_handle = None
            if self._idle_handle is not None:
                self._idle_handle.cancel()
                self._idle_handle = None
            self._set_measuring(False)
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

        # 核心功耗优化：数值稳定锁定后，安排延时 3 秒主动断开连接，允许体脂秤迅速进入深休眠
        if is_stable:
            self._schedule_disconnect(delay=DISCONNECT_DELAY, reason="测量稳定锁定")

    def _idle_timeout(self) -> None:
        """连续 15 秒无新数据时，把"测量中"置 False 并主动断开连接。"""
        self._idle_handle = None
        if time.monotonic() - self._last_data_at >= 15:
            self._set_measuring(False)
            self.hass.async_create_task(self._disconnect_client("测量超时无数据"))

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
