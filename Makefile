# 可通过命令行覆盖，例如: make deploy PHONE_SSH=root@192.168.1.100
PHONE_SSH ?= phone-root
USER_SSH  ?= phone

.PHONY: help deploy deploy-dashboard deploy-scripts status ssh forward backup

help:
	@echo "Phone Mini-Server 管理命令:"
	@echo "  make deploy           部署全部脚本与 Web 控制台并重启仪表盘"
	@echo "  make deploy-dashboard 仅部署 dashboard/ 并重启 :8888"
	@echo "  make deploy-scripts   同步 android/ 与 ubuntu/ 运维脚本"
	@echo "  make status           查看手机小主机运行状态"
	@echo "  make ssh              以日常用户 SSH 登录小主机"
	@echo "  make forward          通过 USB 转发 2222/8888/8080/7681 端口"
	@echo "  make backup           停服并拉取 ubuntu.img 镜像备份"

deploy: deploy-scripts deploy-dashboard

deploy-scripts:
	adb push android/linux-ctl.sh /data/local/tmp/linux-ctl.sh
	adb push android/service.d/99-ubuntu-server.sh /data/local/tmp/99-ubuntu-server.sh
	adb shell 'su -c "mv /data/local/tmp/linux-ctl.sh /data/local/linux/linux.sh && chmod 755 /data/local/linux/linux.sh"'
	adb shell 'su -c "mv /data/local/tmp/99-ubuntu-server.sh /data/adb/service.d/99-ubuntu-server.sh && chmod 755 /data/adb/service.d/99-ubuntu-server.sh"'
	adb shell 'su -c "mkdir -p /data/local/linux/mnt/mnt/acc-data && (mountpoint -q /data/local/linux/mnt/mnt/acc-data || mount -o bind /data/adb/vr25/acc-data /data/local/linux/mnt/mnt/acc-data)"'
	scp ubuntu/rc.local $(PHONE_SSH):/etc/rc.local
	ssh $(PHONE_SSH) "chmod 755 /etc/rc.local"

deploy-dashboard:
	ssh $(PHONE_SSH) "mkdir -p /opt/phone-dashboard/static"
	scp -r dashboard/* $(PHONE_SSH):/opt/phone-dashboard/
	ssh $(PHONE_SSH) "chmod 755 /opt/phone-dashboard/server.py"
	ssh $(PHONE_SSH) "pkill -f '[p]hone-dashboard/server.py' || true"
	ssh $(PHONE_SSH) "pkill -f '[u]sr/local/bin/phone-dashboard' || true"
	ssh $(PHONE_SSH) "/etc/rc.local && sleep 1 && ps -ef | grep '[p]hone-dashboard'"

status:
	adb shell su -c "/data/local/linux/linux.sh status"

ssh:
	ssh $(USER_SSH)

forward:
	adb forward tcp:2222 tcp:22
	adb forward tcp:8888 tcp:8888
	adb forward tcp:8080 tcp:8080
	adb forward tcp:7681 tcp:7681
	@echo "USB 端口转发完成: 2222(SSH) 8888(控制台) 8080(网盘) 7681(终端)"

backup:
	adb shell su -c "/data/local/linux/linux.sh stop"
	adb pull /data/local/linux/ubuntu.img ./ubuntu-backup-$$(date +%Y%m%d).img
	adb shell su -c "/data/local/linux/linux.sh start"
