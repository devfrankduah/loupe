#!/usr/bin/env bash
# Records docs/demo.gif: the README's quickstart scan of the blender test page,
# rendered on the local backend with no account, so it uses no Bright Data
# traffic. Quotes are cut to 80 characters so the output fits the 150 by 24
# window.
#
# Needs asciinema and agg (brew install asciinema agg) and the virtual
# environment from the README with the render extra. Run it from anywhere in
# the repo:
#
#   scripts/record-demo.sh
#
# The Bright Data values are removed from the recorded command. If .env holds
# them, they're loaded only so the recording can be checked for the token and
# any part of the endpoint before it's converted; the script stops if it finds
# one. The .cast file is deleted either way and never leaves the temporary
# folder.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
# shellcheck disable=SC1091
source .venv/bin/activate
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

work="$(mktemp -d)"
cast="$work/demo.cast"
cleanup() {
  rm -f -- "$cast"
  rmdir -- "$work"
}
trap cleanup EXIT

asciinema rec --quiet --headless --return --window-size 150x24 --capture-env TERM \
  --command 'printf "\$ loupe --render --quote-chars 80 https://devfrankduah.github.io/loupe/test-page/\n"; env -u BRIGHTDATA_API_TOKEN -u BRIGHTDATA_BROWSER_WSS loupe --render --quote-chars 80 https://devfrankduah.github.io/loupe/test-page/' \
  "$cast"

# Stops here, through set -e, if the recording holds a secret.
speed="$(python -I scripts/check_demo_cast.py "$cast")"

agg --speed "$speed" --idle-time-limit 120 --last-frame-duration 8 --font-size 16 --theme monokai \
  "$cast" docs/demo.gif
echo "wrote docs/demo.gif at ${speed}x"
