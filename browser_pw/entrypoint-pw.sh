#!/bin/bash
socat TCP-LISTEN:9333,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9222 &
exec /entrypoint.sh
