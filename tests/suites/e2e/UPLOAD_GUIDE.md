# Cost Management Data Upload Guide

This guide shows how to upload NISE-generated data to your local Cost Management instance.

## Overview

- **Upload URL**: `https://cost-onprem-gateway-cost-onprem.apps.snotp701.rhsovcloud.test/api/ingress/v1/upload`
- **Gateway URL**: `https://cost-onprem-gateway-cost-onprem.apps.snotp701.rhsovcloud.test`
- **Cluster ID**: `nisedemo01`
- **Data Period**: 2025-12-01 to 2025-12-31
- **Tarball**: `/tmp/nisedemo01-cost-mgmt.tar.gz` (17MB, 7 CSV files + manifest)
- **Authentication**: **DISABLED** (`INGRESS_AUTH=false`) - No JWT token required

## Option 1: Simple Upload (No Authentication - RECOMMENDED)

Your ingress has authentication disabled, so you can upload directly without Keycloak credentials.

### Automated Script (Easiest)

```bash
./upload_simple.sh
```

### Manual cURL Command

```bash
curl -k -X POST \
  -F "file=@/tmp/nisedemo01-cost-mgmt.tar.gz;type=application/vnd.redhat.hccm.tar+tgz" \
  "https://cost-onprem-gateway-cost-onprem.apps.snotp701.rhsovcloud.test/api/ingress/v1/upload"
```

Expected response (HTTP 200 or 202):
```json
{
  "request_id": "<uuid>",
  "upload": {"request_id": "<uuid>"}
}
```

## Option 2: Authenticated Upload (When INGRESS_AUTH=true)

If you later enable authentication, use one of the available Keycloak clients:

### Available Keycloak Clients

#### Client 1: cost-management-operator (Recommended for API access)
```bash
CLIENT_ID="cost-management-operator"
CLIENT_SECRET="WDx7PZQ08UkLNDMzUpDeQk3za75pcl2k"
```

To retrieve the secret from Kubernetes:
```bash
kubectl get secret keycloak-client-secret-cost-management-operator -n keycloak \
  -o jsonpath='{.data.CLIENT_SECRET}' | base64 -d
```

#### Client 2: cost-management-ui (For UI access)
```bash
CLIENT_ID="cost-management-ui"
CLIENT_SECRET="3JfTjqu1337dZ5IC2NBx2SWHC64pcuPM"
```

To retrieve the secret from Kubernetes:
```bash
kubectl get secret cost-onprem-ui-oauth-client -n cost-onprem \
  -o jsonpath='{.data.client-secret}' | base64 -d
```

### Step 1: Get JWT Token

```bash
# Set Keycloak details
KEYCLOAK_URL="https://keycloak-keycloak.apps.snotp701.rhsovcloud.test"
KEYCLOAK_REALM="cost-onprem"
KEYCLOAK_CLIENT_ID="cost-management-operator"
KEYCLOAK_CLIENT_SECRET="WDx7PZQ08UkLNDMzUpDeQk3za75pcl2k"

# Get the token
JWT_TOKEN=$(curl -sk -X POST \
  "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=${KEYCLOAK_CLIENT_ID}" \
  -d "client_secret=${KEYCLOAK_CLIENT_SECRET}" \
  | jq -r '.access_token')

echo "JWT Token obtained: ${JWT_TOKEN:0:50}..."
```

### Step 2: Upload with Authentication

```bash
curl -k -X POST \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -F "file=@/tmp/nisedemo01-cost-mgmt.tar.gz;type=application/vnd.redhat.hccm.tar+tgz" \
  "https://cost-onprem-gateway-cost-onprem.apps.snotp701.rhsovcloud.test/api/ingress/v1/upload"
```

## Verification Steps

### 1. Check Listener Logs

The Koku listener should consume the Kafka message:

```bash
kubectl logs -n cost-onprem -l app.kubernetes.io/component=listener --tail=100 -f
```

Look for:
- `Received message from platform.upload.announce`
- `Processing OCP data for cluster: nisedemo01`

### 2. Check MASU Processor Logs

MASU processes the CSV files from S3:

```bash
kubectl logs -n cost-onprem -l app.kubernetes.io/component=cost-processor --tail=100 -f
```

Look for:
- `Downloading file from S3`
- `Processing manifest <uuid>`
- `Summary processing complete`

### 3. Query Database for Manifest

Check if the manifest was created:

```bash
# Get database pod
DB_POD=$(kubectl get pods -n cost-onprem -l app.kubernetes.io/component=database -o jsonpath='{.items[0].metadata.name}')

# Query manifest table
kubectl exec -n cost-onprem $DB_POD -- psql -U koku_user -d costonprem_koku -c \
  "SELECT id, cluster_id, num_total_files, num_processed_files, creation_datetime
   FROM reporting_common_costusagereportmanifest
   WHERE cluster_id='nisedemo01'
   ORDER BY creation_datetime DESC
   LIMIT 5;"
```

Expected output:
```
 id | cluster_id | num_total_files | num_processed_files | creation_datetime
----+------------+-----------------+---------------------+-------------------
  1 | nisedemo01 |               7 |                   7 | 2026-02-12 ...
```

### 4. Check File Processing Status

Verify all files were processed successfully:

```bash
kubectl exec -n cost-onprem $DB_POD -- psql -U koku_user -d costonprem_koku -c \
  "SELECT rf.report_name, rf.status, rf.completed_datetime
   FROM reporting_common_costusagereportmanifest m
   JOIN reporting_common_costusagereportstatus rf ON m.id = rf.manifest_id
   WHERE m.cluster_id='nisedemo01'
   ORDER BY rf.completed_datetime DESC;"
```

