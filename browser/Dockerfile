FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    xvfb \
    xdotool \
    scrot \
    x11vnc \
    fonts-liberation \
    dbus-x11 \
    procps \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --break-system-packages --no-cache-dir websocket-client

RUN useradd -m -s /bin/bash browser && mkdir -p /data && chown browser:browser /data

COPY entrypoint.sh /entrypoint.sh
COPY cdp.py /usr/local/bin/cdp.py
RUN chmod +x /entrypoint.sh /usr/local/bin/cdp.py

# Enterprise policy, not just --disable-features flags: a form field in
# this browser got silently overwritten by the browser itself a few
# seconds after being set (e.g. a real saved name appearing in an
# unrelated "full_name" field with no corresponding JS-visible write),
# which --disable-features=Autofill alone did not stop. Policy-level
# settings are enforced above the individual feature-flag layer and are
# the standard, documented way to fully lock down autofill/password
# manager/sync for an automated browser instance.
RUN mkdir -p /etc/chromium/policies/managed
COPY policy.json /etc/chromium/policies/managed/fara-policy.json

USER browser
ENV HOME=/home/browser
ENV DISPLAY=:99
# Debian's default locale is POSIX/C, under which xdotool can't interpret
# multi-byte UTF-8 (any accented/non-ASCII character) and aborts entirely
# instead of typing it. C.utf8 is a built-in glibc locale, no locale-gen
# needed.
ENV LANG=C.utf8
ENV LC_ALL=C.utf8

EXPOSE 9222 5900

ENTRYPOINT ["/entrypoint.sh"]
