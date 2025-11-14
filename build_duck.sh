#!/usr/bin/env bash
# build_duck.sh – online/offline UltraWiFiDuck builder (self-contained)

set -euo pipefail
umask 0000         # inherits lax permissions without needing extra chmod
############################## arguments ######################################
SERIAL=/dev/ttyUSB0
REPO_OVERRIDE=""
for a in "$@"; do
  [[ $a == /dev/tty* ]]   && SERIAL=$a
  [[ $a == --repo=* ]]    && REPO_OVERRIDE="${a#--repo=}"
done

############################## paths & tag ####################################
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$HERE/duck_build
IMG=uwd_pio_offline:latest                # one canonical tag
# mkdir -p "$ROOT" "$ROOT/docker"         # has too strict files' restrictions
install -d -m 0777 "$ROOT" "$ROOT/docker" # ensures lax restrictions

######################## 0. offer to load saved tar ###########################
IMAGE_LOADED=0
LATEST=$(ls -1t "$HERE"/build_duck-*.tar.gz 2>/dev/null | head -n1 || true)
if [[ -n $LATEST ]]; then
  read -rp "Load $(basename "$LATEST") into Docker? [y/N] " ans
  if [[ ${ans,,} == y ]]; then
     sudo docker load -i "$LATEST"
     IMAGE_LOADED=1
  fi
fi

######################## 1. build image if needed #############################
if [[ $IMAGE_LOADED == 0 && -z $(sudo docker image ls -q "$IMG") ]]; then
cat >"$ROOT/docker/Dockerfile" <<'DOCKER'
# FROM python:3.12-slim
# RUN apt-get update && \
#     apt-get install -y --no-install-recommends \
#         git build-essential libusb-1.0-0 ca-certificates && \
#     pip install --no-cache-dir platformio esptool && \
#     useradd -m pio
# USER pio
# WORKDIR /w
# install the ESP32 tool-chain into the image layer
# dummy RUN platformio platform install espressif32@6.3.2 --force
## just my need
# RUN platformio platform install espressif32@6.3.2 \
#         --with-package framework-arduinoespressif32 \
#         --with-package toolchain-xtensa-esp32s2-elf \
#         --with-package tool-cmake \
#         --with-package tool-ninja \
#         --with-package mklittlefs \
#         --with-package tool-esptoolpy \
#         --force --silent
## all S2,S3, C3, etc. boards supported:
# RUN platformio platform install espressif32@6.3.2 --with-all-packages --force --silent
## first bigger
# RUN echo "[env:s2]\nplatform = espressif32@6.3.2\nboard = esp32-s2-kaluga-1\nframework = arduino" > platformio.ini && \
#     mkdir -p src && \
#     echo "void setup(){} void loop(){}" > src/main.cpp && \
#     platformio run --silent && \
#     rm -rf /tmp/bootstrap
##
# ########################################################################
# # 1️⃣  base image + PIO core                                            #
# ########################################################################
# FROM python:3.12-slim
# RUN apt-get update && \
#     apt-get install -y --no-install-recommends \
#         git build-essential libusb-1.0-0 ca-certificates && \
#     pip install --no-cache-dir platformio esptool && \
#     useradd -m pio
# USER pio
# WORKDIR /w
#
# ########################################################################
# # 2️⃣  bootstrap — pull **all** Kaluga-S2 packages once                 #
# ########################################################################
# # create a one-file project
# RUN mkdir /tmp/bootstrap
# WORKDIR /tmp/bootstrap
# RUN printf "[env:s2]\nplatform = espressif32@6.3.2\nboard = esp32-s2-kaluga-1\nframework = arduino\n" \
#         > platformio.ini && \
#     mkdir src && echo "void setup(){} void loop(){}" > src/main.cpp
#
# # full build of the dummy sketch ⇒ downloads framework, tool-chains, cmake, ninja, mklittlefs…
# RUN platformio run -e s2 --silent
#
# # grab the renamed compiler that PIO later asks for
# RUN pio pkg install -g -t toolchain-xtensa-esp32s2@8.4.0+2021r2-patch5 \
#         --silent --force
#
# # clean up the throw-away project
# WORKDIR /w
# RUN rm -rf /tmp/bootstrap
# ##

