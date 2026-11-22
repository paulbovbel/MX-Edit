#!/bin/sh
# MX5 Bridge @VERSION@ (unofficial): database service
#
# Answer bridge QML requests against the device's rig database.
# Exchange files under /tmp/mx5bridge (name = kind + sequence number):
#   q<N>.sql   Read request (0x08): read-only SQL (sqlite3 -safe disables
#              .system/.shell and readfile/writefile; fsdir still reads directories)
#   w<N>.sql   Write request (0x09): SQL statements executed as one
#              transaction (begin immediate ... commit); the first error
#              aborts and rolls back everything. Before the first write
#              after startup, create an online database backup.
#   a<N>.sql   Action (0x0A): status | backup | restore | restart | diag |
#              nam-on | nam-off (toggle NAM and restart the app)
#   s<N>.sql   Shell (0x0B): script executed as root; reply [{rc, out}] includes stdout
#              and stderr; client file access uses this channel
#   *.go       Marker: request completely written
#   *.r/       Reply: directory whose filenames encode JSON {ok, rows, err},
#              base64url in 240-character chunks: <number starting at 10000>_<chunk>;
#              QML reads these via App.entryList. Through 0.5 it imported a JS file,
#              which grew app memory by ~2.5 KB per import.
#   *.rdy      Marker: reply complete
#   *.done     Marker: reply read; remove all files for N
#
# Wait using read -t on a private FIFO (no subprocess per iteration): 40 ms while
# requests arrive, 100 ms after 5 s idle. BusyBox has no timeout command; run_tmo
# kills timed-out commands using a watchdog subshell that also waits via read -t.
#
# Backup: $BAK (sqlite3 .backup, consistent even while the app is running).
# $WRITTEN marks writes since the backup.
# Recovery is coordinated by /usr/Evil/Scripts/evil:
#   prestart   Before each app launch: restore backup if $RESTORE was requested
#              or the app crashed shortly after startup three times
#              and writes have occurred since backup
#   postexit   After each app exit: maintain the crash counter
#
# Optional NAM mod (github.com/lolgab/headrush-nam-mod), only when included in firmware:
# the startup loop launches with NAM (LD_PRELOAD) only if
# 'mx5bridge-db.sh nam' succeeds. NAM stays enabled while $NAM_OFF is absent.
# nam-on/nam-off toggle it and restart the app. After three consecutive crashes
# shortly after launch with NAM, postexit disables NAM ($NAM_OFF contains 'auto')
# before considering database recovery.
#
# Usage: mx5bridge-db.sh daemon        (launched by runevil)
#        mx5bridge-db.sh prestart      (from /usr/Evil/Scripts/evil)
#        mx5bridge-db.sh nam           (from evil: exit 0 means launch with NAM)
#        mx5bridge-db.sh postexit <exitcode> <start-time>

EVIL=/media/az01-internal/Evil
DB=$EVIL/evil.db
DIR=/tmp/mx5bridge
SQLITE=/usr/Evil/Scripts/mx5bridge-sqlite3
DIAG=/usr/Evil/Scripts/mx5bridge-diag.sh
LOCK=/tmp/mx5bridge.db.lock
STATE=$EVIL/mx5bridge          # Survives factory reset (which only clears Resources and usb_mnt)
BAK=$STATE/evil.db.mx5bak
BAD=$STATE/evil.db.mx5bad      # Database replaced during recovery
WRITTEN=$STATE/written         # Writes have occurred since backup
RESTORE=$STATE/restore         # Recovery requested for next app launch
RESTART=$DIR/restart           # App restart requested by bridge (not shutdown)
CRASHES=$DIR/crashes           # Consecutive early-crash counter
TICK=$DIR/tick.fifo            # Wait without subprocesses (read -t); no writers
NAM_LIB=/usr/Evil/libnam_preload.so     # Only in firmware built with NAM
NAM_INFO=/usr/Evil/Scripts/mx5bridge-nam.txt  # NAM source information (build.py)
NAM_OFF=$STATE/nam-aus         # NAM disabled ('auto' means disabled by crash guard)
NAM_ACTIVE=$DIR/nam-aktiv      # Running app was launched with NAM
CRASH_SECS=90                  # App exit within this interval after launch counts as a crash
CRASH_MAX=3
T_READ=20                      # Timeouts in seconds
T_WRITE=30
T_SHELL=60
T_BACKUP=60

log() { logger -t mx5bridge-db -- "$*"; }

# Timed command: run_tmo <seconds> <input> <output> <error> command...
# Return the command's exit code, or 137 on timeout (with a message in <error>).
run_tmo() {
	secs=$1 in=$2 out=$3 err=$4
	shift 4
	"$@" < "$in" > "$out" 2> "$err" &
	pid=$!
	( read -t "$secs" x <&7; kill -9 $pid 2>/dev/null ) &
	wd=$!
	wait $pid
	rc=$?
	kill $wd 2>/dev/null
	wait $wd 2>/dev/null
	[ $rc -eq 137 ] && echo "Timeout after $secs s; terminated" >> "$err"
	return $rc
}

