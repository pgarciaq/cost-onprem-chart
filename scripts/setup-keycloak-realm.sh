#!/bin/bash
set -euo pipefail

################################################################################
# Keycloak Realm Setup for Cost Management On-Premise
#
# Creates/updates the 'cost-management' realm with the correct client and
# protocol mappers for JWT authentication. Idempotent — safe to run multiple
# times (e.g., after a Keycloak pod restart that loses in-memory state).
#
# Usage:
#   ./scripts/setup-keycloak-realm.sh [OPTIONS]
#
# Options:
#   --namespace NS         Keycloak namespace (default: keycloak)
#   --realm NAME           Realm name (default: cost-management)
#   --client-id NAME       Client ID (default: cost-management-operator)
#   --org-id VALUE         org_id claim value (default: 1234567)
#   --account-number VALUE account_number claim value (default: 10001)
#   --verbose              Enable verbose output
#   --help                 Display this help message
#
# Environment Variables:
#   KEYCLOAK_NS              Keycloak namespace (default: keycloak)
#   KEYCLOAK_REALM           Realm name (default: cost-management)
#   KEYCLOAK_CLIENT_ID       Client ID (default: cost-management-operator)
#   KEYCLOAK_ADMIN_PASSWORD  Admin password (default: admin123)
#   KEYCLOAK_SECRET_NAME     K8s secret with CLIENT_SECRET (default: keycloak-client-secret-cost-management-operator)
#   ORG_ID                   org_id claim value (default: 1234567)
#   ACCOUNT_NUMBER           account_number claim value (default: 10001)
################################################################################

KEYCLOAK_NS="${KEYCLOAK_NS:-keycloak}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-cost-management}"
KEYCLOAK_CLIENT_ID="${KEYCLOAK_CLIENT_ID:-cost-management-operator}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-admin123}"
KEYCLOAK_SECRET_NAME="${KEYCLOAK_SECRET_NAME:-keycloak-client-secret-cost-management-operator}"
ORG_ID="${ORG_ID:-1234567}"
ACCOUNT_NUMBER="${ACCOUNT_NUMBER:-10001}"
VERBOSE="${VERBOSE:-false}"

PORT_FORWARD_PID=""
LOCAL_PORT=18080

log() { echo "[$(date +%H:%M:%S)] $*"; }
log_verbose() { [[ "$VERBOSE" == "true" ]] && log "$*" || true; }
error() { echo "[$(date +%H:%M:%S)] ERROR: $*" >&2; }

cleanup() {
    if [ -n "$PORT_FORWARD_PID" ]; then
        kill "$PORT_FORWARD_PID" 2>/dev/null || true
        wait "$PORT_FORWARD_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

usage() {
    sed -n '3,/^##/p' "$0" | grep '^#' | sed 's/^# \?//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --namespace)     KEYCLOAK_NS="$2"; shift 2 ;;
        --realm)         KEYCLOAK_REALM="$2"; shift 2 ;;
        --client-id)     KEYCLOAK_CLIENT_ID="$2"; shift 2 ;;
        --org-id)        ORG_ID="$2"; shift 2 ;;
        --account-number) ACCOUNT_NUMBER="$2"; shift 2 ;;
        --verbose)       VERBOSE="true"; shift ;;
        --help|-h)       usage ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

# Get client secret from k8s secret
get_client_secret() {
    if kubectl get secret "$KEYCLOAK_SECRET_NAME" -n "$KEYCLOAK_NS" &>/dev/null; then
        kubectl get secret "$KEYCLOAK_SECRET_NAME" -n "$KEYCLOAK_NS" \
            -o jsonpath='{.data.CLIENT_SECRET}' | base64 -d
    else
        error "Secret $KEYCLOAK_SECRET_NAME not found in namespace $KEYCLOAK_NS"
        exit 1
    fi
}

start_port_forward() {
    # Kill any existing port-forward on this port
    pkill -f "port-forward.*keycloak.*${LOCAL_PORT}" 2>/dev/null || true
    sleep 1

    kubectl port-forward -n "$KEYCLOAK_NS" svc/keycloak-service "${LOCAL_PORT}:8080" &>/dev/null &
    PORT_FORWARD_PID=$!
    sleep 2

    if ! kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
        error "Failed to start port-forward to Keycloak"
        exit 1
    fi
    log_verbose "Port-forward active: localhost:${LOCAL_PORT} -> keycloak-service:8080"
}

