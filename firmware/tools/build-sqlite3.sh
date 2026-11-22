#!/bin/sh
# Build a static sqlite3 CLI for MX5 (32-bit ARM, hard-float).
# Linux/WSL (Ubuntu): sudo apt install gcc-arm-linux-gnueabihf unzip wget
# Output: ../bin/sqlite3 (build.py installs it as /usr/Evil/Scripts/mx5bridge-sqlite3)
set -e
cd "$(dirname "$0")"
VER=3530400
URL="https://sqlite.org/2026/sqlite-amalgamation-$VER.zip"
SHA3="628a44cfe82c66aed1ccbbe85a562d2e33ebe64b3288981ed76285612227934e"
mkdir -p ../bin work
cd work
[ -e "sqlite-amalgamation-$VER.zip" ] || wget -q "$URL"
echo "$SHA3  sqlite-amalgamation-$VER.zip" | sha3sum -a 256 -c - 2>/dev/null || \
	echo "(sha3sum unavailable or checksum mismatch; please verify manually)"
unzip -qo "sqlite-amalgamation-$VER.zip"
cd "sqlite-amalgamation-$VER"
arm-linux-gnueabihf-gcc -Os -static -o ../../../bin/sqlite3 shell.c sqlite3.c \
	-DSQLITE_THREADSAFE=0 -DSQLITE_OMIT_LOAD_EXTENSION -DSQLITE_ENABLE_JSON1 \
	-DHAVE_READLINE=0 -lm
arm-linux-gnueabihf-strip ../../../bin/sqlite3
ls -l ../../../bin/sqlite3
file ../../../bin/sqlite3 2>/dev/null || true
echo "Done: bin/sqlite3"
