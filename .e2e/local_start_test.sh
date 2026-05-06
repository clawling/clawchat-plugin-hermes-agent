SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"

if [ ! -f "$ENV_FILE" ]; then
    echo "missing env file: $ENV_FILE (expected JWT=...)" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if [ -z "${JWT:-}" ]; then
    echo "JWT not set in $ENV_FILE" >&2
    exit 1
fi

case "$JWT" in
    Bearer\ *) AUTH_HEADER="$JWT" ;;
    *)         AUTH_HEADER="Bearer $JWT" ;;
esac

RESPONSE=$(curl -sS --location --request POST 'http://company.newbaselab.com:19001/v1/agents/connect-codes' \
    --header 'x-device-id: apifox' \
    --header "Authorization: $AUTH_HEADER")

echo "connect-codes response: $RESPONSE"

CODE=$(echo "$RESPONSE" | python3 -c "import sys, json; d = json.load(sys.stdin); print(d.get('data', {}).get('code') or d.get('code', ''))")

if [ -z "$CODE" ]; then
    echo "failed to obtain connect code" >&2
    exit 1
fi

echo "connect code: $CODE"

if [ ! -d ./.e2e/tmp/hermes_data_base ]; then
    cat >&2 <<'EOF'
missing baseline Hermes data dir: ./.e2e/tmp/hermes_data_base

Each test run is freshly seeded by copying ./.e2e/tmp/hermes_data_base ->
./.e2e/tmp/hermes_data, so the baseline must exist first. Bootstrap it
once by running Hermes interactively against an empty dir, completing
the first-run setup (model / API keys), then exiting:

    mkdir -p ./.e2e/tmp/hermes_data_base
    docker run -it --rm -v ./.e2e/tmp/hermes_data_base:/opt/data \
        nousresearch/hermes-agent chat

After it exits, ./.e2e/tmp/hermes_data_base will hold the initialized
config Hermes needs and you can re-run this script.
EOF
    exit 1
fi

rm -rf ./.e2e/tmp/hermes_data
cp -r ./.e2e/tmp/hermes_data_base ./.e2e/tmp/hermes_data
docker run -it --rm \
    -v ./.e2e/tmp/hermes_data:/opt/data \
    -v ./.e2e/dev_install.md:/opt/dev_install.md:ro \
    nousresearch/hermes-agent chat -q "Here is the connect <CODE>: $CODE; Install and configure clawchat by following the instructions in the local file (use tool:'execute_code' to read it with Python): /opt/dev_install.md"
#docker run -it --rm  -v ./.e2e/tmp/hermes_data:/opt/data nousresearch/hermes-agent gateway run
#docker run -it -v ./.e2e/tmp/hermes_data:/opt/data nousresearch/hermes-agent gateway run
