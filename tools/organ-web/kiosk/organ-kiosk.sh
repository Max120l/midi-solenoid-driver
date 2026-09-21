#!/bin/bash
# Full-screen browser on the organ's own screen, started by the desktop
# session (see organ-kiosk.desktop). Waits for the front desk to answer, so
# a slow boot shows the page and not an error, then hands over to Chromium
# in kiosk mode: no address bar, no tabs, no "restore session?" bubble.
#
# The organ_web service must be enabled; this only shows its page.

URL="${ORGAN_URL:-http://localhost:8080/}"
PAGE="${URL}?kiosk=1"          # the page remembers it is the case screen: the screen time-out defaults on

until curl -fs "${URL}api/state" >/dev/null 2>&1; do
    sleep 1
done

BROWSER=$(command -v chromium-browser || command -v chromium)

exec "$BROWSER" \
    --kiosk "$PAGE" \
    --disk-cache-dir=/dev/shm/organ-kiosk-cache \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    --check-for-update-interval=31536000 \
    --ozone-platform-hint=auto \
    --touch-events=enabled \
    --overscroll-history-navigation=0 \
    --password-store=basic
