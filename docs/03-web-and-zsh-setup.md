# 03 - 国内源、Oh My Zsh 与轻量 Web 套件部署

## 1. 国内镜像源配置

### APT（Ubuntu 24.04 ARM64 `ubuntu-ports`）
Ubuntu 24.04 使用 DEB822 格式的 `/etc/apt/sources.list.d/ubuntu.sources`，已替换为清华大学镜像源：
- 初始无 `ca-certificates` 时先用 `http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports`
- 装完 `ca-certificates` 后升级为 `https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports`

### PyPI (`pip`)
已配置全局 `/etc/pip.conf`：
```ini
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
```

---

## 2. Zsh + Oh My Zsh（与主力机同款）

- 默认 Shell 已切换为 `/usr/bin/zsh`（`chsh -s /usr/bin/zsh root`）。
- 主题：`ZSH_THEME="ys"`
- 插件：`git`、`zsh-autosuggestions`、`zsh-syntax-highlighting`、`z`、`extract`、`web-search`、`copyfile`、`copypath`
- 语言环境：已生成 `en_US.UTF-8` 与 `zh_CN.UTF-8`，默认 `LANG=C.UTF-8`，时区 `Asia/Shanghai`。
- 快捷别名：`ll`、`la`、`l`、`bat`（读取电池电量/状态/温度）。

---

## 3. 轻量 Web 管理套件（总内存占用 < 50 MB）

所有 Web 服务均由 Ubuntu 内的 `/etc/rc.local` 随开机自动拉起，日志存放在 `/var/log/phone-server/`。

| 服务 | 端口 | 程序路径 | 功能说明 |
| :--- | :--- | :--- | :--- |
| **监控仪表盘** | `8888` | `/usr/local/bin/phone-dashboard` | 零依赖 Python 3 实时看板：CPU、内存、`/` 与 `/mnt/sdcard` 磁盘、手机电池电量/温度/电流/健康度、服务状态、Top 进程，顶部一键跳转网盘与终端 |
| **FileBrowser** | `8080` | `/usr/local/bin/filebrowser` | Go 单二进制网页轻网盘，默认中文界面，根目录 `/`（含 `/mnt/sdcard` 手机存储），支持上传/下载/在线预览与编辑 |
| **ttyd 网页终端** | `7681` | `/usr/local/bin/ttyd` | 浏览器直接运行完整 Zsh 彩色终端（需 HTTP Basic 认证） |

### 修改 Web 登录密码

- **修改 FileBrowser 密码**：登录 `http://<手机IP>:8080` 后，在左侧 **设置 → 个人资料** 里直接修改，或命令行：
  ```bash
  filebrowser users update admin -p "你的新密码" -d /etc/filebrowser/filebrowser.db
  ```
- **修改 ttyd 密码**：编辑 `/etc/phone-server-auth`（格式 `admin:你的新密码`），然后重启 ttyd：
  ```bash
  pkill -f /usr/local/bin/ttyd && /etc/rc.local
  ```

---

## 4. 手机端 Termux 快捷命令

已写入 `/data/data/com.termux/files/home/.bashrc`，在手机上打开 Termux 即可直接使用：

| 命令 | 作用 |
| :--- | :--- |
| `u` | 一键进入 Ubuntu Zsh 终端 |
| `u-status` | 查看挂载状态、4 个核心服务状态、Wi-Fi IP 与电量 |
| `u-start` / `u-stop` / `u-restart` | 启动 / 停止 / 重启 Linux 小主机 |
| `bat` | 查看 ACC 电池详细遥测信息 |
