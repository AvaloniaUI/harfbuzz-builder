# Linux build image: Clang 20 on Ubuntu 20.04 (glibc 2.31), with meson and ninja.
FROM ubuntu:20.04
ENV DEBIAN_FRONTEND=noninteractive
COPY requirements.txt /tmp/requirements.txt
RUN apt-get update \
 && apt-get install -y --no-install-recommends software-properties-common gnupg ca-certificates curl git python3 \
    python3-pip \
 && curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key | apt-key add - \
 && echo "deb http://apt.llvm.org/focal/ llvm-toolchain-focal-20 main" > /etc/apt/sources.list.d/llvm.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends clang-20 lld-20 llvm-20 libc6-dev binutils \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m pip install --no-cache-dir --upgrade "pip<25" \
 && python3 -m pip install --no-cache-dir -r /tmp/requirements.txt
ENV HARFBUZZ_BUILDER_CC=clang-20 HARFBUZZ_BUILDER_CXX=clang++-20 HOME=/tmp
