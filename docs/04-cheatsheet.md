# 04 - 日常高频指令速查手册

## 1. 登录与连接

```bash
# Wi-Fi 免密直连（已写入本机 ~/.ssh/config）
ssh phone

# 换了 Wi-Fi 后查手机新 IP（插 USB 数据线）
adb shell ip -4 addr show wlan0

# 无 Wi-Fi 或局域网隔离时，走 USB 端口转发连 SSH / Web
adb forward tcp:2222 tcp:22
ssh -p 2222 root@127.0.0.1

adb forward tcp:8888 tcp:8888   # 浏览器访问 http://127.0.0.1:8888
adb forward tcp:8080 tcp:8080   # 浏览器访问 http://127.0.0.1:8080
adb forward tcp:7681 tcp:7681   # 浏览器访问 http://127.0.0.1:7681
```

---

## 2. 小主机服务管理（Android 侧 `adb shell su` 或 Termux）

```bash
/data/local/linux/linux.sh status    # 查看挂载、服务、IP 和电量
/data/local/linux/linux.sh start     # 挂载镜像、加唤醒锁并启动全部服务
/data/local/linux/linux.sh stop      # 停止全部服务、释放唤醒锁并卸载镜像
/data/local/linux/linux.sh restart   # 重启小主机环境
/data/local/linux/linux.sh shell     # 直接进入 Ubuntu Zsh
```

---

## 3. Ubuntu 内部常用操作（`ssh phone` 后）

```bash
# 查看开机自启脚本与服务日志
cat /etc/rc.local
tail -f /var/log/phone-server/*.log

# 查看 Web 认证账号密码
cat /etc/phone-server-auth

# 查看磁盘与内存
df -h / /mnt/sdcard
free -h
htop

# 查看手机电池电量 / 状态 / 温度（单位 0.1°C）
bat
```

---

## 4. ACC 充电保护指令

```bash
adb shell su -c "acc -D"                  # 看守护进程是否在跑
adb shell su -c "acc -i"                  # 看电池健康度、循环、电压电流温度
adb shell su -c "acc -s"                  # 看当前配置
adb shell su -c "acc -s pc=80 rc=70"      # 改成 70% 复充、80% 停充
adb shell su -c "acc -f 100"              # 临时充满一次到 100%（适合要带出门）
```

---

## 5. 免 USB 线隔空无线安装 APK（SSH + `nsenter`）

### 原理
Ubuntu `chroot` 与 Android 共享同一个 Linux 内核与 PID 命名空间。通过 `nsenter -t 1 -m` 切回 Android `init`（PID 1）的挂载命名空间，即可在 Wi-Fi SSH 内直接调用 Android 宿主的 `/system/bin/pm install` 静默安装 APK，**完全无需插 USB 数据线，也无需开启无线 ADB**。

### 使用方式
```bash
# 方式 1：直接使用 Makefile（推荐）
make install-apk APK=/path/to/your.apk   # 无线静默安装任意本地 APK
make install-latest-apk                  # 拉取最新伴侣 App Release 并无线直装

# 方式 2：手工两行命令
scp your.apk phone-root:/mnt/sdcard/Download/.tmp.apk
ssh phone-root 'nsenter -t 1 -m -- /system/bin/sh -c "cp /sdcard/Download/.tmp.apk /data/local/tmp/.tmp.apk && pm install -r /data/local/tmp/.tmp.apk && rm -f /data/local/tmp/.tmp.apk /sdcard/Download/.tmp.apk"'
```

> **提示**：同样原理也可以在 SSH 里用 `nsenter -t 1 -m -- /system/bin/sh -c "..."` 直接执行任何 Android 宿主命令（如 `pm list packages`、`am start`、`acc -s` 等）。

---

## 6. 备份与彻底卸载

### 备份整个 Ubuntu 系统
```bash
# 先停服保证文件系统一致性，再拉出单个镜像文件即可
adb shell su -c "/data/local/linux/linux.sh stop"
adb pull /data/local/linux/ubuntu.img ./ubuntu-backup.img
```

### 彻底无痕卸载（不影响原安卓系统）
```bash
adb shell su -c "/data/local/linux/linux.sh stop"
adb shell su -c "rm -rf /data/local/linux /data/adb/service.d/99-ubuntu-server.sh"
```
