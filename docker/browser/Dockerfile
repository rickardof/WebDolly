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
    locales \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Generate it_IT locale so xdotool and Chromium report coherent locale data
# matching the user's actual geolocation (Italy).
RUN sed -i '/it_IT.UTF-8/s/^# //' /etc/locale.gen && locale-gen

# Timezone: ENV alone sets $TZ in child processes, but libc only honours it
# if there is no /etc/localtime symlink. Intl.DateTimeFormat inside Chromium
# reads the symlink first, so without this it falls back to "UTC" regardless
# of the ENV value. Set both so JS, libc and HTTP Date headers agree.
RUN ln -sf /usr/share/zoneinfo/Europe/Rome /etc/localtime \
    && echo "Europe/Rome" > /etc/timezone

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
ENV LANG=it_IT.UTF-8
ENV LC_ALL=it_IT.UTF-8
ENV TZ=Europe/Rome

EXPOSE 9222 5900

ENTRYPOINT ["/entrypoint.sh"]
