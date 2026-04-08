#!/bin/bash
# =============================================================
# Yocto Build Script for RPi 4B Home Monitoring System
# Run this on the GCP VM (Ubuntu 24.04)
# =============================================================
set -e

YOCTO_DIR="$HOME/yocto"
RELEASE="scarthgap"   # Yocto 5.0 LTS — latest stable

echo "============================================"
echo " Step 1: Clone Poky (if not already done)"
echo "============================================"
if [ ! -d "$YOCTO_DIR/poky/.git" ]; then
    git clone git://git.yoctoproject.org/poky "$YOCTO_DIR/poky"
fi
cd "$YOCTO_DIR/poky"
git checkout "$RELEASE"

echo "============================================"
echo " Step 2: Clone meta-raspberrypi"
echo "============================================"
if [ ! -d "$YOCTO_DIR/meta-raspberrypi/.git" ]; then
    git clone git://git.yoctoproject.org/meta-raspberrypi "$YOCTO_DIR/meta-raspberrypi"
fi
cd "$YOCTO_DIR/meta-raspberrypi"
git checkout "$RELEASE"

echo "============================================"
echo " Step 3: Clone meta-openembedded"
echo "============================================"
if [ ! -d "$YOCTO_DIR/meta-openembedded/.git" ]; then
    git clone git://git.openembedded.org/meta-openembedded "$YOCTO_DIR/meta-openembedded"
fi
cd "$YOCTO_DIR/meta-openembedded"
git checkout "$RELEASE"

echo "============================================"
echo " Step 4: Copy custom layer meta-home-monitor"
echo "============================================"
if [ -d "$YOCTO_DIR/meta-home-monitor" ]; then
    echo "meta-home-monitor already present"
else
    echo "ERROR: Copy meta-home-monitor to $YOCTO_DIR/ before running this script"
    exit 1
fi

echo "============================================"
echo " Step 5: Initialize build environment"
echo "============================================"
cd "$YOCTO_DIR"
source poky/oe-init-build-env build

echo "============================================"
echo " Step 6: Copy configuration files"
echo "============================================"
cp "$YOCTO_DIR/config/local.conf" "$YOCTO_DIR/build/conf/local.conf"
cp "$YOCTO_DIR/config/bblayers.conf" "$YOCTO_DIR/build/conf/bblayers.conf"

echo "============================================"
echo " Step 7: Build the image"
echo "============================================"
echo ""
echo "Ready to build! Run:"
echo "  cd $YOCTO_DIR && source poky/oe-init-build-env build"
echo "  bitbake home-monitor-image"
echo ""
echo "The image will be at:"
echo "  $YOCTO_DIR/build/tmp/deploy/images/raspberrypi4-64/"
echo ""

read -p "Start build now? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    bitbake home-monitor-image
fi