# JSON string content without surrounding quotes: escape backslash, quote, TAB and LF;
# discard other control characters
json_string() {
	tr -d '\000-\010\013\014\016-\037' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\r/\\r/g' -e "s/$TAB/\\\\t/g" |
		awk 'BEGIN{ORS=""} NR>1{print "\\n"} {print}'
}

# Store a reply: reply <base> <json-rows or empty> <error-file or empty>
# JSON {ok, rows, err} -> base64url -> 240-character chunks -> filenames in <base>.r
reply() {
	{
		if [ -n "$3" ]; then
			printf '{"ok":0,"rows":[],"err":"'
			json_string < "$3"
			printf '"}'
		else
			printf '{"ok":1,"err":"","rows":'
			if [ -n "$2" ] && [ -s "$2" ]; then cat "$2"; else printf '[]'; fi
			printf '}'
		fi
	} > "$1.json"
	rm -rf "$1.rtmp"
	mkdir "$1.rtmp" || return 1
	# Use awk + xargs instead of a read loop: BusyBox reads pipes byte by byte
	# (1.6 MB takes 28 s instead of 1 s). Names use only [0-9A-Za-z_/-], so xargs splitting is safe.
	base64 -w 0 < "$1.json" | tr '+/' '-_' | fold -w 240 |
		awk -v d="$1.rtmp" '{ printf "%s/%d_%s\n", d, NR + 9999, $0 }' | xargs -r touch
	mv "$1.rtmp" "$1.r" && : > "$1.rdy"
	rm -f "$1.json"
}

fail() {   # fail <base> <text>
	echo "$2" > "$1.err"
	reply "$1" "" "$1.err"
	rm -f "$1.sql" "$1.go" "$1.out" "$1.err"
}

free_kb() {
	df -k "$EVIL" 2>/dev/null | awk 'NR==2{print $4}'
}

file_kb() {
	[ -e "$1" ] && du -k "$1" | awk '{print $1}' || echo 0
}

# Create a backup; return 0 on success, otherwise error text on stdout
backup() {
	mkdir -p "$STATE" || { echo "Cannot create backup folder"; return 1; }
	need=$(( $(file_kb "$DB") + 2048 ))
	have=$(( $(free_kb) + $(file_kb "$BAK") ))
	if [ "$have" -lt "$need" ]; then
		echo "Not enough space for backup ($have KB free, $need KB required)"
		return 1
	fi
	rm -f "$BAK.tmp"
	if run_tmo $T_BACKUP /dev/null /dev/null "$DIR/backup.err" \
			"$SQLITE" -cmd ".timeout 5000" "file:$DB?mode=ro" ".backup '$BAK.tmp'"; then
		mv "$BAK.tmp" "$BAK" && rm -f "$WRITTEN" && sync
		log "Backup created: $BAK ($(file_kb "$BAK") KB)"
		return 0
	fi
	rm -f "$BAK.tmp"
	echo "Backup failed: $(cat "$DIR/backup.err")"
	return 1
}

# Read request (-safe: no file writes or shell execution on the read channel)
answer_read() {
	base="$1"
	if run_tmo $T_READ "$base.sql" "$base.out" "$base.err" \
			"$SQLITE" -safe -readonly -json -cmd ".timeout 3000" "file:$DB?mode=ro"; then
		reply "$base" "$base.out" ""
	else
		reply "$base" "" "$base.err"
	fi
	rm -f "$base.sql" "$base.go" "$base.out" "$base.err"
}

# Write request: one transaction, abort on first error (-bail rolls back)
answer_write() {
	base="$1"
	if [ ! -e "$DIR/backup-done" ]; then
		# First write since startup: back up first
		if ! msg=$(backup); then
			fail "$base" "$msg"
			return
		fi
		touch "$DIR/backup-done"
	fi
	touch "$WRITTEN"
	{
		echo "pragma foreign_keys = on;"
		echo "begin immediate;"
		cat "$base.sql"
		echo ";"
		echo "select total_changes() as changes;"
		echo "commit;"
	} > "$base.tx"
	if run_tmo $T_WRITE "$base.tx" "$base.out" "$base.err" \
			"$SQLITE" -safe -bail -json -cmd ".timeout 3000" "$DB"; then
		sync
		reply "$base" "$base.out" ""
	else
		reply "$base" "" "$base.err"
	fi
	rm -f "$base.sql" "$base.go" "$base.tx" "$base.out" "$base.err"
}

