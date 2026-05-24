#!/bin/bash
# Register a source/provider in Cost Management
set -euo pipefail

# Configuration
CLUSTER_ID="${1:-nisecluster01}"
SOURCE_NAME="${2:-nise-cluster-01}"
NAMESPACE="${NAMESPACE:-cost-onprem}"
GATEWAY_URL="https://cost-onprem-gateway-cost-onprem.apps.snotp701.rhsovcloud.test"

# Keycloak configuration
KEYCLOAK_URL="${KEYCLOAK_URL:-https://keycloak-keycloak.apps.snotp701.rhsovcloud.test}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-kubernetes}"
KEYCLOAK_CLIENT_ID="${KEYCLOAK_CLIENT_ID:-cost-management-operator}"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Cost Management Source Registration ===${NC}\n"
echo -e "Cluster ID: ${BLUE}$CLUSTER_ID${NC}"
echo -e "Source Name: ${BLUE}$SOURCE_NAME${NC}"
echo ""

# Check if jq is available
if ! command -v jq &> /dev/null; then
    echo -e "${RED}Error: jq is not installed${NC}"
    echo "Please install jq: sudo dnf install jq"
    exit 1
fi

# Step 1: Get JWT token
echo -e "${YELLOW}Step 1: Getting JWT token from Keycloak...${NC}"

KEYCLOAK_CLIENT_SECRET="${KEYCLOAK_CLIENT_SECRET:-}"
if [ -z "$KEYCLOAK_CLIENT_SECRET" ] && command -v kubectl &> /dev/null; then
    echo -e "  Retrieving client secret from Kubernetes..."
    KEYCLOAK_CLIENT_SECRET=$(kubectl get secret keycloak-client-secret-cost-management-operator -n keycloak -o jsonpath='{.data.CLIENT_SECRET}' 2>/dev/null | base64 -d || echo "")
fi

if [ -z "$KEYCLOAK_CLIENT_SECRET" ]; then
    echo -e "${RED}Error: KEYCLOAK_CLIENT_SECRET not set${NC}"
    echo ""
    echo "Please set the Keycloak client secret:"
    echo "  export KEYCLOAK_CLIENT_SECRET=\"<secret>\""
    echo ""
    echo "To get the secret, run:"
    echo "  kubectl get secret keycloak-client-secret-cost-management-operator -n keycloak -o jsonpath='{.data.CLIENT_SECRET}' | base64 -d"
    exit 1
fi

TOKEN_RESPONSE=$(curl -sk -X POST \
    "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=client_credentials" \
    -d "client_id=${KEYCLOAK_CLIENT_ID}" \
    -d "client_secret=${KEYCLOAK_CLIENT_SECRET}" \
    --max-time 30)

JWT_TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.access_token // empty')

if [ -z "$JWT_TOKEN" ] || [ "$JWT_TOKEN" = "null" ]; then
    echo -e "${RED}Error: Failed to get JWT token${NC}"
    echo "Response: $TOKEN_RESPONSE"
    exit 1
fi

echo -e "${GREEN}✓ JWT token obtained${NC}\n"

# Step 2: Check if source already exists
echo -e "${YELLOW}Step 2: Checking for existing sources...${NC}"

EXISTING_SOURCES=$(curl -sk \
    "${GATEWAY_URL}/api/cost-management/v1/sources" \
    -H "Authorization: Bearer $JWT_TOKEN" \
    -H "Content-Type: application/json" \
    --max-time 30)

# Check if source with this cluster_id already exists
EXISTING_SOURCE_ID=$(echo "$EXISTING_SOURCES" | jq -r ".data[] | select(.source_ref==\"$CLUSTER_ID\") | .id // empty")

