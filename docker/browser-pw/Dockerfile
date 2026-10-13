# Same browser as fara-browser, plus a socat forwarder so Playwright on the
# host can reach CDP: Chromium binds its debugging port to loopback inside
# the container only (it ignores --remote-debugging-address), so the port
# can't be published directly.
FROM fara-browser
USER root
RUN apt-get update && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint-pw.sh /entrypoint-pw.sh
RUN chmod +x /entrypoint-pw.sh
USER browser
ENTRYPOINT ["/entrypoint-pw.sh"]
