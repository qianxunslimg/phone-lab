#!/system/bin/sh
# Android-side lifecycle controller for the native Ubuntu 24.04 ARM64 chroot.
# Deployed to: /data/local/linux/linux.sh

set -u

LINUX_DIR="/data/local/linux"
IMG="$LINUX_DIR/ubuntu.img"
MNT="$LINUX_DIR/mnt"
BUSYBOX="/data/adb/magisk/busybox"
CHROOT_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ACC_DATA_DIR="/data/adb/vr25/acc-data"

log() {
  printf '[linux-ctl] %s\n' "$*"
}

in_chroot() {
  env -i HOME=/root TERM="${TERM:-xterm-256color}" LANG=C.UTF-8 PATH="$CHROOT_PATH" \
    chroot "$MNT" "$@"
}

ensure_loop_device() {
  loop=$("$BUSYBOX" losetup -f)
  if [ ! -b "$loop" ]; then
    sleep 1
    if [ ! -b "$loop" ]; then
      # Android loop driver uses max_part=7 -> minor = index * 8
      idx="${loop#/dev/block/loop}"
      mknod -m 0600 "$loop" b 7 $((idx * 8))
    fi
  fi
  printf '%s' "$loop"
}

mount_bind_if_exists() {
  src="$1"
  dst="$2"
  if [ -d "$src" ]; then
    mkdir -p "$dst"
    mountpoint -q "$dst" || mount -o bind "$src" "$dst"
  fi
}

mount_fs() {
  mkdir -p "$MNT"
  if ! mountpoint -q "$MNT"; then
    # Allow kernel loop thread to read the image under SELinux Enforcing
    chcon u:object_r:magisk_file:s0 "$IMG" 2>/dev/null || true
    loop=$(ensure_loop_device)
    "$BUSYBOX" losetup "$loop" "$IMG"
    mount -t ext4 -o rw,noatime "$loop" "$MNT"
  fi

  mkdir -p "$MNT/proc" "$MNT/sys" "$MNT/dev"
  mountpoint -q "$MNT/proc" || mount -t proc proc "$MNT/proc"
  mountpoint -q "$MNT/sys" || mount -t sysfs sysfs "$MNT/sys"
  mountpoint -q "$MNT/dev" || mount -o bind /dev "$MNT/dev"

  mkdir -p "$MNT/dev/pts" "$MNT/dev/shm" "$MNT/run/sshd"
  mountpoint -q "$MNT/dev/pts" || mount -t devpts devpts "$MNT/dev/pts" -o gid=5,mode=620,ptmxmode=0666
  mountpoint -q "$MNT/dev/shm" || mount -t tmpfs tmpfs "$MNT/dev/shm" -o mode=1777

  mount_bind_if_exists "/sdcard" "$MNT/mnt/sdcard"
  mount_bind_if_exists "$ACC_DATA_DIR" "$MNT/mnt/acc-data"
}

start_server() {
  mount_fs
  echo "ubuntu_server" > /sys/power/wake_lock 2>/dev/null || true

  if ! pgrep -f "/usr/sbin/sshd" >/dev/null 2>&1; then
    in_chroot /usr/sbin/sshd
  fi
  if [ -x "$MNT/etc/rc.local" ]; then
    in_chroot /etc/rc.local >"$LINUX_DIR/rc.local.log" 2>&1 &
  fi
  log "Ubuntu server started."
}

stop_server() {
  for pat in \
    "/usr/local/bin/ttyd" \
    "/usr/local/bin/filebrowser" \
    "/opt/phone-dashboard/server.py" \
    "/usr/sbin/sshd"
  do
    pkill -f "$pat" 2>/dev/null || true
  done

  echo "ubuntu_server" > /sys/power/wake_unlock 2>/dev/null || true

  for target in \
    "$MNT/mnt/acc-data" \
    "$MNT/mnt/sdcard" \
    "$MNT/dev/shm" \
    "$MNT/dev/pts" \
    "$MNT/dev" \
    "$MNT/proc" \
    "$MNT/sys" \
    "$MNT"
  do
    umount -l "$target" 2>/dev/null || true
  done
  log "Ubuntu server stopped."
}

enter_shell() {
  mount_fs
  shell_bin="/bin/bash"
  [ -x "$MNT/usr/bin/zsh" ] && shell_bin="/usr/bin/zsh"
  exec env -i HOME=/root TERM="${TERM:-xterm-256color}" LANG=C.UTF-8 PATH="$CHROOT_PATH" \
    chroot "$MNT" "$shell_bin" -l
}

show_status() {
  mountpoint -q "$MNT" && log "Mount: OK ($MNT)" || log "Mount: NOT MOUNTED"
  for svc in \
    "/usr/sbin/sshd" \
    "/opt/phone-dashboard/server.py" \
    "/usr/local/bin/filebrowser" \
    "/usr/local/bin/ttyd"
  do
    if pgrep -f "$svc" >/dev/null 2>&1; then
      log "Service OK:   $svc"
    else
      log "Service DOWN: $svc"
    fi
  done
  ip_line=$(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}')
  bat_cap=$(cat /sys/class/power_supply/battery/capacity 2>/dev/null || echo "?")
  bat_stat=$(cat /sys/class/power_supply/battery/status 2>/dev/null || echo "?")
  log "Wi-Fi IP:     ${ip_line:-N/A}"
  log "Battery:      ${bat_cap}% (${bat_stat})"
}

case "${1:-}" in
  start)   start_server ;;
  stop)    stop_server ;;
  restart) stop_server; sleep 1; start_server ;;
  shell)   enter_shell ;;
  status)  show_status ;;
  *)
    printf 'Usage: %s {start|stop|restart|status|shell}\n' "$0" >&2
    exit 1
    ;;
esac