##   AllTools Stage 1  –  create a throw-away project and let PIO pull everything once #
FROM python:3.12-slim AS bootstrap
RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential libusb-1.0-0 ca-certificates \
    && pip install --no-cache-dir platformio esptool \
    && useradd -m pio
USER pio
WORKDIR /tmp/bootstrap
RUN printf "[env:s2]\nplatform = espressif32@6.3.2\nboard = esp32-s2-kaluga-1\nframework = arduino\n" \
       > platformio.ini \
 && mkdir src && echo "void setup(){} void loop(){}" > src/main.cpp \
 && platformio run -e s2 --silent                     # downloads all tool-chains

##   Stage 2  –  final image with the pre-filled ~/.platformio folder #
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential libusb-1.0-0 ca-certificates \
    && pip install --no-cache-dir platformio esptool \
    && useradd -m pio
RUN printf "[env:s2]\nplatform = espressif32@6.3.2\nboard = esp32-s2-kaluga-1\nframework = arduino\n" \
        > platformio.ini && \
    mkdir src && echo "void setup(){} void loop(){}" > src/main.cpp

# full build of the dummy sketch ⇒ downloads framework, tool-chains, cmake, ninja, mklittlefs…
RUN platformio run -e s2 --silent

# grab the renamed compiler that PIO later asks for
RUN pio pkg install -g -t toolchain-xtensa-esp32s2@8.4.0+2021r2-patch5 \
        --silent --force
##   AllTools

# copy the pre-populated package folder from the bootstrap stage
COPY --from=bootstrap \
     --chown=pio:pio \
     /home/pio/.platformio /home/pio/.platformio
USER pio
WORKDIR /w
RUN  chown -R pio:pio /home/pio/.platformio
RUN mkdir -p /home/pio/.platformio/lib && \
    cd /home/pio/.platformio/lib && \
    git clone --depth 1 https://github.com/mathieucarbou/AsyncTCP.git           AsyncTCP  && \
#v2.10.8     git clone --depth 1 https://github.com/ayushsharma82/ESPAsyncWebServer.git  ESPAsyncWebServer && \
    git clone --depth 1 https://github.com/ESP32Async/ESPAsyncWebServer.git  ESPAsyncWebServer && \
    git clone --depth 1 https://github.com/bblanchon/ArduinoJson.git        async-mqtt-client && \
    git clone --depth 1 https://github.com/marvinroger/async-mqtt-client.git        ArduinoJson && \
    rm -rf AsyncTCP/.git ESPAsyncWebServer/.git ArduinoJson/.git
ENTRYPOINT ["/usr/local/bin/platformio"]
DOCKER
  chmod -R a+rwX "$ROOT/docker"
  sudo docker build -t "$IMG" "$ROOT/docker"
fi

######################## 2.  clone exactly once if no local repo exists #
# --repo=/some/path   → accept either the repo root **or** the parent dir
if [[ -n $REPO_OVERRIDE ]]; then
    [[ -d $REPO_OVERRIDE/.git ]] && SRC=$REPO_OVERRIDE \
        || SRC=$REPO_OVERRIDE/UltraWiFiDuck
else
    SRC=$HERE/UltraWiFiDuck            # sibling of the script
    [[ -d $SRC/.git ]] || SRC=$ROOT/src
fi

# ───────── 1. local repo already there → leave it alone ─────────
if [[ -d $SRC/.git ]]; then
    echo "✔  re-using existing repo at $SRC (no network needed)"
    # OPTIONAL: make it writable for every user only the *first* time
    [[ ! -f "$SRC/.duck_perms_fixed" ]] && \
        chmod -R a+rwX "$SRC" && touch "$SRC/.duck_perms_fixed"
else
# ───────── 2. repo missing → clone once (needs Internet) ─────────
    echo "⇣  cloning UltraWiFiDuck into $SRC"
    git clone --recursive --depth 1 \
        https://github.com/EmileSpecialProducts/UltraWiFiDuck.git "$SRC"
    chmod -R a+rwX "$SRC"        # keep Windows-friendly permissions
fi

# make repo tree world-writable # stric restrictions
chmod -R a+rwX "$SRC"