Status codes:
- `0` = Pending
- `1` = Success
- `2` = Failed

### 5. Check Summary Tables (if applicable)

For pod usage data, check if summary tables were populated:

```bash
# Get tenant schema
SCHEMA=$(kubectl exec -n cost-onprem $DB_POD -- psql -U koku_user -d costonprem_koku -t -c \
  "SELECT c.schema_name
   FROM reporting_common_costusagereportmanifest m
   JOIN api_provider p ON m.provider_id = p.uuid
   JOIN api_customer c ON p.customer_id = c.id
   WHERE m.cluster_id='nisedemo01' LIMIT 1;" | tr -d '[:space:]')

# Query summary table
kubectl exec -n cost-onprem $DB_POD -- psql -U koku_user -d costonprem_koku -c \
  "SELECT COUNT(*) as rows,
          COALESCE(SUM(pod_request_cpu_core_hours), 0) as cpu_hours,
          COALESCE(SUM(pod_request_memory_gigabyte_hours), 0) as mem_gb_hours
   FROM ${SCHEMA}.reporting_ocpusagelineitem_daily_summary
   WHERE cluster_id='nisedemo01';"
```

## Troubleshooting

### Check if authentication is enabled

```bash
kubectl get deployment cost-onprem-ingress -n cost-onprem -o jsonpath='{.spec.template.spec.containers[?(@.name=="ingress")].env[?(@.name=="INGRESS_AUTH")].value}'
```

- `false` = No authentication required
- `true` = JWT token required

### Upload fails with 401 Unauthorized

This means authentication is enabled. You need a JWT token:

- Verify you're using the correct Keycloak client (use `cost-management-operator`)
- Check JWT token is valid and not expired
- Get a fresh token using the commands in Option 2 above

### Upload fails with 503 Service Unavailable

- Ingress pods may not be ready
- Check pod status: `kubectl get pods -n cost-onprem -l app.kubernetes.io/component=ingress`
- Check ingress logs: `kubectl logs -n cost-onprem -l app.kubernetes.io/component=ingress --tail=50`

### Files not processed (status = 0)

- Check MASU logs for errors
- Verify S3 connectivity and credentials
- Check Celery workers are running: `kubectl get pods -n cost-onprem -l app.kubernetes.io/component=cost-worker`

### Summary tables not populated

**Note**: Your data contains storage/volume metrics, not pod CPU/memory usage. Summary tables may not populate fully without pod usage data.

To generate proper pod usage data, run NISE with:

```bash
nise report ocp \
  --ros-ocp-info \
  --static-report-file <your-config.yml> \
  --write-monthly \
  --ocp-cluster-id nisedemo01 \
  --start-date $(date -d "yesterday" +%Y-%m-%d) \
  --end-date $(date +%Y-%m-%d)
```

The `--ros-ocp-info` flag generates both pod_usage and ros_usage files for complete Cost Management + ROS pipeline testing.

## Alternative: Direct S3 Upload (Bypass Ingress)

If ingress upload fails, you can upload directly to S3:

```bash
# Get S3 credentials
S3_ACCESS_KEY=$(kubectl get secret cost-onprem-s3-credentials -n cost-onprem -o jsonpath='{.data.aws-access-key-id}' | base64 -d)
S3_SECRET_KEY=$(kubectl get secret cost-onprem-s3-credentials -n cost-onprem -o jsonpath='{.data.aws-secret-access-key}' | base64 -d)
S3_ENDPOINT=$(kubectl get cm cost-onprem-s3-config -n cost-onprem -o jsonpath='{.data.S3_ENDPOINT}')
S3_BUCKET=$(kubectl get cm cost-onprem-s3-config -n cost-onprem -o jsonpath='{.data.S3_BUCKET}')

# Upload using aws CLI
AWS_ACCESS_KEY_ID=$S3_ACCESS_KEY \
AWS_SECRET_ACCESS_KEY=$S3_SECRET_KEY \
aws s3 cp /tmp/nisedemo01-cost-mgmt.tar.gz \
  s3://${S3_BUCKET}/data/nisedemo01/ \
  --endpoint-url "$S3_ENDPOINT"
```

## Next Steps

After successful upload and processing:

### If Authentication is Disabled (Current Setup)

1. View data in the API:
   ```bash
   curl -k "https://cost-onprem-gateway-cost-onprem.apps.snotp701.rhsovcloud.test/api/cost-management/v1/reports/openshift/costs/"
   ```

2. Check ROS recommendations (if pod usage data was uploaded):
   ```bash
   curl -k "https://cost-onprem-gateway-cost-onprem.apps.snotp701.rhsovcloud.test/api/cost-management/v1/recommendations/openshift"
   ```

### If Authentication is Enabled

1. Get a JWT token first (see Option 2 above)

2. View data in the API:
   ```bash
   curl -k -H "Authorization: Bearer $JWT_TOKEN" \
     "https://cost-onprem-gateway-cost-onprem.apps.snotp701.rhsovcloud.test/api/cost-management/v1/reports/openshift/costs/"
   ```

3. Check ROS recommendations:
   ```bash
   curl -k -H "Authorization: Bearer $JWT_TOKEN" \
     "https://cost-onprem-gateway-cost-onprem.apps.snotp701.rhsovcloud.test/api/cost-management/v1/recommendations/openshift"
   ```
