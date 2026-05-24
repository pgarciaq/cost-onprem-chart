#!/bin/bash
# Script to prepare NISE data tarball and upload to Cost Management
set -euo pipefail

# Configuration
NISE_DATA_DIR="/home/pgarciaq/dev/koku/nise/generatedata/nisecluster01/20251201-20260101"
# Correct URL: Gateway routes /api/ingress/* to the ingress service
UPLOAD_URL="https://cost-onprem-gateway-cost-onprem.apps.snotp701.rhsovcloud.test/api/ingress/v1/upload"
OUTPUT_TAR="nisecluster01-cost-mgmt.tar.gz"
NAMESPACE="${NAMESPACE:-cost-onprem}"

# Upload timeout (10 minutes for 17MB file)
UPLOAD_TIMEOUT=600

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Cost Management Data Upload Tool ===${NC}\n"

# Step 1: Create tarball
echo -e "${YELLOW}Step 1: Creating tarball from NISE data...${NC}"

if [ ! -d "$NISE_DATA_DIR" ]; then
    echo -e "${RED}Error: NISE data directory not found: $NISE_DATA_DIR${NC}"
    exit 1
fi

cd "$NISE_DATA_DIR"

# Check if manifest exists
if [ ! -f "manifest.json" ]; then
    echo -e "${RED}Error: manifest.json not found in $NISE_DATA_DIR${NC}"
    exit 1
fi

# Create tarball with all CSV files and manifest
tar -czf "/tmp/$OUTPUT_TAR" *.csv manifest.json

echo -e "${GREEN}✓ Tarball created: /tmp/$OUTPUT_TAR${NC}"
echo -e "  Size: $(ls -lh /tmp/$OUTPUT_TAR | awk '{print $5}')"
echo -e "  Files included: $(tar -tzf /tmp/$OUTPUT_TAR | wc -l) files"
echo ""

# Step 2: Test endpoint and check authentication
echo -e "${YELLOW}Step 2: Testing upload endpoint and checking authentication...${NC}"
echo -e "  URL: $UPLOAD_URL"

# Test if endpoint is reachable
echo -e "  Testing connectivity..."
TEST_RESPONSE=$(curl -sk -I "$UPLOAD_URL" --max-time 10 2>&1 || echo "FAILED")

if echo "$TEST_RESPONSE" | grep -q "FAILED\|Could not resolve\|Connection refused\|Failed to connect"; then
    echo -e "${RED}✗ Upload endpoint is not reachable${NC}"
    echo -e "  Response: $TEST_RESPONSE"
    echo ""
    echo "Please check:"
    echo "  1. Route exists: kubectl get route cost-onprem-api -n $NAMESPACE"
    echo "  2. Gateway is running: kubectl get pods -n $NAMESPACE -l app.kubernetes.io/component=gateway"
    exit 1
fi

# Check if authentication is required
AUTH_REQUIRED="false"
if echo "$TEST_RESPONSE" | grep -q "401\|Unauthorized"; then
    AUTH_REQUIRED="true"
    echo -e "${BLUE}✓ Endpoint reachable - Authentication REQUIRED (401 Unauthorized)${NC}"
elif echo "$TEST_RESPONSE" | grep -q "405\|Method Not Allowed"; then
    # 405 means endpoint exists but GET not allowed (expected for upload endpoint)
    echo -e "${GREEN}✓ Endpoint reachable - Authentication may not be required${NC}"
elif echo "$TEST_RESPONSE" | grep -q "503\|Service Unavailable"; then
    echo -e "${RED}✗ Service unavailable (503)${NC}"
    echo "Gateway pods may not be ready. Check: kubectl get pods -n $NAMESPACE -l app.kubernetes.io/component=gateway"
    exit 1
else
    echo -e "${GREEN}✓ Endpoint reachable${NC}"
fi
echo ""

