# Deploying `standalone-swe-agent` on Google Cloud Run

This deploys a single container that serves both the React dashboard and the
Python HTTP/SSE agent, driven by the **Anthropic API** as the LLM backend
(Ollama can't run serverlessly). Sessions are ephemeral; the agent's workspace
is `/tmp/workspace`.

## Prerequisites

```bash
gcloud auth login
gcloud config set project <YOUR_GCP_PROJECT>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
```

You also need an Anthropic API key (`ANTHROPIC_API_KEY`, starts with `sk-ant-`).

## Quick deploy

```bash
export GCP_PROJECT=my-gcp-project
export ANTHROPIC_API_KEY=sk-ant-...
bash cloudrun/deploy.sh
```

This builds the image with Cloud Build, then `gcloud run deploy`s it with these
defaults:

| Setting | Value | Notes |
|---|---|---|
| region | `us-central1` | Override with `GCP_REGION=…`. |
| auth | `--no-allow-unauthenticated` | Cloud Run IAM gates inbound; see "Auth model" below. |
| memory / CPU | 1 GiB / 1 vCPU | Plenty for the agent loop; bump for heavy subagent fan-out. |
| timeout | 3600 s | A single agent turn can run many tool calls. |
| max instances | 5 | Sessions live in-process — see "Scaling" below. |
| LLM | `claude-sonnet-4-6` | Override with `ANTHROPIC_MODEL=…`. |
| Approval mode | `read-only` | All mutations + shell are blocked; safe by default. Override with `APPROVAL=auto-accept` or `yolo`. |

`deploy.sh` prints the service URL and a working curl example at the end.

## Auth model

The dashboard's existing token-injection lives in `web/server.ts` (the local
Node proxy); it never reached the browser. In Cloud Run there's no Node layer,
so the recommended outer boundary is **Cloud Run IAM**:

- `--no-allow-unauthenticated` (deploy.sh default) means only callers with
  `roles/run.invoker` on this service can hit any endpoint.
- The Python server runs with `SWE_AGENT_TRUST_NETWORK=1`, a positive opt-in
  that asserts an outer auth layer is in place. With that env var set, the
  server's "non-loopback bind requires a token" gate relaxes — but the
  "non-read-only without token" gate is still enforced, so YOLO/auto-accept
  modes still require `SWE_AGENT_SERVER_TOKEN`.

Three usage patterns from there:

1. **CLI / curl access** with an identity token:
   ```bash
   TOKEN=$(gcloud auth print-identity-token)
   curl -H "Authorization: Bearer $TOKEN" $URL/api/health
   ```
2. **Browser dashboard access**: put **[IAP](https://cloud.google.com/iap/docs/enabling-cloud-run)**
   in front of the service. IAP transparently authenticates the user via Google
   sign-in and forwards an identity token to the backend.
3. **Belt + suspenders**: also set `SWE_AGENT_SERVER_TOKEN` so a misconfigured
   IAM grant alone can't reach `/api/chat`. Operators inject it via curl,
   `-H "Authorization: Bearer $TOKEN_OR_RUN_IDENTITY"` (the server checks for
   the configured token in constant time).

`ALLOW_UNAUTH=1` flips Cloud Run to public; the script will warn loudly. Don't
do this without a token AND outer firewall rules.

## Environment variables

The container reads these (most are set by `deploy.sh`):

| Env var | Purpose | Default |
|---|---|---|
| `PORT` | Bind port. Cloud Run injects 8080. | `8080` |
| `ANTHROPIC_API_KEY` | Required. The agent's LLM credential. | — |
| `ANTHROPIC_MODEL` | Model id (e.g. `claude-sonnet-4-6`). | `claude-sonnet-4-6` |
| `ANTHROPIC_MAX_TOKENS` | Max tokens per turn. | `4096` |
| `SWE_AGENT_BACKEND` | `anthropic`\|`ollama` (overrides model-prefix routing). | `anthropic` |
| `SWE_AGENT_STATIC_DIR` | Built SPA bundle. | `/app/web/dist` |
| `SWE_AGENT_TRUST_NETWORK` | `1` to permit non-loopback bind without a token (assumes outer auth). | `1` |
| `SWE_AGENT_SERVER_TOKEN` | Optional bearer; checked in constant time. | _unset_ |

`--cwd /tmp/workspace`, `--approval read-only`, `--no-preflight`, `--no-persist`
are baked into the Dockerfile CMD; override at deploy time with `--args` if needed.

## Local container smoke test

```bash
docker build -t swe-agent .
docker run --rm -p 8080:8080 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e SWE_AGENT_TRUST_NETWORK=1 \
  swe-agent

# in another terminal:
curl -sf http://127.0.0.1:8080/api/health
curl -sf http://127.0.0.1:8080/api/tools | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["tools"]))'
curl -sf http://127.0.0.1:8080/ | head -c 200   # → SPA index.html
```

The dashboard at `http://127.0.0.1:8080/` works fully because the React app
hits same-origin `/api/*` and the Python server serves both.

## Known limitations

- **Ephemeral filesystem.** Only `/tmp` is writeable. `--cwd /tmp/workspace` is
  empty on each cold start. For a long-lived workspace, mount Cloud Storage
  via FUSE or a GCS bucket and adjust `--cwd`.
- **Session locality.** The agent server keeps sessions in an in-process
  registry. With multiple Cloud Run instances, a follow-up request can land on
  a different instance that doesn't know the session. Mitigations: cap at
  `--max-instances 1` for single-operator use, or enable Cloud Run **session
  affinity** (`--session-affinity`) and rely on the client to send the same
  `session_id` cookie/header.
- **Cold start.** First request after idle pays a ~1-2 s container-spinup cost
  plus a one-shot Anthropic preflight if you don't pass `--no-preflight`.
  The Dockerfile passes `--no-preflight` to keep cold starts cheap; the agent
  still surfaces auth errors on the first real chat turn.
- **Approval modes.** `auto-accept` and `yolo` over HTTP still require
  `SWE_AGENT_SERVER_TOKEN` — `SWE_AGENT_TRUST_NETWORK` does NOT bypass that
  rule. Set the token via env or move it to Secret Manager (`--set-secrets`).
- **No Ollama in Cloud Run.** The container has no Ollama binary or model
  weights. The CLI's local-Ollama path keeps working on developer machines.

## Updating

`deploy.sh` rebuilds and replaces the revision in place. The image tag is
`gcr.io/$GCP_PROJECT/$SERVICE_NAME:<utc-timestamp>` so rollbacks via
`gcloud run services update-traffic --to-revisions=<prior>=100` are
straightforward.
