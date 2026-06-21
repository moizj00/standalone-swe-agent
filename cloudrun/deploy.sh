#!/usr/bin/env bash
# Build the container with Cloud Build and deploy it to Cloud Run.
#
# Required:
#   GCP_PROJECT          your GCP project id
#   ANTHROPIC_API_KEY    Anthropic API key for the agent backend
#
# Optional:
#   GCP_REGION           default us-central1
#   SERVICE_NAME         default swe-agent
#   ANTHROPIC_MODEL      default claude-sonnet-4-6
#   APPROVAL             default read-only (read-only|auto-accept|yolo)
#   SWE_AGENT_SERVER_TOKEN  optional bearer; recommended for belt+suspenders auth
#   ALLOW_UNAUTH=1       skip --no-allow-unauthenticated (NOT recommended)
#   MAX_INSTANCES        default 5; set to 1 for in-process session stickiness
#
# Usage:
#   GCP_PROJECT=my-proj ANTHROPIC_API_KEY=sk-ant-... bash cloudrun/deploy.sh
set -euo pipefail

: "${GCP_PROJECT:?GCP_PROJECT is required}"
: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY is required}"

REGION="${GCP_REGION:-us-central1}"
SERVICE="${SERVICE_NAME:-swe-agent}"
MODEL="${ANTHROPIC_MODEL:-claude-sonnet-4-6}"
APPROVAL="${APPROVAL:-read-only}"
MAX_INSTANCES="${MAX_INSTANCES:-5}"
IMAGE="gcr.io/${GCP_PROJECT}/${SERVICE}:$(date -u +%Y%m%d-%H%M%S)"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "→ Building image: ${IMAGE}"
gcloud builds submit "${REPO_ROOT}" \
  --project "${GCP_PROJECT}" \
  --tag "${IMAGE}"

# Compose the env-var list. ANTHROPIC_API_KEY ships as an env var; for stronger
# isolation, point it at a Secret Manager secret instead (--set-secrets) — left
# to the operator since secret-manager setup is out of scope here.
ENV_VARS=(
  "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
  "ANTHROPIC_MODEL=${MODEL}"
  "SWE_AGENT_BACKEND=anthropic"
  "SWE_AGENT_TRUST_NETWORK=1"
)
[[ -n "${SWE_AGENT_SERVER_TOKEN:-}" ]] && ENV_VARS+=("SWE_AGENT_SERVER_TOKEN=${SWE_AGENT_SERVER_TOKEN}")

# Join env vars with ^|^ delimiter so values containing commas survive.
joined=$(IFS='|'; echo "${ENV_VARS[*]}")

AUTH_FLAG="--no-allow-unauthenticated"
[[ "${ALLOW_UNAUTH:-0}" == "1" ]] && AUTH_FLAG="--allow-unauthenticated"

echo "→ Deploying service: ${SERVICE} in ${REGION} (model=${MODEL}, approval=${APPROVAL}, auth=${AUTH_FLAG})"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --project "${GCP_PROJECT}" \
  --region "${REGION}" \
  --platform managed \
  ${AUTH_FLAG} \
  --memory 1Gi \
  --cpu 1 \
  --timeout 3600 \
  --max-instances "${MAX_INSTANCES}" \
  --concurrency 80 \
  --port 8080 \
  --command "python" \
  --args "-m,swe_agent.server,--host,0.0.0.0,--cwd,/tmp/workspace,--model,${MODEL},--approval,${APPROVAL},--no-preflight,--no-persist" \
  --set-env-vars "^|^${joined}"

URL=$(gcloud run services describe "${SERVICE}" --project "${GCP_PROJECT}" --region "${REGION}" --format='value(status.url)')
echo
echo "✓ Deployed: ${URL}"
echo
if [[ "${AUTH_FLAG}" == "--no-allow-unauthenticated" ]]; then
  cat <<EOF
Authenticated requests (operators with run.invoker on this service):

  TOKEN=\$(gcloud auth print-identity-token)
  curl -H "Authorization: Bearer \${TOKEN}" ${URL}/api/health
  curl -H "Authorization: Bearer \${TOKEN}" ${URL}/api/tools | jq '.tools|length'

For browser dashboard access, front the service with IAP (recommended) or
use an OAuth2 proxy. Cloud Run identity tokens are short-lived (~1h).
EOF
else
  cat <<EOF
Service is allow-unauthenticated. ANYONE on the internet can hit /api/chat;
set SWE_AGENT_SERVER_TOKEN at minimum and consider IAP regardless.

  curl ${URL}/api/health
EOF
fi