# Step 3: Get JWT token if needed
JWT_TOKEN=""
if [ "$AUTH_REQUIRED" = "true" ]; then
    echo -e "${YELLOW}Step 3: Getting JWT token from Keycloak...${NC}"

    # Keycloak configuration for your cluster
    KEYCLOAK_URL="${KEYCLOAK_URL:-https://keycloak-keycloak.apps.snotp701.rhsovcloud.test}"
    KEYCLOAK_REALM="${KEYCLOAK_REALM:-kubernetes}"
    KEYCLOAK_CLIENT_ID="${KEYCLOAK_CLIENT_ID:-cost-management-operator}"
    KEYCLOAK_CLIENT_SECRET="${KEYCLOAK_CLIENT_SECRET:-}"

    # Get client secret from Kubernetes if not provided
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
        echo ""
        echo "Or use the UI client:"
        echo "  kubectl get secret cost-onprem-ui-oauth-client -n cost-onprem -o jsonpath='{.data.client-secret}' | base64 -d"
        exit 1
    fi

    # Get token
    echo -e "  Keycloak URL: $KEYCLOAK_URL"
    echo -e "  Realm: $KEYCLOAK_REALM"
    echo -e "  Client: $KEYCLOAK_CLIENT_ID"
    echo -e "  Requesting token..."

    TOKEN_RESPONSE=$(curl -sk -X POST \
        "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "grant_type=client_credentials" \
        -d "client_id=${KEYCLOAK_CLIENT_ID}" \
        -d "client_secret=${KEYCLOAK_CLIENT_SECRET}" \
        --max-time 30)

    JWT_TOKEN=$(echo "$TOKEN_RESPONSE" | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)

    if [ -z "$JWT_TOKEN" ]; then
        echo -e "${RED}Error: Failed to get JWT token${NC}"
        echo "Response: $TOKEN_RESPONSE"
        exit 1
    fi

    echo -e "${GREEN}✓ JWT token obtained${NC}"
    echo -e "  Token (first 50 chars): ${JWT_TOKEN:0:50}..."
    echo ""
else
    echo -e "${YELLOW}Step 3: Skipping authentication (not required)${NC}\n"
fi

# Step 4: Upload to Cost Management
STEP_NUM=4
if [ "$AUTH_REQUIRED" = "true" ]; then
    STEP_NUM=4
else
    STEP_NUM=3
fi

echo -e "${YELLOW}Step ${STEP_NUM}: Uploading to Cost Management...${NC}"
echo -e "  URL: $UPLOAD_URL"
echo -e "  File: /tmp/$OUTPUT_TAR ($(ls -lh /tmp/$OUTPUT_TAR | awk '{print $5}'))"
echo -e "  Timeout: ${UPLOAD_TIMEOUT}s"

if [ -n "$JWT_TOKEN" ]; then
    echo -e "  Using authentication: YES"
    echo -e "  Uploading (this may take 1-2 minutes)..."

    UPLOAD_RESPONSE=$(curl -sk \
        -w "\n__HTTP_CODE__:%{http_code}" \
        --max-time "$UPLOAD_TIMEOUT" \
        --progress-bar \
        -X POST \
        -H "Authorization: Bearer $JWT_TOKEN" \
        -F "file=@/tmp/$OUTPUT_TAR;type=application/vnd.redhat.hccm.tar+tgz" \
        "$UPLOAD_URL" 2>&1)
else
    echo -e "  Using authentication: NO"
    echo -e "  Uploading (this may take 1-2 minutes)..."

    UPLOAD_RESPONSE=$(curl -sk \
        -w "\n__HTTP_CODE__:%{http_code}" \
        --max-time "$UPLOAD_TIMEOUT" \
        --progress-bar \
        -X POST \
        -F "file=@/tmp/$OUTPUT_TAR;type=application/vnd.redhat.hccm.tar+tgz" \
        "$UPLOAD_URL" 2>&1)
fi

# Parse HTTP code
if echo "$UPLOAD_RESPONSE" | grep -q "__HTTP_CODE__:"; then
    BODY=$(echo "$UPLOAD_RESPONSE" | sed 's/__HTTP_CODE__:.*//')
    HTTP_CODE=$(echo "$UPLOAD_RESPONSE" | grep -o "__HTTP_CODE__:[0-9]*" | cut -d: -f2)
else
    BODY="$UPLOAD_RESPONSE"
    HTTP_CODE="unknown"
fi

echo ""
echo -e "${GREEN}Upload Response:${NC}"
echo -e "  HTTP Status: $HTTP_CODE"
echo -e "  Body: $BODY"

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "202" ]; then
    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}✓ Upload successful!${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""
    echo "Next steps:"
    echo ""
    echo "1. Check listener logs (wait ~10-30 seconds):"
    echo "   kubectl logs -n $NAMESPACE -l app.kubernetes.io/component=listener --tail=50 -f"
    echo ""
    echo "2. Check MASU processor logs (~1-2 minutes):"
    echo "   kubectl logs -n $NAMESPACE -l app.kubernetes.io/component=cost-processor --tail=50 -f"
    echo ""
    echo "3. Query database for manifest (~30 seconds after upload):"
    echo "   DB_POD=\$(kubectl get pods -n $NAMESPACE -l app.kubernetes.io/component=database -o jsonpath='{.items[0].metadata.name}')"
    echo "   kubectl exec -n $NAMESPACE \$DB_POD -- psql -U koku_user -d costonprem_koku -c \\"
    echo "     \"SELECT id, cluster_id, num_total_files, num_processed_files, creation_datetime \\"
    echo "     \"FROM reporting_common_costusagereportmanifest \\"
    echo "     \"WHERE cluster_id='nisecluster01' \\"
    echo "     \"ORDER BY creation_datetime DESC LIMIT 5;\""
    echo ""
    echo "4. Check file processing status (~2-5 minutes):"
    echo "   kubectl exec -n $NAMESPACE \$DB_POD -- psql -U koku_user -d costonprem_koku -c \\"
    echo "     \"SELECT rf.report_name, rf.status, rf.completed_datetime \\"
    echo "     \"FROM reporting_common_costusagereportmanifest m \\"
    echo "     \"JOIN reporting_common_costusagereportstatus rf ON m.id = rf.manifest_id \\"
    echo "     \"WHERE m.cluster_id='nisecluster01' \\"
    echo "     \"ORDER BY rf.completed_datetime DESC;\""
    echo ""
    echo "Status codes: 0=Pending, 1=Success, 2=Failed"
else
    echo ""
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}✗ Upload failed!${NC}"
    echo -e "${RED}========================================${NC}"
    echo ""
    if [ "$HTTP_CODE" = "401" ]; then
        echo "Authentication error (401 Unauthorized)"
        echo "  - JWT token may be invalid or expired"
        echo "  - Try setting KEYCLOAK_CLIENT_SECRET and running again"
        echo ""
    elif [ "$HTTP_CODE" = "503" ]; then
        echo "Service unavailable (503)"
        echo "  - Gateway or ingress pods may not be ready"
        echo "  - Check: kubectl get pods -n $NAMESPACE -l app.kubernetes.io/component=gateway"
        echo "  - Check: kubectl get pods -n $NAMESPACE -l app.kubernetes.io/component=ingress"
        echo ""
    elif [ "$HTTP_CODE" = "28" ]; then
        echo "Upload timeout"
        echo "  - File upload took longer than ${UPLOAD_TIMEOUT}s"
        echo "  - Try increasing UPLOAD_TIMEOUT or check network connectivity"
        echo ""
    fi
    echo "Troubleshooting:"
    echo "  - Check gateway pods: kubectl get pods -n $NAMESPACE -l app.kubernetes.io/component=gateway"
    echo "  - Check gateway logs: kubectl logs -n $NAMESPACE -l app.kubernetes.io/component=gateway --tail=50"
    echo "  - Check ingress logs: kubectl logs -n $NAMESPACE -l app.kubernetes.io/component=ingress --tail=50"
    exit 1
fi
