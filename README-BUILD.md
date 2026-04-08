# Home Monitor — Yocto Build Guide

## Architecture

```
[RPi Zero 2W + PiHut ZeroCam]  --RTSP-->  [RPi 4B Server]  <--HTTP--  [Mobile Phone]
      (Camera Node)                        (This Image)                 (Web Browser)
```

## What This Image Includes

- **Video**: ffmpeg, GStreamer, v4l-utils for RTSP receive/record/transcode
- **Web UI**: nginx + Flask app accessible from mobile browser
- **Storage**: Auto-records in 15-min segments, auto-cleans after 7 days
- **Network**: WiFi, SSH, NetworkManager
- **Init**: systemd (monitor-server starts on boot)

## GCP VM Requirements

- **CPU**: 4+ vCPUs (8 recommended for faster build)
- **RAM**: 8GB minimum (16GB recommended)
- **Disk**: 100GB+ SSD
- **OS**: Ubuntu 24.04 LTS

## Step-by-Step Build Instructions

### 1. Transfer files to GCP VM

From your Windows machine:
```bash
# Using gcloud SCP (adjust VM name/zone):
gcloud compute scp --recurse C:\Users\vinun\yocto\scripts yocto:~/yocto/scripts
gcloud compute scp --recurse C:\Users\vinun\yocto\config yocto:~/yocto/config
gcloud compute scp --recurse C:\Users\vinun\yocto\meta-home-monitor yocto:~/yocto/meta-home-monitor
```

Or SSH in and use git/manual copy.

### 2. SSH into VM and run setup

```bash
gcloud compute ssh yocto

# Make script executable
chmod +x ~/yocto/scripts/setup-and-build.sh

# Run the setup (clones repos, copies configs)
~/yocto/scripts/setup-and-build.sh
```

### 3. Build the image

```bash
cd ~/yocto
source poky/oe-init-build-env build
bitbake home-monitor-image
```

**Build time**: ~2-4 hours on a 4-core VM (first build). Subsequent builds are much faster due to sstate cache.

### 4. Find the output image

```bash
ls ~/yocto/build/tmp/deploy/images/raspberrypi4-64/home-monitor-image-raspberrypi4-64.wic.bz2
```

### 5. Flash to SD card

Transfer the `.wic.bz2` to your local machine, then:

```bash
# Linux/Mac:
bzcat home-monitor-image-raspberrypi4-64.wic.bz2 | sudo dd of=/dev/sdX bs=4M status=progress

# Windows: Use balenaEtcher — it handles .wic.bz2 directly
```

### 6. First boot on RPi 4B

1. Insert SD card into RPi 4B
2. Connect Ethernet (or configure WiFi via serial console)
3. Boot up — monitor-server starts automatically
4. Find the IP: check your router or use `nmap -sn 192.168.1.0/24`
5. Open `http://<rpi-ip>` on your phone

### 7. Configure cameras

Edit camera IPs via the API:
```bash
curl -X POST http://<rpi-ip>/api/cameras \
  -H "Content-Type: application/json" \
  -d '{"name": "Front Door", "rtsp_url": "rtsp://192.168.1.101:8554/stream"}'
```

Or SSH in and edit `/opt/monitor/cameras.json`.

## Tuning local.conf for your VM

Edit `config/local.conf` before building:

```
# Match to your VM's core count:
BB_NUMBER_THREADS = "8"    # number of CPU cores
PARALLEL_MAKE = "-j 8"     # same as above
```

## Next Steps (Phase 2)

- Build a separate Yocto image for RPi Zero 2W (camera node)
- Add motion detection (using motion or OpenCV)
- Add HTTPS/TLS for secure mobile access
- Add USB external storage auto-mount for more recording space
- Add push notifications via Telegram/webhook