if [ -n "$EXISTING_SOURCE_ID" ]; then
    EXISTING_SOURCE_NAME=$(echo "$EXISTING_SOURCES" | jq -r ".data[] | select(.id==$EXISTING_SOURCE_ID) | .name")
    echo -e "${YELLOW}⚠ Source already exists!${NC}"
    echo -e "  Source ID: $EXISTING_SOURCE_ID"
    echo -e "  Source Name: $EXISTING_SOURCE_NAME"
    echo -e "  Cluster ID: $CLUSTER_ID"
    echo ""
    read -p "Delete and recreate? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}  Deleting existing source...${NC}"
        DELETE_RESPONSE=$(curl -sk -X DELETE \
            "${GATEWAY_URL}/api/cost-management/v1/sources/${EXISTING_SOURCE_ID}" \
            -H "Authorization: Bearer $JWT_TOKEN" \
            --max-time 30)
        echo -e "${GREEN}  ✓ Deleted${NC}\n"
        sleep 2
    else
        echo -e "${BLUE}Keeping existing source. Exiting.${NC}"
        exit 0
    fi
fi

# Step 3: Get source type ID
echo -e "${YELLOW}Step 3: Getting OpenShift source type ID...${NC}"

SOURCE_TYPES=$(curl -sk \
    "${GATEWAY_URL}/api/cost-management/v1/source_types" \
    -H "Authorization: Bearer $JWT_TOKEN" \
    -H "Content-Type: application/json" \
    --max-time 30)

OCP_TYPE_ID=$(echo "$SOURCE_TYPES" | jq -r '.data[] | select(.name=="openshift") | .id // empty')

if [ -z "$OCP_TYPE_ID" ] || [ "$OCP_TYPE_ID" = "null" ]; then
    echo -e "${RED}Error: OpenShift source type not found${NC}"
    echo "Available source types:"
    echo "$SOURCE_TYPES" | jq -r '.data[] | "  - \(.name) (id: \(.id))"'
    exit 1
fi

echo -e "${GREEN}✓ OpenShift source type ID: $OCP_TYPE_ID${NC}\n"

# Step 4: Get application type ID (optional)
echo -e "${YELLOW}Step 4: Getting Cost Management application type ID...${NC}"

APP_TYPES=$(curl -sk \
    "${GATEWAY_URL}/api/cost-management/v1/application_types" \
    -H "Authorization: Bearer $JWT_TOKEN" \
    -H "Content-Type: application/json" \
    --max-time 30)

COST_MGMT_APP_ID=$(echo "$APP_TYPES" | jq -r '.data[] | select(.name=="/insights/platform/cost-management") | .id // empty')

if [ -n "$COST_MGMT_APP_ID" ] && [ "$COST_MGMT_APP_ID" != "null" ]; then
    echo -e "${GREEN}✓ Application type ID: $COST_MGMT_APP_ID${NC}\n"
else
    echo -e "${YELLOW}⚠ Application type not found (optional)${NC}\n"
fi

# Step 5: Create source
echo -e "${YELLOW}Step 5: Creating source...${NC}"

CREATE_PAYLOAD=$(jq -n \
    --arg name "$SOURCE_NAME" \
    --arg type_id "$OCP_TYPE_ID" \
    --arg ref "$CLUSTER_ID" \
    '{
        name: $name,
        source_type_id: ($type_id | tonumber),
        source_ref: $ref
    }')

echo -e "  Payload: $CREATE_PAYLOAD"

CREATE_RESPONSE=$(curl -sk -w "\n__HTTP_CODE__:%{http_code}" -X POST \
    "${GATEWAY_URL}/api/cost-management/v1/sources" \
    -H "Authorization: Bearer $JWT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$CREATE_PAYLOAD" \
    --max-time 60)

# Parse HTTP code
if echo "$CREATE_RESPONSE" | grep -q "__HTTP_CODE__:"; then
    BODY=$(echo "$CREATE_RESPONSE" | sed 's/__HTTP_CODE__:.*//')
    HTTP_CODE=$(echo "$CREATE_RESPONSE" | grep -o "__HTTP_CODE__:[0-9]*" | cut -d: -f2)
else
    BODY="$CREATE_RESPONSE"
    HTTP_CODE="unknown"
fi

if [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "201" ]; then
    echo -e "${RED}✗ Failed to create source (HTTP $HTTP_CODE)${NC}"
    echo "Response: $BODY"
    exit 1
