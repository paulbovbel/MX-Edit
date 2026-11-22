#!/usr/bin/env bash
# Download NAM from GitHub and apply it to a stock Update.img (Linux/WSL).
#
# The NAM mod (Neural Amp Modeler replacing Anxiety OD) is by lolgab:
#     https://github.com/lolgab/headrush-nam-mod (GPLv3, NAM core MIT)
# It is NOT distributed with MX5 Bridge. This script downloads source for the
# specified version (NAM_REF), builds its command-line tool
# (gui-core-cli, same patch pipeline as the GUI) and applies it to the
# stock Update.img. build.py then adds the bridge:
#
#     bash tools/nam-mod.sh <stock Update.img> <NAM Update.img> [2|4]
#     python3 build.py <NAM Update.img> <Bridge+NAM Update.img>
#
# 2|4: NAM instance count (2: Anxiety OD only; 4: also Anxiety OD V2), default 2.
# Environment: NAM_REF  NAM tag or commit (default below; update deliberately by hand)
#              NAM_WORK Working folder (default ~/mx5bridge-nam, outside the repo)
#
# One-time dependencies (Ubuntu/WSL):
#     sudo apt install -y cmake gcc make bzip2 curl libext2fs-dev libblkid-dev uuid-dev liblzma-dev libcurl4-openssl-dev
# The script downloads the Bootlin ARM toolchain from the URL specified by
# the mod's docker/Dockerfile. Downloads remain cached in the working folder.
set -euo pipefail

REF=${NAM_REF:-v0.1.6}
REPO=https://github.com/lolgab/headrush-nam-mod
WORK=${NAM_WORK:-$HOME/mx5bridge-nam}

die() { echo "ERROR: $*" >&2; exit 1; }

[ $# -ge 2 ] || die "Usage: bash $0 <stock Update.img> <output Update.img> [2|4]"
IN=$(realpath "$1")
OUT=$(realpath -m "$2")
INST=${3:-2}
[ "$INST" = 2 ] || [ "$INST" = 4 ] || die "Instanzen muessen 2 oder 4 sein, nicht '$INST'"
[ -f "$IN" ] || die "Eingabe fehlt: $IN"
[ "$IN" != "$OUT" ] || die "Eingabe und Ausgabe muessen verschieden sein"

# Check tools (one apt command for all missing dependencies)
fehlt=""
for c in curl tar bzip2 cmake cc make sha256sum; do
	command -v "$c" >/dev/null 2>&1 || fehlt="$fehlt $c"
done
for h in ext2fs/ext2fs.h blkid/blkid.h uuid/uuid.h lzma.h curl/curl.h; do
	[ -f "/usr/include/$h" ] || [ -n "$(find /usr/include -path "*/$h" -print -quit 2>/dev/null)" ] \
		|| fehlt="$fehlt $h"
done
[ -z "$fehlt" ] || die "es fehlt:$fehlt
  sudo apt install -y cmake gcc make bzip2 curl libext2fs-dev libblkid-dev uuid-dev liblzma-dev libcurl4-openssl-dev"

# 1. NAM source (version REF) from GitHub
SRC=$WORK/src-$REF
TGZ=$WORK/headrush-nam-mod-$REF.tar.gz
mkdir -p "$WORK"
if [ ! -f "$SRC/CMakeLists.txt" ]; then
	echo "Downloading NAM mod $REF from $REPO ..."
	curl -fL -o "$TGZ.part" "$REPO/archive/$REF.tar.gz"
	mv "$TGZ.part" "$TGZ"
	rm -rf "$SRC"; mkdir -p "$SRC"
	tar -xzf "$TGZ" -C "$SRC" --strip-components=1
fi
[ -f "$SRC/tools/gui_core_cli.c" ] || die "$SRC ist keine NAM-Mod (tools/gui_core_cli.c fehlt)"
cd "$SRC"

# 2. ARM toolchain specified by NAM
TC_URL=$(sed -n 's/^ARG TOOLCHAIN_URL=//p' docker/Dockerfile | head -n 1)
[ -n "$TC_URL" ] || die "TOOLCHAIN_URL nicht in docker/Dockerfile gefunden"
TC=$WORK/$(basename "$TC_URL" .tar.bz2)
if [ ! -x "$TC/bin/arm-buildroot-linux-gnueabihf-g++" ]; then
	echo "Downloading ARM toolchain $(basename "$TC_URL") ..."
	rm -rf "$TC"; mkdir -p "$TC"
	curl -fL "$TC_URL" | tar -xj -C "$TC" --strip-components=1
	# Bootlin toolchains must adjust their paths once after extraction
	[ -x "$TC/relocate-sdk.sh" ] && "$TC/relocate-sdk.sh" >/dev/null
fi

# 3. NAM core and blobs (libnam_hook.so, libnam_preload.so, trampoline), using NAM scripts
./scripts/fetch_nam_core.sh
if [ ! -f blobs/libnam_hook.so ] || [ ! -f blobs/libnam_preload.so ] || [ ! -f blobs/trampoline_gonk.bin ]; then
	echo "Building NAM blobs (ARM, takes a few minutes) ..."
	PATH="$TC/bin:$PATH" ./scripts/build_blobs_native.sh
fi
touch blobs/*   # Otherwise CMake rebuilds them with Docker, as in the NAM CI

# 4. NAM command-line tool
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release >/dev/null
cmake --build build --target gui-core-cli --parallel >/dev/null
CLI=$SRC/build/gui-core-cli
[ -x "$CLI" ] || die "gui-core-cli wurde nicht gebaut"

# 5. Patching
echo "Applying NAM mod $REF (MX5, $INST instances) ..."
"$CLI" "$IN" "$OUT" --model mx5 --instances "$INST"

# Write source information next to output; build.py includes it in the image log
{
	echo "nam_mod_repo=$REPO"
	echo "nam_mod_ref=$REF"
	[ -f "$TGZ" ] && echo "nam_mod_tar_sha256=$(sha256sum "$TGZ" | cut -d' ' -f1)"
	echo "nam_instanzen=$INST"
	echo "toolchain=$(basename "$TC_URL")"
	echo "eingabe_sha1=$(sha1sum "$IN" | cut -d' ' -f1)"
	echo "ausgabe_sha1=$(sha1sum "$OUT" | cut -d' ' -f1)"
	echo "erstellt=$(date -Iseconds)"
} > "$OUT.nam.txt"
echo "Done: $OUT (Source info: $OUT.nam.txt)"