# Shell request: run script as root; return stdout/stderr and exit code
answer_shell() {
	base="$1"
	run_tmo $T_SHELL /dev/null "$base.out" "$base.err" sh -c 'sh "$1" 2>&1' shell "$base.sql"
	rc=$?
	cat "$base.err" >> "$base.out"
	{
		printf '[{"rc":%d,"out":"' "$rc"
		json_string < "$base.out"
		# json_string loses the trailing newline; preserve it in the output
		[ -s "$base.out" ] && [ "$(tail -c 1 "$base.out" | od -An -tx1 | tr -d ' ')" = "0a" ] && printf '\\n'
		printf '"}]'
	} > "$base.rows"
	reply "$base" "$base.rows" ""
	rm -f "$base.sql" "$base.go" "$base.out" "$base.err" "$base.rows"
}

# Actions
answer_action() {
	base="$1"
	act=$(head -n 1 "$base.sql" | tr -d '\r\n ')
	case "$act" in
	status)
		bak_kb=$(file_kb "$BAK")
		bak_age=-1
		[ -e "$BAK" ] && bak_age=$(( $(date +%s) - $(date -r "$BAK" +%s 2>/dev/null || echo 0) ))
		[ -e "$WRITTEN" ] && written=1 || written=0
		[ -e "$BAD" ] && bad=1 || bad=0
		crashes=$(cat "$CRASHES" 2>/dev/null || echo 0)
		echo "[{\"db_kb\":$(file_kb "$DB"),\"free_kb\":$(free_kb),\"backup_kb\":$bak_kb,\"backup_age\":$bak_age,\"written\":$written,\"bad\":$bad,\"crashes\":$crashes,$(nam_json),\"version\":\"@VERSION@\"}]" > "$base.out"
		reply "$base" "$base.out" ""
		;;
	nam-on|nam-off)
		if [ ! -e "$NAM_LIB" ]; then
			echo "This firmware does not include NAM" > "$base.err"
			reply "$base" "" "$base.err"
		else
			mkdir -p "$STATE"
			if [ "$act" = nam-on ]; then rm -f "$NAM_OFF"; else echo manual > "$NAM_OFF"; fi
			sync
			log "NAM ${act#nam-} (app restart)"
			echo "[{$(nam_json),\"restarting\":1}]" > "$base.out"
			reply "$base" "$base.out" ""
			restart_app
		fi
		;;
	backup)
		if msg=$(backup); then
			echo "[{\"backup_kb\":$(file_kb "$BAK")}]" > "$base.out"
			touch "$DIR/backup-done"
			reply "$base" "$base.out" ""
		else
			echo "$msg" > "$base.err"
			reply "$base" "" "$base.err"
		fi
		;;
	restore)
		if [ ! -e "$BAK" ]; then
			echo "no backup available" > "$base.err"
			reply "$base" "" "$base.err"
		else
			mkdir -p "$STATE"
			touch "$RESTORE"
			echo '[{"restarting":1}]' > "$base.out"
			reply "$base" "$base.out" ""
			restart_app
		fi
		;;
	restart)
		echo '[{"restarting":1}]' > "$base.out"
		reply "$base" "$base.out" ""
		restart_app
		;;
	diag)
		# On-demand diagnostics (through 0.5: every startup): file on the device's USB drive
		if run_tmo $T_SHELL /dev/null "$base.out" "$base.err" "$DIAG" manual && [ -s "$base.out" ]; then
			printf '[{"file":"' > "$base.rows"
			head -n 1 "$base.out" | json_string >> "$base.rows"
			printf '"}]' >> "$base.rows"
			reply "$base" "$base.rows" ""
		else
			echo "Diagnostics not written (USB drive not mounted?) $(cat "$base.err")" > "$base.err"
			reply "$base" "" "$base.err"
		fi
		rm -f "$base.rows"
		;;
	*)
		echo "unknown action: $act" > "$base.err"
		reply "$base" "" "$base.err"
		;;
	esac
	rm -f "$base.sql" "$base.go" "$base.out" "$base.err"
}

# NAM state as JSON fields without braces: nam_installed, nam_on (next launch),
# nam_active (running app), nam_auto_off (disabled by crash guard), nam_ref (version)
nam_json() {
	if [ ! -e "$NAM_LIB" ]; then
		printf '"nam_installed":0,"nam_on":0,"nam_active":0,"nam_auto_off":0,"nam_ref":""'
		return
	fi
	[ -e "$NAM_OFF" ] && on=0 || on=1
	[ -e "$NAM_ACTIVE" ] && active=1 || active=0
	[ "$(cat "$NAM_OFF" 2>/dev/null)" = auto ] && auto=1 || auto=0
	ref=$(sed -n 's/^nam_mod_ref=//p' "$NAM_INFO" 2>/dev/null | head -n 1 | json_string)
	printf '"nam_installed":1,"nam_on":%s,"nam_active":%s,"nam_auto_off":%s,"nam_ref":"%s"' \
		$on $active $auto "$ref"
}

