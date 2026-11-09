# 星河实验板 XH-01 使用手册（虚构演示资料）

本文件只用于软件演示，参数不代表真实器件规格；实际硬件以制造商资料为准。

## 供电与接口
XH-01 实验板使用 USB 5V 输入，板载稳压输出 3.3V。禁止将 5V 电源直接接到 3.3V 引脚。
串口连接 TX→RX、RX→TX，并共地。默认串口波特率 115200，8 数据位，无校验，1 停止位。

## 串口协议与 LED
演示固件接收以换行符结尾的 ASCII 指令：STATUS 查询 LED 状态；LED_ON 点亮 LED；
LED_OFF 熄灭 LED。返回 LED=ON 或 LED=OFF。串口控制前请先检查 device_protocol.md。
模拟设备仅改变内存状态，不会操作真实电路。真实串口模式需配置端口并连接兼容固件。

## 器件与库存
演示库存表 components 包含 model、stock、location 三列。STM32F103C8T6 与 ESP32-WROOM-32
的库存必须通过 query_database 实时查询，手册不提供库存数量。库存数据为虚构样例。

## 排错
没有串口响应时，先检查端口、共地、接线与波特率；然后查询 STATUS。
扫描版 PDF 没有文本层时需要先进行 OCR。旧版 Word .doc 请转换为 .docx 再导入。
