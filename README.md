# StickBuddy — M5StickS3 一键语音输入

> **English**: StickBuddy turns the [M5Stack](https://m5stack.com/) M5StickS3 into a push-to-talk voice input key for Windows. Hold the front button and speak — on release, your speech is transcribed (Zhipu `glm-asr-2512` ASR) and auto-typed into whatever input box your cursor is in: WeChat, browsers, Word, anything. The device screen shows live status: 🔴 recording → 🟠 transcribing → 🟢 done. Front-button click = Enter; side-button click = focus WorkBuddy; side-button long-press = Esc. Built on a custom ESP32-S3 firmware (NimBLE, ST7789 status UI) + a Windows BLE host app. Chinese instructions below.

把 M5StickS3 变成一个**实体语音输入键**：按住正面键说话，松开后自动转写并打进光标所在的任意输入框（微信、浏览器、Word、WorkBuddy……全都能用）。

设备屏幕用**中文文字**实时显示任务状态：

| 状态 | 屏幕显示 | 含义 |
|---|---|---|
| 录音中 | 录音中（红色） | 按住正面键说话 |
| 转写中 | 转写中（橙色） | 智谱 ASR 转写中 |
| 完成 | 已完成（绿色，1.5 秒） | 文字已打进输入框 |
| 出错 | 出错（红色） | 转写失败 / 未配置 Key |
| WorkBuddy 任务 | 处理中（蓝色） | 回车提交给了 WorkBuddy，任务执行中 |
| 任务完成 | 任务完成（绿色，2.5 秒） | 收到的 WorkBuddy 完成信号 |

> **让屏幕显示「任务完成」**：任务结束时执行一句
> `echo done > D://Tools//StickBuddy//wb_status.txt`
> PC 端只在确实有进行中任务时才响应，空闲时自动忽略。

## 按键功能

| 按键 | 操作 | 功能 |
|---|---|---|
| 正面键 | 按住说话 | 松开 → 转写并注入文字 |
| 正面键 | 单击（<700ms） | 相当于回车 Enter |
| 侧键 | 单击 | 唤起/聚焦 WorkBuddy 窗口 |
| 侧键 | 长按（≥600ms） | 相当于 Esc |

## 你需要

- [M5StickS3](https://docs.m5stack.com/zh_CN/core/StickS3) 一台
- Windows 10/11 电脑（带蓝牙）
- [智谱开放平台](https://open.bigmodel.cn/) API Key（ASR 用 `glm-asr-2512`，有免费额度）

## 安装（三步）

### 1. 烧录固件到 M5StickS3

**方式 A — M5Burner（推荐）**：打开 [M5Burner](https://burner.m5stack.com/) → 选择 StickS3 → 「User / 自定义固件」→ 选择本仓库 Releases 里的 `stickbuddy-merged.bin` → Burn（地址 0x0）。

**方式 B — esptool**：

```bash
pip install esptool
esptool.py --chip esp32s3 --port COM5 --baud 921600 write_flash 0x0 stickbuddy-merged.bin
```

### 2. 安装 PC 端程序

下载 Releases 里的 `StickBuddy.exe`（单文件免安装），放到任意目录（如 `D:\Tools\StickBuddy\`）双击运行即可。想开机自启，把它加进启动项即可。

### 3. 配置 API Key

首次运行后，程序所在目录会生成 `config.json`，把你的智谱 Key 填进去再重启程序：

```json
{
  "zhipu_key": "你的智谱API Key"
}
```

## 注意事项

- 语音经本机蓝牙直连电脑，**不出局域网去别处**，仅发往智谱 ASR 接口转写
- 单段录音上限 28 秒（智谱 ASR 限 30s），超长内容请分多段说
- 转写结果通过**剪贴板粘贴**注入，注入瞬间会覆盖剪贴板内容
- 如果杀毒软件误报/拦截 PyInstaller 单文件（偶发静默退出），把目录加入信任区即可
- 首次连接需蓝牙配对：程序会自动搜索名为 `StickBuddy` 的设备并连接，无需手动配对

## BLE 协议（自制固件可参考）

- 服务 `6b1e0001-...`，TX `6b1e0002`（notify 按键事件），RX `6b1e0003`（PC 下发状态）
- 按键事件：`F:1`/`F:0`（正面键按下/松开），`S:1`/`S:0`（侧键）
- 状态指令：`ST:REC` / `ST:THINK` / `ST:DONE` / `ST:ERR` / `ST:IDLE` / `ST:WBBUSY` / `ST:WBDONE`

## 从源码构建

```bash
# 固件（PlatformIO）
cd firmware && pio run

# PC 程序（PyInstaller）
cd pc && pyinstaller --onefile --windowed --name StickBuddy --icon voicekey.ico ptt_receiver.py
```

## License

MIT