######################## 3.  US-only patch +
# restore media-key header+disable post-build script+ restore CONSUMER_CONTROL pragmas
LOCDIR=$SRC/src/locale; mkdir -p "$LOCDIR"
cat >"$LOCDIR/local_KeyBoard_US.h" <<'HDR'
#include "duckscript.hpp"
#include <USBHIDConsumerControl.h>
static const UnicodeToKeyCode_t Keyboard_US[] = {{0,0,0}};
#define Keyboard_US_INT Keyboard_US
#define Keyboard_BG     Keyboard_US
#define Keyboard_DE     Keyboard_US
#define Keyboard_French Keyboard_US
#define Keyboard_NONE   Keyboard_US
HDR
for f in local_KeyBoard_BG.h local_KeyBoard_DE.h local_Keyboard_French.h \
         local_KeyBoard_US_INT.h local_KeyBoard_NONE.h; do
    ln -sf local_KeyBoard_US.h "$LOCDIR/$f"
done

PATCH=$SRC/src/Local_KeyBoard.h
sed -i 's#locale\\#locale/#g'  "$PATCH"
sed -Ei '/locale\/local_.*_US\.h/!s/^/\/\/ /' "$PATCH"

FIXTAG='// UWD_CONSUMER_KEYS_PATCH'
PATCH_C='
#include <USBHIDConsumerControl.h>                 // added by build script
#ifndef CONSUMER_CONTROL_RECORD
  #define CONSUMER_CONTROL_RECORD       0x0B0A
#endif
#ifndef CONSUMER_CONTROL_FAST_FORWARD
  #define CONSUMER_CONTROL_FAST_FORWARD 0x0B0E
#endif
#ifndef CONSUMER_CONTROL_REWIND
  #define CONSUMER_CONTROL_REWIND       0x0B0F
#endif
#ifndef CONSUMER_CONTROL_EJECT
  #define CONSUMER_CONTROL_EJECT        0x0B1C
#endif   // ------- end consumer-key patch
'

CPP="$SRC/src/duckscript.cpp"
if ! grep -q "$FIXTAG" "$CPP"; then
  awk -v tag="$FIXTAG" -v ins="$PATCH_C" '
    NR==1 {print; next}                         # keep first line as is
    /^#include/ && !p { print ins; p=1 }        # inject once after includes
    {print}
  ' "$CPP" > "$CPP.tmp" && mv "$CPP.tmp" "$CPP"
fi


######################## 4.  Docker Run with exact specific board
docker run --rm -u $(id -u):$(id -g) \
  -v "$SRC":/w \
  -w /w $IMG run -e esp32-s2-kaluga-4MB

# docker run --rm --user pio \
#   -v "$SRC":/w \
#   -w /w "$IMG" run -e esp32-s2-kaluga-4MB

OUT=$SRC/.pio/build/esp32-s2-kaluga-4MB
chmod -R a+r "$OUT"
for f in bootloader.bin partitions.bin firmware.bin; do [[ -f $OUT/$f ]]; done

######################## 5.  Port auto-detect
[[ $SERIAL =~ tty ]] || for d in /dev/ttyACM* /dev/ttyUSB*; do [[ -r $d ]] && SERIAL=$d && break; done


######################## 6. Flash   #########################################
FLASH_OK=1
python -m esptool --chip esp32s2 --port "$SERIAL" erase_flash || FLASH_OK=0
python -m esptool --chip esp32s2 --port "$SERIAL" write_flash \
      0x1000 $OUT/bootloader.bin 0x8000 $OUT/partitions.bin 0x10000 $OUT/firmware.bin || FLASH_OK=0
[[ $FLASH_OK == 1 ]] && echo "✅  flash OK" || echo "⚠️  flash skipped/failed"

########################### 7- Save image prompt ############################
# docker run --rm --entrypoint bash uwd_pio_offline:latest -c \
#   "pio pkg list --global | grep -E 'espressif32|framework|toolchain-xtensa.*s2' && \
#    ls ~/.platformio/platforms/espressif32"

docker run --rm --entrypoint bash $IMG -c \
  "pio pkg list --global | grep -E 'framework-arduino|toolchain-xtensa-esp32s2' && \
   echo '----------------' && ls ~/.platformio/platforms/espressif32 | head"

docker run --rm $IMG pkg list --global

read -rp "Export Docker image for offline use? [y/N] " save
if [[ ${save,,} == y ]]; then
  FILE=$HERE/build_duck-$(date +%Y%m%d-%H%M%S).tar.gz
  docker save $IMG | gzip >"$FILE" && chmod a+r "$FILE"
  echo "Image saved as $FILE"
fi

exit $FLASH_OK
