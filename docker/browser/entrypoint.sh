#!/bin/bash
set -e

# A prior Xvfb crash on this same display can leave /tmp/.X11-unix/X99 and
# /tmp/.X99-lock behind; a fresh Xvfb then refuses to start ("Server is
# already active for display 99"), which cascades into x11vnc and
# chromium both failing with "Missing X server" and the whole container
# exiting. /tmp is the container's own ephemeral layer (not a mounted
# volume), so this is always safe to clear before starting.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &

for i in $(seq 1 50); do
  if DISPLAY=:99 xdotool getdisplaygeometry >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

x11vnc -display :99 -forever -shared -nopw -quiet -rfbport 5900 &

# A recreated container has a new hostname, and Chromium refuses a profile
# whose SingletonLock names another host ("profile appears to be in use by
# another Chromium process") -- the browser never starts and CDP stays dead.
# Only one Chromium ever runs in this container, so a lock found at startup
# is always stale.
rm -f /home/browser/chromium-profile/SingletonLock \
      /home/browser/chromium-profile/SingletonSocket \
      /home/browser/chromium-profile/SingletonCookie

exec chromium \
  --no-sandbox \
  --disable-gpu \
  --kiosk \
  --window-size=1440,900 \
  --window-position=0,0 \
  --force-device-scale-factor=1 \
  --remote-debugging-port=9222 \
  --remote-allow-origins=* \
  --no-first-run \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-translate \
  --disable-features=Translate,Autofill \
  --user-data-dir=/home/browser/chromium-profile \
  "file:///data/test.html"