fi

SOURCE_ID=$(echo "$BODY" | jq -r '.id // empty')

if [ -z "$SOURCE_ID" ] || [ "$SOURCE_ID" = "null" ]; then
    echo -e "${RED}✗ Source created but no ID returned${NC}"
    echo "Response: $BODY"
    exit 1
fi

echo -e "${GREEN}✓ Source created successfully${NC}"
echo -e "  Source ID: $SOURCE_ID"
echo -e "  Source Name: $SOURCE_NAME"
echo -e "  Cluster ID: $CLUSTER_ID"
echo ""

# Step 6: Create application (optional)
if [ -n "$COST_MGMT_APP_ID" ] && [ "$COST_MGMT_APP_ID" != "null" ]; then
    echo -e "${YELLOW}Step 6: Creating application association...${NC}"

    APP_PAYLOAD=$(jq -n \
        --arg source_id "$SOURCE_ID" \
        --arg app_type_id "$COST_MGMT_APP_ID" \
        '{
            source_id: ($source_id | tonumber),
            application_type_id: ($app_type_id | tonumber)
        }')

    APP_RESPONSE=$(curl -sk -X POST \
        "${GATEWAY_URL}/api/cost-management/v1/applications" \
        -H "Authorization: Bearer $JWT_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$APP_PAYLOAD" \
        --max-time 30)

    APP_ID=$(echo "$APP_RESPONSE" | jq -r '.id // empty')

    if [ -n "$APP_ID" ] && [ "$APP_ID" != "null" ]; then
        echo -e "${GREEN}✓ Application created (ID: $APP_ID)${NC}\n"
    else
        echo -e "${YELLOW}⚠ Application creation failed (not critical)${NC}\n"
    fi
else
    echo -e "${YELLOW}Step 6: Skipping application creation${NC}\n"
fi

# Step 7: Verify provider was created in database
echo -e "${YELLOW}Step 7: Verifying provider in database...${NC}"

if command -v kubectl &> /dev/null; then
    DB_POD=$(kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/component=database -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

    if [ -n "$DB_POD" ]; then
        # Wait a few seconds for Kafka message to be processed
        echo -e "  Waiting for Kafka message processing..."
        sleep 5

        PROVIDER_CHECK=$(kubectl exec -n "$NAMESPACE" "$DB_POD" -- psql -U koku_user -d costonprem_koku -t -c \
            "SELECT COUNT(*) FROM api_provider p
             LEFT JOIN api_providerauthentication a ON p.authentication_id = a.id
             WHERE a.credentials->>'cluster_id' = '$CLUSTER_ID'
                OR p.additional_context->>'cluster_id' = '$CLUSTER_ID';" 2>/dev/null | tr -d ' ')

        if [ "$PROVIDER_CHECK" = "1" ]; then
            echo -e "${GREEN}✓ Provider created in database${NC}"
        else
            echo -e "${YELLOW}⚠ Provider not yet in database (may take a few seconds)${NC}"
            echo -e "  Kafka listener processes source creation asynchronously"
        fi
    else
        echo -e "${YELLOW}⚠ Database pod not found, skipping verification${NC}"
    fi
else
    echo -e "${YELLOW}⚠ kubectl not available, skipping verification${NC}"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✓ Source registration complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "You can now upload data for cluster: $CLUSTER_ID"
echo ""
echo "Next steps:"
echo "  1. Upload data: ./prepare_and_upload.sh"
echo "  2. Check listener logs: kubectl logs -n $NAMESPACE -l app.kubernetes.io/component=listener --tail=50"
echo "  3. Check processor logs: kubectl logs -n $NAMESPACE -l app.kubernetes.io/component=cost-processor --tail=50"
echo ""
echo "To verify the source:"
echo "  curl -sk -H \"Authorization: Bearer \$JWT_TOKEN\" \\"
echo "    \"$GATEWAY_URL/api/cost-management/v1/sources\" | jq '.data[] | select(.id==$SOURCE_ID)'"
