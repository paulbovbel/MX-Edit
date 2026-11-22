#!/bin/sh
# MX5 Bridge @VERSION@ (unofficial)
# Write diagnostics to the device's USB drive (MX5Bridge folder) and print the
# path. Usage: mx5bridge-diag.sh <tag> (0.6+: on demand only, via action 'diag')

TAG="${1:-manual}"
MNT=/media/az01-internal/Evil/usb_mnt
DIR="$MNT/MX5Bridge"

# Write only if the USB image is mounted on the device
grep -qs " $MNT " /proc/mounts || exit 0
mkdir -p "$DIR" || exit 0

OUT="$DIR/diag_$TAG.txt"
J=/tmp/mx5bridge-journal.txt
journalctl -b --no-pager > "$J" 2>&1
{
	echo "=== MX5 Bridge diagnostics ($TAG) ==="
	date
	uname -a
	echo
	echo "=== Firmware ==="
	cat /tmp/evil-firmware-version 2>/dev/null; echo
	echo
	echo "=== USB-Gadget g1 ==="
	ls -la /sys/kernel/config/usb_gadget/g1/functions 2>&1
	ls -la /sys/kernel/config/usb_gadget/g1/configs/c.1 2>&1
	cat /sys/kernel/config/usb_gadget/g1/UDC 2>&1
	echo
	echo "=== ALSA cards ==="
	cat /proc/asound/cards 2>&1
	echo
	echo "=== ALSA rawmidi ==="
	ls -la /dev/snd 2>&1
	for f in /proc/asound/card*/midi*; do echo "--- $f"; cat "$f" 2>&1; done
	echo
	echo "=== ALSA Sequencer ==="
	cat /proc/asound/seq/clients 2>&1
	echo
	aconnect -l 2>&1
	echo
	echo "=== Journal: relevant lines ==="
	grep -i -E "midi|assign|bridge|device url|qml|gadget|f_midi|usb" "$J" | tail -n 800
	echo
	echo "=== Journal: last 1500 lines ==="
	tail -n 1500 "$J"
	echo
	echo "=== dmesg (tail) ==="
	dmesg 2>&1 | tail -n 300
} > "$OUT" 2>&1
rm -f "$J"

sync
echo "$OUT"
exit 0
