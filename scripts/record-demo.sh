#!/usr/bin/env bash
# Records docs/demo.gif: one rendered scan of the blender test page through
# Bright Data's Browser API, which uses a few KB of Browser API traffic.
# Quotes are cut to 80 characters so all 20 lines fit the 150 by 22 window.
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

asciinema rec --quiet --headless --return --window-size 150x22 --capture-env TERM \
  --command 'printf "\$ loupe --render --quote-chars 80 https://devfrankduah.github.io/loupe/test-page/\n"; loupe --render --quote-chars 80 https://devfrankduah.github.io/loupe/test-page/' \
  "$cast"

# Stops here, through set -e, if the recording holds a secret.
speed="$(python -I scripts/check_demo_cast.py "$cast")"

agg --speed "$speed" --idle-time-limit 120 --last-frame-duration 8 --font-size 16 --theme monokai \
  "$cast" docs/demo.gif
echo "wrote docs/demo.gif at ${speed}x"
