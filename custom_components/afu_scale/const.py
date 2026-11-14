"""AFU 体脂秤集成常量"""

DOMAIN = "afu_scale"

CONF_HEIGHT_CM = "height_cm"
CONF_SEX = "sex"
CONF_AGE = "age"

DEFAULT_HEIGHT_CM = 175
DEFAULT_SEX = "male"
DEFAULT_AGE = 29

# GATT 服务 / 特征
SERVICE_UUID = "0000ffb0-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000ffb2-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ffb1-0000-1000-8000-00805f9b34fb"

# 报文魔数 / 稳定标志
PACKET_MAGIC = 0xAC
STABLE_FLAG = 0x02

# 握手包（某些固件需要写入后才推送完整数据）
HANDSHAKE = bytes([0xFD, 0x37, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x37])

# 连接与休眠管理参数
CONNECT_TIMEOUT = 20
CONNECT_ATTEMPTS = 3
RECONNECT_DELAY = 10
DISCONNECT_DELAY = 3   # 稳定锁定后主动断开延迟（秒），留出缓冲更新数据
COOLDOWN_DELAY = 20    # 测完断开后的静默冷却期（秒），防止残留广播触发重复连接
