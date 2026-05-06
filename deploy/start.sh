#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="hermes-plugin-test"
POD_NAME="hermes-agent"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180s}"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not found in PATH" >&2
  exit 1
fi

echo "[1/4] Ensuring namespace '${NAMESPACE}' exists..."
kubectl apply -f "${SCRIPT_DIR}/namespace.yaml"

echo "[2/4] Removing previous pod (if any) for a clean launch..."
kubectl delete pod "${POD_NAME}" \
  --namespace "${NAMESPACE}" \
  --ignore-not-found \
  --wait=true \
  --grace-period=0 \
  --force >/dev/null 2>&1 || true

echo "[3/4] Applying pod manifest..."
kubectl apply -f "${SCRIPT_DIR}/pod.yaml"

echo "[4/4] Waiting up to ${WAIT_TIMEOUT} for pod to become Ready..."
kubectl wait \
  --for=condition=Ready \
  "pod/${POD_NAME}" \
  --namespace "${NAMESPACE}" \
  --timeout="${WAIT_TIMEOUT}"

kubectl get pod "${POD_NAME}" --namespace "${NAMESPACE}" -o wide

cat <<EOF

Pod is up. Useful follow-ups:
  kubectl -n ${NAMESPACE} logs -f ${POD_NAME}
  kubectl -n ${NAMESPACE} exec -it ${POD_NAME} -- bash
  kubectl -n ${NAMESPACE} delete pod ${POD_NAME}
EOF
