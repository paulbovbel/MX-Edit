#!/bin/sh
# Local mx5bridge-db.sh integration test on WSL (redirected paths, 2 s shell timeout).
# Usage: sh daemontest.sh ../mod/mx5bridge-db.sh (requires sqlite3, flock, logger, busybox)
# The service runs under busybox sh, matching the device.
set -e
T=/tmp/mx5btest
rm -rf "$T"; mkdir -p "$T/evil" "$T/tmp"
SRC="$1"
sed -e "s#^EVIL=.*#EVIL=$T/evil#" -e "s#^DIR=.*#DIR=$T/tmp#" -e "s#^SQLITE=.*#SQLITE=/usr/bin/sqlite3#" \
    -e "s#^LOCK=.*#LOCK=$T/lock#" -e "s#^DIAG=.*#DIAG=/bin/false#" -e "s#^T_SHELL=.*#T_SHELL=2#" \
    -e "s#^NAM_LIB=.*#NAM_LIB=$T/nam/libnam_preload.so#" -e "s#^NAM_INFO=.*#NAM_INFO=$T/nam/info.txt#" \
    -e "s#@VERSION@#test#g" "$SRC" > "$T/db.sh"
chmod +x "$T/db.sh"
DB=$T/evil/evil.db
sqlite3 "$DB" "create table rigs (id text primary key collate nocase, name text not null collate nocase, content text not null, show_order integer default 0, author text default '', is_readonly integer default 0, color integer default 0, created_at integer not null default (strftime('%s', 'now')), prog_num integer default -1, unique(id));
create table setlists (id text primary key, name text not null);
create table setlist_rigs (id text primary key, setlist_id text not null, rig_id text, show_order integer default 0, foreign key (setlist_id) references setlists(id) on delete cascade, foreign key (rig_id) references rigs(id) on delete cascade);
insert into rigs(id,name,content) values('r1','ONE','{}'),('r2','TWO','{}');
insert into setlists values('s1','SET'); insert into setlist_rigs values('x1','s1','r1',0),('x2','s1','r2',1);"

