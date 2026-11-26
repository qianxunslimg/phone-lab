#!/system/bin/sh
# Magisk service.d boot hook: waits for Android boot completion, then starts Ubuntu server.
# Deployed to: /data/adb/service.d/99-ubuntu-server.sh

while [ "$(getprop sys.boot_completed)" != "1" ]; do
  sleep 2
done

# Give /sdcard and wlan0 a moment to initialize
sleep 5
/data/local/linux/linux.sh start >/data/local/linux/boot.log 2>&1
