#!/bin/sh
# Cleanup old recordings — keep last 7 days
find /opt/monitor/recordings -name "*.mp4" -mtime +7 -delete
find /opt/monitor/snapshots -name "*.jpg" -mtime +3 -delete
