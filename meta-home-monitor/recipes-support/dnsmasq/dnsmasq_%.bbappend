# Disable standalone dnsmasq service.
# NetworkManager launches its own internal dnsmasq instance
# when ipv4.method=shared is used (WiFi hotspot with DHCP).
# The standalone daemon would conflict by binding the same port.

SYSTEMD_AUTO_ENABLE = "disable"