# Before app launch (from evil, after prestart): launch with NAM? Save the answer for
# postexit and status.
nam_start() {
	mkdir -p "$DIR"
	if [ -e "$NAM_LIB" ] && [ ! -e "$NAM_OFF" ]; then
		touch "$NAM_ACTIVE"
		return 0
	fi
	rm -f "$NAM_ACTIVE"
	return 1
}

# Stop the app; evil's startup loop relaunches it. The marker prevents
# exit code 0 from being interpreted as a shutdown request.
restart_app() {
	read -t 1 x <&7   # Let the reply reach QML first
	touch "$RESTART"
	log "App restart requested"
	for p in /proc/[0-9]*; do
		[ "$(cat "$p/comm" 2>/dev/null)" = "Evil" ] && kill -TERM "${p#/proc/}"
	done
}

# Before app launch (from /usr/Evil/Scripts/evil): recovery
prestart() {
	mkdir -p "$DIR"
	if [ -e "$RESTORE" ]; then
		rm -f "$RESTORE"
		if [ -e "$BAK" ]; then
			log "Restoring database from backup"
			rm -f "$BAD"
			mv "$DB" "$BAD" 2>/dev/null
			mv "$DB-journal" "$BAD-journal" 2>/dev/null
			if cp "$BAK" "$DB"; then
				rm -f "$WRITTEN" "$CRASHES"
				sync
				log "Recovery complete"
			else
				log "Recovery failed; keeping the old database"
				mv "$BAD" "$DB" 2>/dev/null
			fi
		else
			log "Wiederherstellung angefordert, aber no backup available"
		fi
	fi
}

# After app exit: detect a crash loop
postexit() {
	code="$1"; start="$2"
	mkdir -p "$DIR"
	[ -e "$RESTART" ] && { rm -f "$CRASHES"; return; }
	now=$(date +%s)
	if [ "$code" != "0" ] && [ $(( now - start )) -lt "$CRASH_SECS" ]; then
		n=$(( $(cat "$CRASHES" 2>/dev/null || echo 0) + 1 ))
		echo "$n" > "$CRASHES"
		log "App exited shortly after launch (code $code), consecutive failure $n"
		if [ "$n" -ge "$CRASH_MAX" ] && [ -e "$NAM_ACTIVE" ]; then
			log "Crash loop with NAM; disabling NAM (nam-on re-enables it)"
			mkdir -p "$STATE"
			echo auto > "$NAM_OFF"
			sync
			rm -f "$CRASHES"
		elif [ "$n" -ge "$CRASH_MAX" ] && [ -e "$WRITTEN" ] && [ -e "$BAK" ]; then
			log "Crash loop after bridge write; restoring backup"
			touch "$RESTORE"
			rm -f "$CRASHES"
		fi
	else
		rm -f "$CRASHES"
	fi
}

TAB=$(printf '\t')

case "$1" in
daemon)
	exec 8>"$LOCK"
	flock -n 8 || exit 0
	mkdir -p "$DIR"
	rm -rf "$DIR"/q* "$DIR"/w* "$DIR"/a* "$DIR"/s* "$TICK"
	mkfifo "$TICK" && exec 7<> "$TICK" || exit 1
	log "Database service @VERSION@ started"
	quiet=0
	while :; do
		busy=0
		for go in "$DIR"/q*.go "$DIR"/w*.go "$DIR"/a*.go "$DIR"/s*.go; do
			[ -e "$go" ] || continue
			busy=1
			base="${go%.go}"
			case "${base##*/}" in
			q*) answer_read "$base" ;;
			w*) answer_write "$base" ;;
			a*) answer_action "$base" ;;
			s*) answer_shell "$base" ;;
			esac
		done
		for done in "$DIR"/q*.done "$DIR"/w*.done "$DIR"/a*.done "$DIR"/s*.done; do
			[ -e "$done" ] || continue
			rm -rf "${done%.done}".*
		done
		# Wait 40 ms during requests and for 5 s afterwards, otherwise 100 ms; no subprocess
		if [ $busy -eq 1 ]; then quiet=0; elif [ $quiet -lt 125 ]; then quiet=$((quiet + 1)); fi
		if [ $quiet -lt 125 ]; then read -t 0.04 x <&7; else read -t 0.1 x <&7; fi
	done
	;;
prestart)
	prestart
	;;
nam)
	nam_start
	;;
postexit)
	postexit "$2" "$3"
	;;
*)
	echo "Usage: $0 daemon | prestart | nam | postexit <code> <start>" >&2
	exit 2
	;;
esac
