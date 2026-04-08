#!/bin/bash
# =============================================================
# setup-env.sh — Install host dependencies for Yocto build
# Run once on a fresh Ubuntu 24.04 VM
# =============================================================
set -e

echo ">>> Installing Yocto build dependencies..."
sudo apt update
sudo apt install -y \
    gawk wget git diffstat unzip texinfo gcc build-essential chrpath socat \
    cpio python3 python3-pip python3-pexpect xz-utils debianutils iputils-ping \
    python3-git python3-jinja2 libegl1 libsdl1.2-dev pylint xterm \
    python3-subunit mesa-common-dev zstd liblz4-tool file locales

echo ">>> Setting locale..."
sudo locale-gen en_US.UTF-8

echo ">>> Setting up swap (8GB)..."
if [ ! -f /swapfile ]; then
    sudo fallocate -l 8G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab
fi

echo ">>> Environment ready."
