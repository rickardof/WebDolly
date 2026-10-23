#!/bin/bash
set -e

# Timezone belt-and-suspenders: image already sets ENV TZ=Europe/Rome AND
# /etc/localtime symlink, but keep an export here so a `docker run -e TZ=...`
# override at container start is picked up by this shell and its children.
export TZ="${TZ:-Europe/Rome}"

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

# Italian keyboard layout so accented chars (à è é ì ò ù) have NATIVE X
# keysyms. Without this, xdotool must dynamically map each accented char
# to an unused keysym slot, which races the keypress and drops the char
# in Chromium ~70% of the time. Measured on Xvfb us layout: "La città è
# perché così però" came out as "La citt  perch cos però" randomly.
# Retry in a short loop — a bare setxkbmap right after Xvfb starts can
# silently no-op if the XKB extension isn't fully initialised yet.
for i in $(seq 1 10); do
  if DISPLAY=:99 setxkbmap it 2>/dev/null \
      && DISPLAY=:99 setxkbmap -query 2>/dev/null | grep -q "layout:\s*it"; then
    echo "entrypoint: xkb layout set to it"
    break
  fi
  sleep 0.2
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
  --enable-unsafe-swiftshader \
  --use-gl=angle \
  --use-angle=swiftshader \
  --enable-webgl \
  --ignore-gpu-blocklist \
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
  --disable-blink-features=AutomationControlled \
  --lang=it-IT \
  --accept-lang=it-IT,it;q=0.9,en;q=0.8 \
  --user-data-dir=/home/browser/chromium-profile \
  "file:///data/test.html"
