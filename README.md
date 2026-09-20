# AFU 体脂秤集成 (AFU Body Scale)

将 **沃莱科技 / 蚂蚁阿福体脂秤（AFU-WL-TZ-A1）** 接入 Home Assistant 的自定义集成。

通过你已有的 ESP32 蓝牙代理（或 HA 自带蓝牙）**主动连接**体脂秤，订阅 GATT 通知并解析 `0xAC` 报文，实时上报体重及全套 BIA 身体指标。

## 特性

- 经蓝牙代理主动连接体脂秤，订阅 `0xFFB2` 通知
- 解析 `0xAC` 体重报文（体重 / 稳定标志 / 阻抗）
- 本地 BIA 计算：BMI、体脂率、水分率、肌肉量、蛋白质率、骨量
- "测量中"二进制传感器：收到数据即开，15 秒无数据自动关（适合做动画/通知触发）
- 自动重连（断开后每 30 秒重试）
- 中文实体命名，`afu` 前缀避免与其他体脂秤冲突
- 支持 UI 配置（config flow），无需手改 yaml

## 前置条件

- Home Assistant（2023.10+，需要 Bluetooth 集成）
- **可连接的蓝牙控制器**之一：
  - ESPHome **蓝牙代理**（`bluetooth_proxy: active: true`），或
  - HA 自带/USB 蓝牙适配器
- 体脂秤（广播名为 `AFU-WL-TZ-A1`）

> 注意：本秤**不广播测量数据**，必须主动建立 GATT 连接才能读到数据。因此 `connectable=True` 的控制器是必需的，仅靠广播监听的被动方案（如 passive BLE）无法工作。

## 安装

### 方式一：手动复制

1. 将 `custom_components/afu_scale/` 整个文件夹复制到 HA 的 `config/custom_components/afu_scale/` 下
2. **完全重启 HA**（不是"重新加载"）

### 方式二：HACS 自定义仓库（如已发布）

HACS → 自定义存储库 → 填入仓库地址 https://github.com/carl-chang/afu_scale → 类型选 *Integration* → 下载 → 重启 HA

## 配置

设置 → 设备与服务 → 添加集成 → 搜索 **AFU Body Scale** → 填入：

| 字段 | 说明 |
|---|---|
| MAC 地址 | 体脂秤的 BLE 地址（如 `D0:5C:00:29:DC:6C`） |
| 身高 (cm) | 用于 BIA 计算 |
| 性别 | male=男 / female=女 |
| 年龄 | 用于 BIA 计算 |

> 找不到 MAC？用手机 App（如 nRF Connect）或 `bleak` 扫描广播名 `AFU-WL-TZ-A1` 即可。

## 实体

| 实体 | 单位 | 说明 |
|---|---|---|
| `sensor.afu_ti_zhi_cheng_ti_zhong` | kg | 体重（实时，测量中持续更新） |
| `sensor.afu_ti_zhi_cheng_dian_zu_kang` | Ω | 电阻抗 |
| `sensor.afu_ti_zhi_cheng_cheng_zhong_wen_ding` | - | 读数锁定（1=已稳定） |
| `sensor.afu_ti_zhi_cheng_bmi` | - | BMI |
| `sensor.afu_ti_zhi_cheng_ti_zhi_lv` | % | 体脂率 |
| `sensor.afu_ti_zhi_cheng_shui_fen_lv` | % | 水分率 |
| `sensor.afu_ti_zhi_cheng_ji_rou_liang` | kg | 肌肉量 |
| `sensor.afu_ti_zhi_cheng_dan_bai_zhi_lv` | % | 蛋白质率 |
| `sensor.afu_ti_zhi_cheng_gu_liang` | kg | 骨量 |
| `sensor.afu_ti_zhi_cheng_zui_jin_ce_liang_shi_jian` | - | 最近测量时间 |
| `binary_sensor.afu_ti_zhi_cheng_ce_liang_zhong` | - | 测量中（收到数据开，15s 无数据关） |

> 实体 ID 由名称拼音自动生成，实际 ID 以 HA 中为准（开发者工具 → 状态 搜索 `afu`）。

## 常见用法

### 自动化：测量完成通知

```yaml
trigger:
  - platform: state
    entity_id: binary_sensor.afu_ti_zhi_cheng_ce_liang_zhong
    from: "on"
    to: "off"
condition:
  - condition: state
    entity_id: sensor.afu_ti_zhi_cheng_ti_zhong
    state: "!unknown"
action:
  - service: notify.mobile_app_phone
    data:
      title: 测量完成
      message: >-
        体重 {{ states('sensor.afu_ti_zhi_cheng_ti_zhong') }} kg ·
        体脂率 {{ states('sensor.afu_ti_zhi_cheng_ti_zhi_lv') }} %
```

### 仪表盘动画（需 card-mod）

站上秤测量时，体重卡片脉冲发光：

```yaml
type: sensor
entity: sensor.afu_ti_zhi_cheng_ti_zhong
card_mod:
  style: |
    {% if is_state('binary_sensor.afu_ti_zhi_cheng_ce_liang_zhong','on') %}
    @keyframes afu-glow {
      0%   { box-shadow: 0 0 0 0 rgba(255,140,0,.7); }
      70%  { box-shadow: 0 0 0 20px rgba(255,140,0,0); }
      100% { box-shadow: 0 0 0 0 rgba(255,140,0,0); }
    }
    ha-card { animation: afu-glow 1.2s ease-out infinite; }
    {% endif %}
```

