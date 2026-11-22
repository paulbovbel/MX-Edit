#!/bin/sh
# MX5 Bridge @VERSION@ (unofficial): USB-MIDI without USB audio
#
#   mx5bridge-usb.sh evil-begin [audio|storage]  Evil takes control of USB
#   mx5bridge-usb.sh evil-end                    Evil releases USB
#   mx5bridge-usb.sh daemon                      Background service (launched by runevil)
#
# Evil (USB audio/USB Transfer) always takes priority. Create the MIDI-only
# connection only while USB is free and remove it before every use by Evil.
# All access shares one lock.

CFG=/sys/kernel/config/usb_gadget
G=$CFG/mx5b
UDC_NAME=ff580000.usb
LOCK=/tmp/mx5bridge.lock
FLAG=/tmp/mx5bridge.evil-usb
MNT=/media/az01-internal/Evil/usb_mnt
DIR=$MNT/MX5Bridge
OFF_SWITCH=$DIR/usb_midi_aus.txt

log() { logger -t mx5bridge -- "$*"; }

gadget_down() {
	[ -e "$G" ] || return 0
	echo "" > "$G/UDC" 2>/dev/null
	rm -f "$G/configs/c.1/midi.mx5bridge"
	rmdir "$G/configs/c.1/strings/0x409" 2>/dev/null
	rmdir "$G/configs/c.1" 2>/dev/null
	rmdir "$G/functions/midi.mx5bridge" 2>/dev/null
	rmdir "$G/strings/0x409" 2>/dev/null
	rmdir "$G" 2>/dev/null
	log "MIDI-only connection removed"
}

gadget_up() {
	mkdir "$G" 2>/dev/null || return 1
	(
		cd "$G" || exit 1
		echo 0x1209 > idVendor || exit 1
		echo 0x0001 > idProduct || exit 1
		mkdir -p strings/0x409 || exit 1
		cat /tmp/evil-serial-number > strings/0x409/serialnumber 2>/dev/null
		echo "HeadRush" > strings/0x409/manufacturer || exit 1
		echo "HeadRush @DEVICE@ Bridge" > strings/0x409/product || exit 1
		mkdir -p configs/c.1/strings/0x409 || exit 1
		echo "MIDI" > configs/c.1/strings/0x409/configuration || exit 1
		echo 100 > configs/c.1/MaxPower || exit 1
		mkdir functions/midi.mx5bridge || exit 1
		echo 1 > functions/midi.mx5bridge/in_ports || exit 1
		echo 1 > functions/midi.mx5bridge/out_ports || exit 1
		ln -s functions/midi.mx5bridge configs/c.1 || exit 1
		echo "$UDC_NAME" > UDC || exit 1
	)
	if [ $? -ne 0 ]; then
		gadget_down
		return 1
	fi
	log "MIDI-only connection active"
	return 0
}

usb_mnt_ok() {
	# USB drive mounted on the device, not exported to the computer
	grep -qs " $MNT " /proc/mounts && [ ! -e "$CFG/g2" ]
}

case "$1" in
evil-begin)
	exec 9>"$LOCK"
	flock -w 10 9
	touch "$FLAG"
	gadget_down
	flock -u 9
	;;
evil-end)
	exec 9>"$LOCK"
	flock -w 10 9
	rm -f "$FLAG"
	flock -u 9
	;;
daemon)
	# Only one instance
	exec 8>/tmp/mx5bridge.daemon.lock
	flock -n 8 || exit 0
	sleep 25
	exec 9>"$LOCK"
	flock 9
	# Write the USB-drive notice only if missing or outdated (through 0.5: every
	# startup, plus diagnostics; diagnostics are now requested through action 'diag')
	if usb_mnt_ok && [ "$(head -n 1 "$DIR/firmware_aktiv.txt" 2>/dev/null)" != "MX5 Bridge @VERSION@ is active." ]; then
		mkdir -p "$DIR"
		{
			echo "MX5 Bridge @VERSION@ is active."
			echo
			echo "To disable USB-MIDI without USB audio, create a file named"
			echo "usb_midi_aus.txt in this folder and restart the @DEVICE@."
		} > "$DIR/firmware_aktiv.txt"
		sync
	fi
	flock -u 9
	if [ -e "$OFF_SWITCH" ]; then
		log "disabled by $OFF_SWITCH"
		exit 0
	fi
	idle=0
	fails=0
	while :; do
		sleep 3
		flock 9
		if [ -e "$FLAG" ] || [ -e "$CFG/g1" ] || [ -e "$CFG/g2" ]; then
			idle=0
		elif [ -e "$G" ]; then
			:
		else
			idle=$((idle + 1))
			if [ $idle -ge 2 ]; then
				if gadget_up; then
					fails=0
				else
					fails=$((fails + 1))
					log "Creation failed ($fails)"
				fi
				idle=0
			fi
		fi
		flock -u 9
		if [ $fails -ge 3 ]; then
			log "Too many errors; pausing"
			sleep 120
			fails=0
		fi
	done
	;;
*)
	echo "Usage: $0 evil-begin [audio|storage] | evil-end | daemon" >&2
	exit 2
	;;
esac
exit 0
