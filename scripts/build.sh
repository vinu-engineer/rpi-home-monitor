#!/bin/bash
# =============================================================
# build.sh — Clone layers, configure, and build
# =============================================================
set -e

YOCTO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE="scarthgap"
NCPU=$(nproc)

echo ">>> Working in: $YOCTO_DIR"
echo ">>> Release: $RELEASE"
echo ">>> CPUs: $NCPU"

# --- Clone Yocto layers (HTTPS — git:// often blocked on cloud VMs) ---
clone_layer() {
    local url=$1 dir=$2 branch=$3
    if [ ! -d "$dir/.git" ]; then
        echo ">>> Cloning $dir ..."
        git clone "$url" "$dir"
    fi
    cd "$dir"
    git checkout "$branch" 2>/dev/null || git checkout -b "$branch" "origin/$branch"
    cd "$YOCTO_DIR"
}

clone_layer "https://git.yoctoproject.org/poky" "$YOCTO_DIR/poky" "$RELEASE"
clone_layer "https://git.yoctoproject.org/meta-raspberrypi" "$YOCTO_DIR/meta-raspberrypi" "$RELEASE"
clone_layer "https://github.com/openembedded/meta-openembedded.git" "$YOCTO_DIR/meta-openembedded" "$RELEASE"

# --- Init build environment ---
echo ">>> Initializing build environment..."
source "$YOCTO_DIR/poky/oe-init-build-env" "$YOCTO_DIR/build"

# --- Copy our configs ---
echo ">>> Copying configuration..."
cp "$YOCTO_DIR/config/local.conf" "$YOCTO_DIR/build/conf/local.conf"
cp "$YOCTO_DIR/config/bblayers.conf" "$YOCTO_DIR/build/conf/bblayers.conf"

# --- Patch local.conf with actual CPU count ---
sed -i "s/^BB_NUMBER_THREADS.*/BB_NUMBER_THREADS = \"$NCPU\"/" "$YOCTO_DIR/build/conf/local.conf"
sed -i "s/^PARALLEL_MAKE.*/PARALLEL_MAKE = \"-j $NCPU\"/" "$YOCTO_DIR/build/conf/local.conf"

echo ""
echo "============================================"
echo " Environment ready. Starting build..."
echo " Image: home-monitor-image"
echo " Machine: raspberrypi4-64"
echo " Cores: $NCPU"
echo "============================================"
echo ""

bitbake home-monitor-image
