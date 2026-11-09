#!/usr/bin/env bash
# Records docs/demo.gif: one rendered scan of frankduah.me through Bright
# Data's Browser API, which uses about 350 KB of Browser API traffic.
#
# Needs asciinema and agg (brew install asciinema agg), the virtual
# environment from the README with the render extra, and both
# BRIGHTDATA_API_TOKEN and BRIGHTDATA_BROWSER_WSS in .env. Run it from
# anywhere in the repo:
#
#   scripts/record-demo.sh
#
# Before converting, it checks the recording for the token and for any part
# of the endpoint, and stops if it finds one. The .cast file is deleted
# either way and never leaves the temporary folder.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
# shellcheck disable=SC1091
source .venv/bin/activate
set -a
# shellcheck disable=SC1091
source .env
set +a

work="$(mktemp -d)"
cast="$work/demo.cast"
cleanup() {
  rm -f -- "$cast"
  rmdir -- "$work"
}
trap cleanup EXIT

asciinema rec --quiet --headless --return --window-size 150x13 --capture-env TERM \
  --command 'printf "\$ loupe https://frankduah.me --render\n"; loupe https://frankduah.me --render' \
  "$cast"

# Stops here, through set -e, if the recording holds a secret.
speed="$(python -I scripts/check_demo_cast.py "$cast")"

agg --speed "$speed" --idle-time-limit 120 --last-frame-duration 8 --font-size 16 --theme monokai \
  "$cast" docs/demo.gif
echo "wrote docs/demo.gif at ${speed}x"