get_admin_token() {
    local token_response
    token_response=$(curl -s -X POST "http://localhost:${LOCAL_PORT}/realms/master/protocol/openid-connect/token" \
        -d "client_id=admin-cli" \
        -d "username=admin" \
        -d "password=${KEYCLOAK_ADMIN_PASSWORD}" \
        -d "grant_type=password")

    local token
    token=$(echo "$token_response" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("access_token",""))' 2>/dev/null || echo "")

    if [ -z "$token" ]; then
        error "Failed to get admin token. Response: $token_response"
        exit 1
    fi
    echo "$token"
}

create_realm() {
    local token="$1"
    local KC="http://localhost:${LOCAL_PORT}"

    # Check if realm exists
    local status
    status=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer ${token}" \
        "${KC}/admin/realms/${KEYCLOAK_REALM}")

    if [ "$status" = "200" ]; then
        log_verbose "Realm '${KEYCLOAK_REALM}' already exists"
        return 0
    fi

    log "Creating realm '${KEYCLOAK_REALM}'..."
    curl -sf -X POST "${KC}/admin/realms" \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" \
        -d "{\"realm\":\"${KEYCLOAK_REALM}\",\"enabled\":true}" >/dev/null

    log "Realm '${KEYCLOAK_REALM}' created"
}

create_or_update_client() {
    local token="$1"
    local client_secret="$2"
    local KC="http://localhost:${LOCAL_PORT}"

    # Check if client exists
    local clients_json
    clients_json=$(curl -s -H "Authorization: Bearer ${token}" \
        "${KC}/admin/realms/${KEYCLOAK_REALM}/clients?clientId=${KEYCLOAK_CLIENT_ID}")

    local client_uuid
    client_uuid=$(echo "$clients_json" | python3 -c 'import json,sys; c=json.load(sys.stdin); print(c[0]["id"] if c else "")' 2>/dev/null || echo "")

    local client_payload
    client_payload=$(cat <<EOF
{
    "clientId": "${KEYCLOAK_CLIENT_ID}",
    "enabled": true,
    "serviceAccountsEnabled": true,
    "publicClient": false,
    "clientAuthenticatorType": "client-secret",
    "secret": "${client_secret}",
    "protocol": "openid-connect",
    "directAccessGrantsEnabled": true
}
EOF
)

    if [ -n "$client_uuid" ]; then
        log_verbose "Updating existing client '${KEYCLOAK_CLIENT_ID}' (uuid: ${client_uuid})"
        curl -sf -X PUT "${KC}/admin/realms/${KEYCLOAK_REALM}/clients/${client_uuid}" \
            -H "Authorization: Bearer ${token}" \
            -H "Content-Type: application/json" \
            -d "$client_payload" >/dev/null
    else
        log "Creating client '${KEYCLOAK_CLIENT_ID}'..."
        curl -sf -X POST "${KC}/admin/realms/${KEYCLOAK_REALM}/clients" \
            -H "Authorization: Bearer ${token}" \
            -H "Content-Type: application/json" \
            -d "$client_payload" >/dev/null

        # Re-fetch UUID for mapper operations
        clients_json=$(curl -s -H "Authorization: Bearer ${token}" \
            "${KC}/admin/realms/${KEYCLOAK_REALM}/clients?clientId=${KEYCLOAK_CLIENT_ID}")
        client_uuid=$(echo "$clients_json" | python3 -c 'import json,sys; c=json.load(sys.stdin); print(c[0]["id"] if c else "")' 2>/dev/null || echo "")
    fi

    echo "$client_uuid"
}

ensure_mapper() {
    local token="$1"
    local client_uuid="$2"
    local mapper_name="$3"
    local claim_name="$4"
    local claim_value="$5"
    local json_type="${6:-String}"
    local KC="http://localhost:${LOCAL_PORT}"

    # Get existing mappers
    local mappers_json
    mappers_json=$(curl -s -H "Authorization: Bearer ${token}" \
        "${KC}/admin/realms/${KEYCLOAK_REALM}/clients/${client_uuid}/protocol-mappers/models")

    # Check if mapper exists and has correct value
    local existing
    existing=$(echo "$mappers_json" | python3 -c "
import json, sys
mappers = json.load(sys.stdin)
for m in mappers:
    if m.get('name') == '${mapper_name}':
        cfg = m.get('config', {})
        if cfg.get('claim.value') == '${claim_value}':
            print('OK')
        else:
            print(m['id'])
        sys.exit()
print('MISSING')
" 2>/dev/null || echo "MISSING")

    if [ "$existing" = "OK" ]; then
        log_verbose "Mapper '${mapper_name}' already correct (${claim_name}=${claim_value})"
        return 0
    fi

    local mapper_payload
    mapper_payload=$(cat <<EOF
{
    "name": "${mapper_name}",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-hardcoded-claim-mapper",
    "config": {
        "claim.name": "${claim_name}",
        "claim.value": "${claim_value}",
        "id.token.claim": "true",
        "access.token.claim": "true",
        "jsonType.label": "${json_type}"
    }
}
EOF
)

    if [ "$existing" = "MISSING" ]; then
        log_verbose "Creating mapper '${mapper_name}' (${claim_name}=${claim_value})"
        curl -sf -X POST \
            "${KC}/admin/realms/${KEYCLOAK_REALM}/clients/${client_uuid}/protocol-mappers/models" \
            -H "Authorization: Bearer ${token}" \
            -H "Content-Type: application/json" \
            -d "$mapper_payload" >/dev/null
    else
        # existing contains the mapper UUID — update it
        log_verbose "Updating mapper '${mapper_name}' (${claim_name}=${claim_value})"
        mapper_payload=$(echo "$mapper_payload" | python3 -c "
import json, sys
d = json.load(sys.stdin)
d['id'] = '${existing}'
print(json.dumps(d))
")
        curl -sf -X PUT \
            "${KC}/admin/realms/${KEYCLOAK_REALM}/clients/${client_uuid}/protocol-mappers/models/${existing}" \
            -H "Authorization: Bearer ${token}" \
            -H "Content-Type: application/json" \
            -d "$mapper_payload" >/dev/null
    fi
}

verify_token() {
    local client_secret="$1"
    local KC="http://localhost:${LOCAL_PORT}"

    local token_response
    token_response=$(curl -s -X POST \
        "${KC}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token" \
        -d "client_id=${KEYCLOAK_CLIENT_ID}" \
        -d "client_secret=${client_secret}" \
        -d "grant_type=client_credentials")

    local has_token
    has_token=$(echo "$token_response" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("OK" if "access_token" in d else d.get("error_description", d.get("error","UNKNOWN")))' 2>/dev/null || echo "PARSE_ERROR")

    if [ "$has_token" != "OK" ]; then
        error "Token verification failed: $has_token"
        return 1
    fi
    log_verbose "Token verification passed"
}

# =============================================================================
# Main
# =============================================================================

log "Setting up Keycloak realm '${KEYCLOAK_REALM}' in namespace '${KEYCLOAK_NS}'..."

# Verify Keycloak pod is running (try both common label patterns)
if ! kubectl get pods -n "$KEYCLOAK_NS" -l app.kubernetes.io/name=keycloak --field-selector=status.phase=Running -o name 2>/dev/null | grep -q pod; then
    if ! kubectl get pods -n "$KEYCLOAK_NS" -l app=keycloak --field-selector=status.phase=Running -o name 2>/dev/null | grep -q pod; then
        error "No running Keycloak pod found in namespace '$KEYCLOAK_NS'"
        exit 1
    fi
fi

CLIENT_SECRET=$(get_client_secret)
log_verbose "Client secret retrieved from k8s secret"

start_port_forward

ADMIN_TOKEN=$(get_admin_token)
log_verbose "Admin token acquired"

create_realm "$ADMIN_TOKEN"

CLIENT_UUID=$(create_or_update_client "$ADMIN_TOKEN" "$CLIENT_SECRET")
if [ -z "$CLIENT_UUID" ]; then
    error "Failed to get client UUID after creation"
    exit 1
fi
log_verbose "Client UUID: $CLIENT_UUID"

ensure_mapper "$ADMIN_TOKEN" "$CLIENT_UUID" "org-id-mapper" "org_id" "$ORG_ID" "String"
ensure_mapper "$ADMIN_TOKEN" "$CLIENT_UUID" "account-number-mapper" "account_number" "$ACCOUNT_NUMBER" "String"
ensure_mapper "$ADMIN_TOKEN" "$CLIENT_UUID" "is-org-admin-mapper" "is_org_admin" "true" "Boolean"

verify_token "$CLIENT_SECRET"

log "Keycloak realm '${KEYCLOAK_REALM}' is ready (client: ${KEYCLOAK_CLIENT_ID}, org_id: ${ORG_ID})"