## 完整前端配置代码
```yaml
cards:
  - type: custom:mushroom-title-card
    title: AFU 体脂秤
    subtitle: AFU-WL-TZ-A1 · 沃莱科技
    icon: mdi:scale-bathroom
    icon_color: blue
  - type: horizontal-stack
    cards:
      - type: custom:mushroom-chips-card
        chips:
          - type: entity
            entity: binary_sensor.afu_ti_zhi_cheng_ce_liang_zhong
            name: 测量中
            icon: mdi:scale-bathroom
            icon_color: blue
            state_color: true
          - type: entity
            entity: sensor.afu_ti_zhi_cheng_zui_jin_ce_liang_shi_jian
            name: 最近测量
            icon: mdi:clock-outline
  - type: grid
    columns: 1
    square: false
    cards:
      - type: sensor
        entity: sensor.afu_ti_zhi_cheng_ti_zhong
        name: 体重
        unit_of_measurement: kg
        icon: mdi:weight-kilogram
        card_mod:
          style: >
            ha-card {
              border-radius: 20px;
              background: linear-gradient(135deg, #2a5298, #1e3c72);
              color: white;
              transition: border 0.3s ease, box-shadow 0.3s ease;
            }

            .name { font-size: 15px; color: rgba(255,255,255,.8); }

            .state { font-size: 44px; font-weight: 700; color: #fff; }

            .unit { font-size: 18px; color: rgba(255,255,255,.75); }

            {% if is_state('binary_sensor.afu_ti_zhi_cheng_ce_liang_zhong','on')
            %}

            @keyframes afu-glow {
              0%   { box-shadow: 0 0 0 0 rgba(255,140,0,.7); border-color: rgba(255,160,0,.9); }
              70%  { box-shadow: 0 0 0 20px rgba(255,140,0,0); border-color: rgba(255,160,0,.9); }
              100% { box-shadow: 0 0 0 0 rgba(255,140,0,0); border-color: rgba(255,160,0,.9); }
            }

            ha-card {
              border: 2px solid rgba(255,160,0,.9);
              background: linear-gradient(135deg, #ff7a00, #cc5200);
              animation: afu-glow 1.2s ease-out infinite;
            }

            {% endif %}
  - type: grid
    columns: 2
    square: false
    cards:
      - type: custom:mushroom-template-card
        primary: "{{ states('sensor.afu_ti_zhi_cheng_bmi') | float | round(2) }}"
        secondary: BMI
        icon: mdi:human-male-height
        icon_color: blue
      - type: custom:mushroom-template-card
        primary: "{{ states('sensor.afu_ti_zhi_cheng_ti_zhi_lu') | float | round(2) }} %"
        secondary: 体脂率
        icon: mdi:percent
        icon_color: red
      - type: custom:mushroom-template-card
        primary: >-
          {{ states('sensor.afu_ti_zhi_cheng_shui_fen_lu') | float | round(2) }}
          %
        secondary: 水分率
        icon: mdi:water-percent
        icon_color: cyan
      - type: custom:mushroom-template-card
        primary: >-
          {{ states('sensor.afu_ti_zhi_cheng_ji_rou_liang') | float | round(2)
          }} kg
        secondary: 肌肉量
        icon: mdi:arm-flex-outline
        icon_color: green
      - type: custom:mushroom-template-card
        primary: >-
          {{ states('sensor.afu_ti_zhi_cheng_dan_bai_zhi_lu') | float | round(2)
          }} %
        secondary: 蛋白质率
        icon: mdi:egg-outline
        icon_color: orange
      - type: custom:mushroom-template-card
        primary: "{{ states('sensor.afu_ti_zhi_cheng_gu_liang') | float | round(2) }} kg"
        secondary: 骨量
        icon: mdi:bone
        icon_color: brown
      - type: custom:mushroom-template-card
        primary: "{{ states('sensor.afu_ti_zhi_cheng_dian_zu_kang') }} Ω"
        secondary: 电阻抗
        icon: mdi:omega
        icon_color: purple
      - type: custom:mushroom-template-card
        primary: "{{ states('sensor.afu_ti_zhi_cheng_ti_zhong') }} kg"
        secondary: 实时体重
        icon: mdi:scale-bathroom
        icon_color: teal
  - type: history-graph
    title: 体重趋势（7 天）
    hours_to_show: 168
    entities:
      - entity: sensor.afu_ti_zhi_cheng_ti_zhong
        name: 体重
```

## 动态展示

![afu](http://images.jybtech.cn/images/202609200957150.gif)

## 工作原理

本集成使用 HA 的 `bluetooth` 集成（`bluetooth.async_ble_device_from_address`）获取可连接设备，通过 `bleak` + `bleak-retry-connector` 建立 GATT 连接，订阅服务 `0xFFB0` 下的特征 `0xFFB2`（notify），解析以 `0xAC` 开头的体重报文。

### 报文格式（`0xAC`）

| 偏移 | 含义 |
|---|---|
| 0 | 魔数 `0xAC` |
| 3-5 | 体重（`(b3-0x68)*65536 + b4*256 + b5`，单位 0.001kg） |
| 6 | `0x02` 表示数值稳定 |
| 8-9 | 阻抗（Big Endian，Ω） |

## 目录结构

```
custom_components/afu_scale/
├── __init__.py         # 集成入口
├── config_flow.py      # UI 配置
├── const.py            # 常量
├── coordinator.py      # BLE 连接 / 报文解析 / BIA 计算
├── binary_sensor.py    # "测量中"传感器
├── sensor.py           # 数据传感器实体
├── manifest.json
└── strings.json / translations/
```

## 调试

- 在 `coordinator.py` 中把 `_LOGGER.debug` 改为 `_LOGGER.info` 可查看每次收到的体重/阻抗
- 连接问题先确认：秤与代理距离、手机 App 是否断开、代理 `active: true`
- 日志关键字：`AFU Scale`

## 开源协议

MIT License