busybox sh "$T/db.sh" daemon &
DPID=$!
sleep 0.3
n=0
FAIL=0
# Read reply directory like QML: sort names, strip sequence, base64url -> text
decode() { ls "$1" | sort | sed 's/^[0-9]*_//' | tr -d '\n' | tr -- '-_' '+/' | base64 -d; }
ask() {   # ask <kind> <text>   -> Reply in $REPLY_TEXT
	n=$((n+1)); base="$T/tmp/$1$n"
	printf '%s' "$2" > "$base.sql"; echo go > "$base.go"
	i=0; while [ ! -e "$base.rdy" ]; do sleep 0.05; i=$((i+1)); [ $i -gt 200 ] && { echo "TIMEOUT $1$n"; FAIL=1; return 1; }; done
	decode "$base.r" > "$T/reply.json"
	echo "--- $1: $(printf '%s' "$2" | head -c 90)"
	head -c 300 "$T/reply.json"; echo
	python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$T/reply.json" 2>/dev/null || { echo "!!! invalid JSON"; FAIL=1; }
	echo done > "$base.done"
}
expect() {   # expect <Python expression using r>
	python3 -c "import json,sys; r=json.load(open(sys.argv[1])); assert $1, r" "$T/reply.json" 2>/dev/null && echo "    ok: $1" || { echo "!!! expected: $1"; FAIL=1; }
}
ask q "select count(*) n from rigs"; expect 'r["ok"] and r["rows"][0]["n"] == 2'
ask a "status"; expect 'r["rows"][0]["version"] == "test"'
ask w "insert into rigs(id,name,content) values('r3','THREE','{}')"; expect 'r["rows"][0]["changes"] == 1'
ls -la "$T/evil" "$T/evil/mx5bridge" | grep -v total
ask q "select id from rigs order by id"; expect '[x["id"] for x in r["rows"]] == ["r1","r2","r3"]'
ask w "insert into rigs(id,name,content) values('r4','FOUR','{}'); insert into rigs(id,name,content) values('r3','DUP','{}')"; expect 'not r["ok"] and "UNIQUE" in r["err"]'
ask q "select id from rigs order by id"; expect 'len(r["rows"]) == 3'
ask w "delete from rigs where id='r1'"
ask q "select rig_id from setlist_rigs order by rig_id"; expect '[x["rig_id"] for x in r["rows"]] == ["r2"]'
ask a "backup"; expect 'r["ok"] and r["rows"][0]["backup_kb"] > 0'
ask a "nonsense"; expect 'not r["ok"] and "unknown action" in r["err"]'
# New in 0.6
ask q "select 'Grüße € \"x\" ' || char(92) || ' tab' || char(9) || 'ende' as t"; expect 'r["rows"][0]["t"] == "Grüße € " + chr(34) + "x" + chr(34) + " " + chr(92) + " tab" + chr(9) + "ende"'
ask q "select readfile('/etc/hostname') as f"; expect 'not r["ok"]'
ask q "select 1 from fsdir('/tmp')"; echo "    (fsdir: allowed read-only in device sqlite 3.53 even with -safe)"
ask q ".system echo pwned"; expect 'not r["ok"] or "pwned" not in json.dumps(r)'
ask q "with recursive c(i) as (select 1 union all select i+1 from c where i < 3000) select i, 'A row with accented text: ä' as t from c"; expect 'len(r["rows"]) == 3000'
echo "    (3000 rows = $(wc -c < "$T/reply.json") bytes of JSON)"
ask s "echo hello; echo error >&2; exit 3"; expect 'r["rows"][0]["rc"] == 3 and "hello" in r["rows"][0]["out"] and "error" in r["rows"][0]["out"]'
ask s 'x=a; b=$(printf "%s" "\\"); printf "%s%s%sc	E
" "$x" "$b" "$b"'; expect 'r["rows"][0]["out"] == "a" + chr(92) * 2 + "c" + chr(9) + "E" + chr(10)'
ask s "sleep 5; echo late"; expect 'r["rows"][0]["rc"] == 137 and "Timeout" in r["rows"][0]["out"]'
KEEP=$FAIL; ask q "select 1; select 2"; FAIL=$KEEP; echo "    (two result sets: invalid JSON is a known, permitted outcome)"
# Simulate recovery
ask w "delete from rigs"
ask q "select count(*) n from rigs"; expect 'r["rows"][0]["n"] == 0'
touch "$T/evil/mx5bridge/restore"
busybox sh "$T/db.sh" prestart
ask q "select count(*) n from rigs"; expect 'r["rows"][0]["n"] == 2'
ls "$T/evil/mx5bridge"
# Simulate a crash loop: three short runs exiting 139 after a write
ask w "delete from rigs"
now=$(date +%s)
busybox sh "$T/db.sh" postexit 139 $now; busybox sh "$T/db.sh" postexit 139 $now; cat "$T/tmp/crashes"; busybox sh "$T/db.sh" postexit 139 $now
ls "$T/evil/mx5bridge"
busybox sh "$T/db.sh" prestart
ask q "select count(*) n from rigs"; expect 'r["rows"][0]["n"] == 2'
# Restart marker suppresses crash counting
touch "$T/tmp/restart"; busybox sh "$T/db.sh" postexit 143 $now; ls "$T/tmp"
# NAM mod (0.7): absent without library; toggling rejected
rm -f "$T/tmp/restart"
ask a "status"; expect 'r["rows"][0]["nam_installed"] == 0 and r["rows"][0]["nam_on"] == 0'
ask a "nam-on"; expect 'not r["ok"] and "does not include NAM" in r["err"]'
busybox sh "$T/db.sh" nam && { echo "!!! nam succeeds without a library"; FAIL=1; } || echo "    ok: nam without library -> disabled"
mkdir -p "$T/nam"; touch "$T/nam/libnam_preload.so"; printf 'nam_mod_ref=v0.1.6\nnam_instanzen=2\n' > "$T/nam/info.txt"
busybox sh "$T/db.sh" nam && echo "    ok: nam enabled -> yes" || { echo "!!! nam reports disabled"; FAIL=1; }
ask a "status"; expect 'r["rows"][0]["nam_installed"] == 1 and r["rows"][0]["nam_on"] == 1 and r["rows"][0]["nam_active"] == 1 and r["rows"][0]["nam_ref"] == "v0.1.6"'
ask a "nam-off"; expect 'r["ok"] and r["rows"][0]["nam_on"] == 0 and r["rows"][0]["restarting"] == 1'
sleep 1.3   # restart_app lets the reply through first
[ -e "$T/tmp/restart" ] && echo "    ok: Restart marker set" || { echo "!!! No restart marker"; FAIL=1; }
rm -f "$T/tmp/restart"
busybox sh "$T/db.sh" nam && { echo "!!! nam succeeds while disabled"; FAIL=1; } || echo "    ok: nam disabled -> no"
ask a "status"; expect 'r["rows"][0]["nam_on"] == 0 and r["rows"][0]["nam_active"] == 0 and r["rows"][0]["nam_auto_off"] == 0'
ask a "nam-on"; expect 'r["ok"] and r["rows"][0]["nam_on"] == 1'
sleep 1.3; rm -f "$T/tmp/restart"
# Crash loop with NAM: disable NAM first (no recovery); without NAM, recover the database
ask w "delete from rigs where id='r2'"
busybox sh "$T/db.sh" nam
busybox sh "$T/db.sh" postexit 139 $now; busybox sh "$T/db.sh" postexit 139 $now; busybox sh "$T/db.sh" postexit 139 $now
[ "$(cat "$T/evil/mx5bridge/nam-aus" 2>/dev/null)" = auto ] && echo "    ok: NAM disabled by crash guard" || { echo "!!! NAM not disabled"; FAIL=1; }
[ -e "$T/evil/mx5bridge/restore" ] && { echo "!!! Recovery requested despite NAM"; FAIL=1; } || echo "    ok: No recovery requested"
busybox sh "$T/db.sh" nam && { echo "!!! nam succeeds after a crash loop"; FAIL=1; } || echo "    ok: Next launch without NAM"
ask a "status"; expect 'r["rows"][0]["nam_on"] == 0 and r["rows"][0]["nam_auto_off"] == 1'
busybox sh "$T/db.sh" postexit 139 $now; busybox sh "$T/db.sh" postexit 139 $now; busybox sh "$T/db.sh" postexit 139 $now
[ -e "$T/evil/mx5bridge/restore" ] && echo "    ok: Recovery takes effect without NAM" || { echo "!!! No recovery"; FAIL=1; }
busybox sh "$T/db.sh" prestart
# Cleanup after .done
sleep 0.3
left=$(ls "$T/tmp" | grep -E '^[qwas][0-9]' || true)
[ -z "$left" ] && echo "    ok: All request files cleaned up" || { echo "!!! Remaining: $left"; FAIL=1; }
kill $DPID
echo
[ $FAIL -eq 0 ] && echo "ALL OK" || echo "ERROR (see !!!)"
