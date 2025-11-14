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
