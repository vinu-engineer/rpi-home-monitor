SUMMARY = "Web server packages for Home Monitor"
DESCRIPTION = "Nginx, Flask, and Python dependencies for the web dashboard."
LICENSE = "MIT"

inherit packagegroup

RDEPENDS:${PN} = " \
    nginx \
    python3 \
    python3-flask \
    python3-jinja2 \
    python3-requests \
    python3-bcrypt \
    python3-pip \
    "
