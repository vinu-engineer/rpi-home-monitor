# Enable nginx to start automatically on boot
SYSTEMD_AUTO_ENABLE = "enable"

# Remove the default server config that conflicts with our monitor.conf
# (default_server listens on port 80, conflicts with our HTTP→HTTPS redirect)
do_install:append() {
    rm -f ${D}${sysconfdir}/nginx/sites-enabled/default_server
    rm -f ${D}${sysconfdir}/nginx/sites-available/default_server
}
