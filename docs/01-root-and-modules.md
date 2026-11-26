# 01 - Root 生态、Magisk 模块与 ACC 充电保护

## 1. 电脑侧 ADB 环境配置（Ubuntu）

首次连接 Android 设备时，系统需要安装 `adb` 以及对应的 USB udev 权限规则包：

```bash
sudo apt-get update
sudo apt-get install -y adb android-sdk-platform-tools-common
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=18d1
```

### 核心知识点
- **为什么需要 `android-sdk-platform-tools-common`**：Linux 默认把插入的 USB 设备节点 `/dev/bus/usb/xxx/yyy` 设为 `root:root` 权限；安装该包会写入 Android 厂商的 udev 规则，允许 `plugdev` 组用户免 `sudo` 访问 ADB 设备。
- **ADB 授权状态**：`adb devices -l` 显示 `unauthorized` 时，需在手机屏幕上点击“允许 USB 调试”；授权后变为 `device`。
- **检验 Root 状态**：
  ```bash
  adb shell su -c "id; magisk -v"
  # 正常输出: uid=0(root) ... context=u:r:magisk:s0 / 31.1:MAGISK:R
  ```

---

## 2. Magisk / Zygisk / LSPosed(Vector) 分层概念

| 层级 | 代表工具 | 作用 |
| :--- | :--- | :--- |
| **系统底层插件** | Magisk 模块 | 通过 Systemless（无修改原厂分区）在开机时动态挂载系统文件、运行后台脚本（如防变砖、ACC、Systemless hosts） |
| **应用层 Hook 框架** | Vector（原 JingMatrix/LSPosed） | 依托 Magisk 的 **Zygisk** 在内存中 Hook 目标 App / 系统组件，无需反编译修改 APK |
| **具体功能模块** | Cemiuiler（MIUI 14 专属） | 基于 Xposed API 实现系统 UI 定制、去系统广告等（注：HyperCeiler 仅支持 HyperOS，MIUI 14 需用 Cemiuiler） |
| **动态逆向调试** | Frida | 面向开发/安全研究，实时插桩查看函数参数、返回值与抓包分析 |

### 命令行管理 Magisk 的实用技巧

```bash
# 查看 Magisk 数据库设置（通过 stdin 避免双层引号转义问题）
adb shell su <<'EOF'
magisk --sqlite "SELECT * FROM settings;"
EOF

# 命令行开启 Zygisk（重启后生效）
adb shell su <<'EOF'
magisk --sqlite "REPLACE INTO settings (key, value) VALUES ('zygisk', 1);"
EOF

# 命令行直接刷入 Magisk 模块 zip
adb shell su -c "magisk --install-module /data/local/tmp/module.zip"
```

### 已安装的模块与工具
- **YetAnotherBootloopProtector (`v8.138`)**：防变砖救砖模块，发生模块导致的开机卡死时自动禁用问题模块。
- **Vector (`v2.2`)**：新版 LSPosed 框架（由 JingMatrix 维护）。
- **Systemless hosts (`v1.0`)**：配合 AdAway 在不修改只读 `/system` 分区的前提下改写系统 Hosts。
- **Cemiuiler (`v1.3.130`) / 雹 Hail (`v1.11.0`) / AdAway (`v6.1.4`)**：玩机与系统管理工具。

---

## 3. ACC 充电保护（24 小时插电防鼓包）

把手机当小主机长期插电时，锂电池长期维持在 100% 高压状态极易发热鼓包。**ACC (Advanced Charging Controller)** 通过内核充电控制节点（如本机的 `/sys/class/power_supply/battery/input_suspend`）在达到阈值时自动停充或切换**旁路直通供电（Battery Idle Mode）**。

### 安装与验证

```bash
# 从 GitHub 官方 Release (VR-25/acc) 下载 zip 后刷入
adb shell su -c "magisk --install-module /data/local/tmp/acc.zip"

# 查看 ACC 守护进程状态与电池实时遥测（未重启前用 /dev/acc，重启后可直接用 acc）
adb shell su -c "/dev/acc -D"
adb shell su -c "/dev/acc -i"
```

### 常用调参命令

```bash
# 查看当前配置
adb shell su -c "acc -s"

# 修改电量保护区间（pc=停充百分比, rc=复充百分比）
# 示例：70% 复充，80% 停充
adb shell su -c "acc -s pc=80 rc=70"

# 示例：纯 24h 插电极限护电池（50% 复充，60% 停充）
adb shell su -c "acc -s pc=60 rc=50"

# 启用优先旁路直通供电（默认已开启）
adb shell su -c "acc -s prioritize_batt_idle_mode=true"

# 温控保护（默认 45°C 暂停，50°C 强制保护）
adb shell su -c "acc -s max_temp_pause=45 max_temp=50"
```

> **提示**：电池在 100% 满电 `Idle` 状态时执行 `acc -t`（测试充电开关）会等待进入充电态而阻塞；待电量自然消耗到阈值以下时，ACC 会自动探测并应用最优充电开关。
