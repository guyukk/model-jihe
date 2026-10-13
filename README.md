# 智能分拣视觉系统

MaixCAM2 上的几何体识别应用，工程版本 **3.2.0**。视觉端按[对接协议](docs/vision-link-protocol.md)每 2 秒主动发送一次整盘快照；线上协议版本字节仍为 `0x02`。当前识别 `cone`、`cube`、`pyramid`；颜色协议编号支持红、橙、黄、绿、蓝、紫、黑、白。未完成 LAB 标定的设备沿用红、黄、蓝、白 HSV 判色。`0x43 SCAN_REQ` 已废弃。

## 运行与交互

从设备桌面启动“几何体检测”，由 launcher 交接相机、显示和 NPU。`main.py` 负责采集、识别、触摸与显示，`link_service.py` 独立调度 UART4 通信。启动后即按周期识别、发送快照和心跳；MCU 的启动、抓取、复核及完赛事务按协议处理。快照最多 12 件，空盘也发送；慢识别时可重发带原快照号的缓存。串口写出成功仅表示本机完成写入，不能证明 MCU 已收到。

主页常显最近一次**完整识别时采集的原始画面**、物料框和名称，扫描中保留旧结果。点击“物料详情”查看形状、颜色、毫米坐标、可抓状态和未上报原因；“长按退出”按住 1.5 秒正常退出应用。分拣页显示序号、名称、形状图示与累计成功数，显示完成后才返回 `0xC4`。界面区分心跳接收、结果年龄和 UART 写出状态，结果超过 15 秒未更新会提示滞留。中文字体加载失败时改用完整英文文案。

## 颜色判定

物料的形状和位置由 YOLO 与 ROI 决定；颜色从检测框中心 60% 区域采样。`COLOR_MODE="auto"` 时，有效的 `/root/lab_colors.json` 启用 LAB 多样本近邻判色；每类保留至少四个实物样本，不合并为一个大阈值。距离超过样本允许范围，或前两类相距不足 8 LAB 单位，仍给出颜色但标“颜色估测”。界面显示已标定 N/8 色，并提醒缺色可能误判。标定文件缺失或损坏时记录原因并沿用原 HSV 判色；设 `COLOR_MODE="hsv"` 可强制回退。**有效采样框不会仅因颜色阈值而被丢弃**。空框或完全落在图像外的异常框仍可能得到 `unknown`，协议层继续拒绝未知颜色。

估测不保证弱光、反光、颜色接近或未标定颜色的准确率。多帧投票优先采信非估测结果。独立桌面应用“颜色标定”从原始相机帧点击采样，不启动模型或本工程 UART；见 [LAB 标定说明](docs/LAB颜色标定说明.md)。旧 HSV 标定说明仍见 [HSV 标定说明](docs/HSV标定说明.md)，ROI 见 [ROI 说明](docs/ROI使用说明.md)。保存标定或修改参数后，从桌面重启正式应用生效。

## 配置速查

配置集中在 [config.py](config.py)：

| 用途 | 主要参数 | 当前值或单位 |
| --- | --- | --- |
| 串口 | `UART_PORT`、`UART_BAUDRATE` | `/dev/ttyS4`，115200 8N1 |
| 模型 | `MODEL_PATH` | 完整包内 `.mud` 优先；否则回退设备预装路径 |
| 检测区域 | `ROI_*`、`ROI_CONFIG_PATH` | 320×256 标定坐标；设备 `/root/roi_config.json` 可覆盖 |
| 颜色 | `COLOR_MODE`、`LAB_CONFIG_PATH`、`LOWER_*`、`UPPER_*` | 默认 auto；有效 LAB 文件优先，HSV 回退 |
| 检测 | `YOLO_*`、`SCAN_*` | 5 帧、至少 3 帧命中、帧间 50 ms |
| 坐标 | `COORD_*`、`SNAPSHOT_COORD_*` | 协议坐标单位 0.1 mm；越界件过滤 |
| 链路与显示 | `HEARTBEAT_INTERVAL`、`LINK_TIMEOUT`、`SNAPSHOT_INTERVAL_MS`、`DISPLAY_DURATION` | 秒或毫秒，见各项注释 |
| 取证 | `DIAG_*`、`STARTUP_LOG_PATH`、`LINK_LOG_PATH`、`FAULT_DUMP_PATH` | 启动、主进程、通信进程与故障栈日志 |

ROI 与机械可抓范围目前并不完全一致；ROI 左侧的部分目标会因机械 X 坐标越界而不上报。不要通过改颜色阈值掩盖这一类坐标过滤。机械坐标须经实物标定后单独调整。

## 工程目录与打包

根目录是设备运行代码；`shared/` 存放被两个 App 共用的模块（目前为 `lab_color.py`，打包时才铺平进各自包，避免跨目录隐性引用），`models/` 存放权重，`calibration/lab_app/` 是独立触摸标定桌面应用，`tests/` 为本机离线回归，`tools/` 包含打包、测试及 MCU 模拟器，`docs/` 为协议和操作说明，`legacy/` 为历史实现。运行 `python tools/package_app.py` 生成 `dist/maix-test1-v3.2.0.zip`；运行 `python tools/package_lab_app.py` 生成 `dist/maix-labcal-v1.0.0.zip`。正式包的 `.mud` 与两份 `.axmodel` 同层；标定包无模型。旧的非完整安装仍可回退到 `/root/models/model-jihe/model_8988.mud`。

## 测试与故障取证

本机运行 `python tools/run_tests.py`，每个离线测试在独立进程执行；打包前运行 `python tools/package_app.py`，再跑测试以校验安装包内容。串口模拟可先用 `python tools/mcu_sim.py --list-ports` 找端口，再用 `python tools/mcu_sim.py --port COMxx --duration 1800` 运行 30 分钟。原始字节、解析结果和统计报告保存在 `.maixpy/mcu_sim/`；详见 [模拟器说明](docs/MCU模拟器使用说明.md)。

设备日志分别在 `/root/startup.log`、`/root/diag.log`、`/root/link.log` 和 `/root/fault_dump.txt`，AXERA 媒体日志在 `/opt/data/AXSyslog/syslog/`。串口模拟、空盘快照及 Python RSS 记录不能证明整机卡死问题已经解决；此前测试曾出现通信中断、识别停滞和设备重启，仍需在真实 MCU、电机及共用供电条件下复现并核对电源与媒体日志。当前仅有空盘条件，非空物料颜色、抓取显示锁步和机械坐标尚未完成实机验收。

界面状态、异常含义和现场验收边界见[正式界面说明](docs/正式界面说明.md)，协议实现细节见[MCU 对接文档](docs/MCU对接文档.md)。
