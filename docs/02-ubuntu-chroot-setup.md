# 02 - 原生 Ubuntu 24.04 chroot 搭建全流程与踩坑实录

## 1. 为什么选择「动态 ext4 镜像 + 原生 chroot」？

| 方案 | 原理 | 优缺点 |
| :--- | :--- | :--- |
| Termux + proot | 用户态系统调用拦截模拟 | 无需 Root；但性能有损耗、无法监听 22/80/443 特权端口、受 Android App 杀后台限制 |
| 直接解压到 `/data/local/...` | 复用 Android `/data` 分区 | 简单；但 Android `/data` 默认带 **`nosuid,nodev`** 挂载参数，会导致 `sudo`、`ping` 等需要 SUID 的程序异常 |
| **稀疏 `ext4` 镜像 + 原生 `chroot`（本方案）** | 独立 `ext4` 文件系统挂载 + 共享原生 Linux 内核 | **零虚拟化损耗**、完整 `rw,suid,dev,exec` 权限、不破坏原厂 Android 系统、单文件镜像易于备份与一键卸载 |

### 安全性与共存性
- Android 原系统与数据完全保留，手机可继续正常使用。
- 镜像采用 `truncate -s 32G` 创建**稀疏文件（Sparse File）**：Ubuntu 内看到的根分区为 32 GB，但手机 `/data` 实际仅按写入量动态占用空间（初始约 700 MB）。

---

## 2. 实战踩坑实录（Android 内核专属知识点）

这次搭建过程中一共踩到并解决了 **4 个非常经典的 Android Linux 内核坑**：

### 坑 1：Android `loop` 设备的次设备号（minor）规则
- **现象**：`/system/bin/losetup -f` 返回 `/dev/block/loop34`，但该节点尚未创建导致 `No such file or directory`；手动 `mknod /dev/block/loop250 b 7 250` 又报 `Bad file descriptor`。
- **根因**：Android 内核启用了 `loop.max_part=7`，每个 loop 设备预留 8 个次设备号，因此 `loopN` 的次设备号不是 `N`，而是 **`N * 8`**（例如 `loop34` 的设备号为 `7, 272`）。
- **解法**：使用 Magisk 内置的 `/data/adb/magisk/busybox losetup -f` 触发 `/dev/loop-control` 自动创建节点，若 ueventd 尚未生成则按 `mknod -m 0600 "$LOOP" b 7 $((IDX * 8))` 创建。

### 坑 2：Android `toybox mount` 不认 `suid,dev,exec` 字符串
- **现象**：执行 `mount -t ext4 -o rw,suid,dev,exec,noatime` 返回 `Invalid argument`。
- **根因**：在 Linux `mount(2)` 系统调用里，`suid/dev/exec` 本来就是默认行为（只有 `nosuid/nodev/noexec` 才是标志位）。桌面端 `util-linux` 会把 `suid` 解析掉，而 Android 的 `toybox mount` 会把它原样透传给 `ext4` 驱动导致报错。
- **解法**：直接传 `-o rw,noatime`，内核会自动以 `suid,dev,exec` 挂载。

### 坑 3：SELinux Enforcing 拦截内核 loop 线程读取镜像文件
- **现象**：`mount` 报 `EXT4-fs (loop34): unable to read superblock`，查看 `dmesg` 发现：
  ```text
  avc: denied { read } for comm="loop34" path="/data/local/linux/ubuntu.img"
  scontext=u:r:kernel:s0 tcontext=u:object_r:system_data_file:s0 tclass=file permissive=0
  ```
- **根因**：在 SELinux `Enforcing` 模式下，内核的 `loop` 线程（`u:r:kernel:s0`）默认无权读取 `system_data_file` 类型的文件。
- **解法**：无需关闭 SELinux，也不需要改全局策略，只需给镜像文件打上 Magisk 专用 SELinux 标签：
  ```bash
  chcon u:object_r:magisk_file:s0 /data/local/linux/ubuntu.img
  ```

### 坑 4：Android 网络权限组（`aid_inet`）与 `apt` 沙盒
- **现象**：部分 Android 内核开启 `CONFIG_ANDROID_PARANOID_NETWORK` 后，非 `3003` 组用户（尤其是 `apt` 默认降权使用的 `_apt` 用户）无法创建网络套接字。
- **解法**：
  1. 在 Ubuntu 内建立 `aid_inet (3003)`、`aid_net_raw (3004)`、`aid_net_admin (3005)` 并把 `root` 和 `_apt` 加入。
  2. 写入 `/etc/apt/apt.conf.d/99android`：`APT::Sandbox::User "root";`。
  3. `/dev/shm` 需在 `mount -o bind /dev` **之后**再 `mkdir -p` 并挂载 `tmpfs`。

---

## 3. 完整复现步骤摘要

1. 下载官方 ARM64 精简包：`ubuntu-base-24.04.5-base-arm64.tar.gz`（清华源镜像）。
2. 创建并格式化 32 GB 稀疏镜像：
   ```bash
   truncate -s 32G /data/local/linux/ubuntu.img
   mkfs.ext4 -F -L ubuntu-phone -E lazy_itable_init=1,lazy_journal_init=1 /data/local/linux/ubuntu.img
   chcon u:object_r:magisk_file:s0 /data/local/linux/ubuntu.img
   ```
3. 挂载镜像及虚拟文件系统（`proc` / `sys` / `dev` / `devpts` / `dev/shm` / `/mnt/sdcard`），解压根文件系统。
4. 配置 `/etc/resolv.conf`（阿里 DNS `223.5.5.5` + DNSPod `119.29.29.29`）与 `/etc/hosts`。
5. 切换 `apt` 清华源，安装 `openssh-server` 等基础工具，写入电脑公钥，配置 `sshd` 仅允许公钥登录并禁用 PAM（`UsePAM no`）。
6. 部署 `/data/local/linux/linux.sh` 与 `/data/adb/service.d/99-ubuntu-server.sh`，通过 `/sys/power/wake_lock` 持有 `ubuntu_server` 唤醒锁实现熄屏不断网、重启自启动。
