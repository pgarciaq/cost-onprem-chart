# Cost Management On-Premise — aarch64 SNO Deployment Guide

This document describes how to deploy Cost Management on-premise on an aarch64
Single Node OpenShift (SNO) cluster. It covers building custom arm64 container
images, deploying the Helm chart, generating test data with nise, and running
the test suite.

**Cluster:** SNO 4.21 on `hpe-apollo-cn99xx-16.khw.eng.rdu2.dc.redhat.com` (aarch64, 64 cores, 128 GiB RAM)
**Last deployed:** 2026-04-21
**Helm release:** `cost-onprem` (namespace `cost-onprem`)

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Network Access](#network-access)
- [Step 1: Build aarch64 Container Images](#step-1-build-aarch64-container-images)
- [Step 2: Push Images to Internal Registry](#step-2-push-images-to-internal-registry)
- [Step 3: Deploy Infrastructure (Keycloak, Kafka, MinIO)](#step-3-deploy-infrastructure)
- [Step 4: Deploy the Helm Chart](#step-4-deploy-the-helm-chart)
- [Step 5: Generate Test Data with Nise](#step-5-generate-test-data-with-nise)
- [Step 6: Upload Data and Trigger Ingestion](#step-6-upload-data-and-trigger-ingestion)
- [Step 7: Scale Workers for Testing](#step-7-scale-workers-for-testing)
- [Step 8: Run the Test Suite](#step-8-run-the-test-suite)
- [Credentials](#credentials)
- [Troubleshooting](#troubleshooting)
- [Useful Commands](#useful-commands)
- [Resuming After Reboot](#resuming-after-reboot)

---

## Prerequisites

- An aarch64 OpenShift cluster (tested on SNO 4.21 with LVM Operator for storage)
- `oc` CLI with cluster-admin access
- `podman` on the build host (can be the hypervisor itself)
- Helm 3.x
- Source repos cloned: `koku`, `ros-ocp-backend`, `insights-ingress-go`, `autotune` (Kruize), optionally `koku-ui`
- `nise` (koku-nise) installed for test data generation

## Network Access

The SNO VM (`192.168.122.131`) is on the hypervisor's libvirt bridge and is not
directly reachable externally. The hypervisor's external IP is `10.6.8.105`.

### /etc/hosts

Add to your workstation's `/etc/hosts`:

```
192.168.122.131  cost-onprem-ui-cost-onprem.apps.sno.karmalabs.corp cost-onprem-gateway-cost-onprem.apps.sno.karmalabs.corp keycloak-keycloak.apps.sno.karmalabs.corp
```

### SSH SOCKS Proxy (recommended)

```bash
ssh -D 1080 -N root@hpe-apollo-cn99xx-16.khw.eng.rdu2.dc.redhat.com
```

Configure your browser to use SOCKS5 proxy at `localhost:1080` (in Firefox:
Settings > Network Settings > Manual proxy > SOCKS Host `localhost`, Port `1080`,
select SOCKS v5, check "Proxy DNS when using SOCKS v5").

### Alternative: Static Route

If your workstation can reach `10.6.8.105`:

```bash
sudo ip route add 192.168.122.0/24 via 10.6.8.105
```

---

## Step 1: Build aarch64 Container Images

The upstream images are x86_64-only. Build arm64 images from source on the
hypervisor (or any aarch64 host with podman).

```bash
ssh root@hpe-apollo-cn99xx-16.khw.eng.rdu2.dc.redhat.com
```

### Koku (API, Masu, Listener, Workers, Beat)

```bash
cd /root/koku
podman build -t koku:latest -f Dockerfile .
```

### ros-ocp-backend (ROS API, Processor, Housekeeper, Rec Poller)

```bash
cd /root/ros-ocp-backend
podman build -t ros-ocp-backend:latest -f Dockerfile .
```

### insights-ingress-go

```bash
cd /root/insights-ingress-go
podman build -t insights-ingress-go:latest -f Dockerfile .
```

### Kruize (Autotune)

```bash
cd /root/autotune
podman build -t autotune:latest -f Dockerfile.autotune .
```

> **rsync caveat:** When syncing the `autotune` repo to the hypervisor, use
> `--exclude='/target'` (with leading slash) to only exclude the root Maven
> build output. A bare `--exclude='target'` also excludes the valid source
> directory `src/main/java/com/autotune/common/target/kubernetes/`, causing
> build failures (`cannot find symbol: class KubernetesServicesImpl`).

### PostgreSQL 16

The upstream `quay.io/insights-onprem/postgresql:16` is x86_64-only. Use the
official multi-arch image:

```bash
podman pull docker.io/library/postgres:16
podman tag docker.io/library/postgres:16 postgresql:16
```

---

## Step 2: Push Images to Internal Registry

Expose the OpenShift internal image registry and push all images:

```bash
export KUBECONFIG=/root/.kcli/clusters/sno/auth/kubeconfig

# Ensure the registry has a route
oc patch configs.imageregistry.operator.openshift.io/cluster --type merge \
  -p '{"spec":{"defaultRoute":true}}'

REGISTRY=$(oc get route default-route -n openshift-image-registry -o jsonpath='{.spec.host}')

# Create the target namespace if it doesn't exist
oc new-project cost-onprem 2>/dev/null || true

# Tag and push each image
for img in koku ros-ocp-backend insights-ingress-go autotune postgresql; do
  case $img in
    ros-ocp-backend) tag="phase6" ;;
    postgresql) tag="16" ;;
    *) tag="latest" ;;
  esac
  podman tag ${img}:${tag:-latest} ${REGISTRY}/cost-onprem/${img}:${tag}
  podman push --tls-verify=false ${REGISTRY}/cost-onprem/${img}:${tag}
done
```

---

## Step 3: Deploy Infrastructure

### Keycloak (RHBK)

The Red Hat Build of Keycloak operator may not have arm64 images. Use the
upstream Keycloak operator:

```bash
cd /path/to/cost-onprem-chart
./scripts/deploy-rhbk.sh --namespace keycloak
```

This creates:
- A `keycloak` namespace with the Keycloak operator and instance
- A `kubernetes` realm with the `cost-management-ui` client
- A `test` user (password: `test123`) with org_id/account attributes

### Kafka (AMQ Streams)

AMQ Streams 3.1 supports aarch64 natively. Install via OLM:

```bash
# Install the AMQ Streams operator (cluster-scoped)
# Then create a KafkaCluster in the kafka namespace
oc create namespace kafka 2>/dev/null || true
# Apply your KafkaCluster CR (see scripts/deploy-test-cost-onprem.sh for details)
```

### MinIO (S3-compatible storage)

MinIO provides multi-arch images. Deploy a standalone instance:

```bash
oc create namespace cost-onprem 2>/dev/null || true
# MinIO is deployed as part of the Helm chart (storage section)
```

---

## Step 4: Deploy the Helm Chart

### Create an arm64 values override file

```yaml
# arm64-values.yaml
ros:
  image:
    repository: image-registry.openshift-image-registry.svc:5000/cost-onprem/ros-ocp-backend
    tag: "phase6"

costManagement:
  api:
    image:
      repository: image-registry.openshift-image-registry.svc:5000/cost-onprem/koku
      tag: "latest"

ingress:
  image:
    repository: image-registry.openshift-image-registry.svc:5000/cost-onprem/insights-ingress-go
    tag: "latest"

database:
  server:
    image:
      repository: image-registry.openshift-image-registry.svc:5000/cost-onprem/postgresql
      tag: "16"

valkey:
  image:
    repository: image-registry.openshift-image-registry.svc:5000/cost-onprem/valkey
    tag: "8"

kruize:
  enabled: true
  image:
    repository: image-registry.openshift-image-registry.svc:5000/cost-onprem/kruize
    tag: "latest"

ui:
  replicaCount: 0  # set to 1 if you built a koku-ui arm64 image
```

### Install

```bash
helm upgrade --install cost-onprem ./cost-onprem \
  -n cost-onprem --create-namespace \
  -f openshift-values.yaml \
  -f arm64-values.yaml \
  --wait --timeout 10m
```

### Post-install Fixes

Several runtime fixes are needed due to environment variable mismatches:

**1. Ingress Kafka broker hostname:**

The `insights-ingress-go` binary reads `INGRESS_KAFKABROKERS` (no underscore),
not `INGRESS_KAFKA_BROKERS`:

```bash
oc set env deploy/cost-onprem-ingress -n cost-onprem \
  INGRESS_KAFKABROKERS=cost-onprem-kafka-kafka-bootstrap.kafka.svc.cluster.local:9092
```

**2. Listener S3 credentials for ROS:**

The Koku listener's `ROSReportShipper` reads `S3_ACCESS_KEY` / `S3_SECRET`
(not `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`):

```bash
oc set env deploy/cost-onprem-koku-listener -n cost-onprem \
  S3_ACCESS_KEY=minioadmin \
  S3_SECRET=minioadmin
```

**3. Init container image pull policy:**

If the Red Hat registry is unreachable (502), the `prepare-ca-bundle` init
container blocks pod startup. Change to `IfNotPresent`:

```bash
oc patch deploy cost-onprem-koku-listener -n cost-onprem --type=json -p='[
  {"op": "replace", "path": "/spec/template/spec/initContainers/0/imagePullPolicy", "value": "IfNotPresent"}
]'
```

---

## Step 5: Generate Test Data with Nise

Generate OCP data for multiple clusters, including ROS container-level data:

```bash
# Example: 4 clusters with different profiles
# Use UUID-formatted cluster_ids (required by ros-ocp-backend)

mkdir -p /tmp/nise-clusters

cat > /tmp/nise-clusters/cluster1-production.yml << 'EOF'
---
generators:
  - OCPGenerator:
      start_date: 2026-04-10
      end_date: 2026-04-21
      nodes:
        - node_name: prod-node-1
          cpu_cores: 16
          memory_gig: 64
          resource_id: prod-node-1-id
          namespaces:
            production-app:
              pods:
                - pod_name: api-server
                  cpu_request: 2
                  mem_request_gig: 8
                  cpu_limit: 4
                  mem_limit_gig: 16
                - pod_name: worker
                  cpu_request: 4
                  mem_request_gig: 16
                  cpu_limit: 8
                  mem_limit_gig: 32
            monitoring:
              pods:
                - pod_name: prometheus
                  cpu_request: 1
                  mem_request_gig: 4
                  cpu_limit: 2
                  mem_limit_gig: 8
        - node_name: prod-node-2
          cpu_cores: 16
          memory_gig: 64
          resource_id: prod-node-2-id
          namespaces:
            production-app:
              pods:
                - pod_name: cache-server
                  cpu_request: 2
                  mem_request_gig: 16
                  cpu_limit: 4
                  mem_limit_gig: 32
            logging:
              pods:
                - pod_name: elasticsearch
                  cpu_request: 4
                  mem_request_gig: 32
                  cpu_limit: 8
                  mem_limit_gig: 48
EOF

# Generate with --ros-ocp-info for ROS data
nise report ocp \
  --static-report-file /tmp/nise-clusters/cluster1-production.yml \
  --ocp-cluster-id a1b2c3d4-e5f6-7890-abcd-111111111111 \
  --insights-upload /tmp/nise-output/cluster1 \
  --ros-ocp-info \
  --daily-reports
```

> **CRITICAL:** The `--ocp-cluster-id` must be a valid UUID. The ROS processor
> validates `Cluster_uuid` in Kafka messages and rejects non-UUID values with:
> `Invalid kafka message: Key: 'KafkaMsg.Metadata.Cluster_uuid' Error:Field validation for 'Cluster_uuid' failed on the 'uuid' tag`

---

## Step 6: Upload Data and Trigger Ingestion

### Register OCP sources in Koku

Each cluster needs a corresponding source in Koku before the listener will
accept its reports:

```bash
# Get a JWT token
KEYCLOAK_ROUTE=$(oc get route keycloak -n keycloak -o jsonpath='{.spec.host}')
CLIENT_SECRET=$(oc get secret keycloak-client-secret-cost-management-ui -n keycloak \
  -o jsonpath='{.data.CLIENT_SECRET}' | base64 -d)
GATEWAY_ROUTE=$(oc get route cost-onprem-api -n cost-onprem -o jsonpath='{.spec.host}')

TOKEN=$(curl -sk "https://${KEYCLOAK_ROUTE}/realms/kubernetes/protocol/openid-connect/token" \
  -d "grant_type=password" \
  -d "client_id=cost-management-ui" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "username=test" \
  -d "password=test123" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Create a source for each cluster
curl -sk -X POST "https://${GATEWAY_ROUTE}/api/cost-management/v1/sources/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Production OCP Cluster",
    "source_type": "OCP",
    "authentication": {"credentials": {"cluster_id": "a1b2c3d4-e5f6-7890-abcd-111111111111"}}
  }'
```

### Package and upload tarballs

```bash
# Package each cluster's data
cd /tmp/nise-output/cluster1/<date-range>/
tar czf /tmp/cluster1.tar.gz *    # NOTE: use * not . to avoid ./ prefix

# Upload via the ingress endpoint
curl -sk -X POST "https://${GATEWAY_ROUTE}/api/ingress/v1/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/cluster1.tar.gz;type=application/vnd.redhat.hccm.tar+tgz"
```

> **Tarball packaging:** Use `tar czf archive.tar.gz *` (not `tar czf archive.tar.gz .`).
> The latter creates entries with `./` prefix (e.g., `./manifest.json`), which causes
> the Koku listener's ROS file matching to fail silently — the listener logs
> "No ROS reports to handle in the current payload" because
> `manifest.resource_optimization_files` has bare names but `mytar.getnames()`
> returns `./`-prefixed names.

### Enable OCP tags (optional, for tag-based cost models)

```bash
curl -sk -X POST "http://cost-onprem-koku-masu:5042/api/cost-management/v1/enabled_tags/" \
  -H "Content-Type: application/json" \
  -d '{"schema":"org1234567","action":"create","tag_keys":["environment","app"],"provider_type":"ocp"}'
```

---

## Step 7: Scale Workers for Testing

On a 64-core SNO node, the default worker configuration (1 replica, 200m CPU
limit) is severely under-provisioned. Scale up before running IQE or heavy
test suites:

```bash
# Scale replicas
oc scale deploy -n cost-onprem \
  cost-onprem-celery-worker-ocp --replicas=3
oc scale deploy -n cost-onprem \
  cost-onprem-celery-worker-summary --replicas=3
oc scale deploy -n cost-onprem \
  cost-onprem-celery-worker-priority --replicas=2
oc scale deploy -n cost-onprem \
  cost-onprem-celery-worker-cost-model --replicas=2
oc scale deploy -n cost-onprem \
  cost-onprem-celery-worker-default --replicas=2

# Increase resource limits
for deploy in \
  cost-onprem-celery-worker-ocp \
  cost-onprem-celery-worker-summary \
  cost-onprem-celery-worker-priority \
  cost-onprem-celery-worker-cost-model \
  cost-onprem-celery-worker-default; do
  oc set resources deploy/$deploy -n cost-onprem \
    --requests=cpu=500m,memory=512Mi \
    --limits=cpu=2,memory=2Gi
done

# Also bump API, Masu, and Database
oc set resources deploy/cost-onprem-koku-api -n cost-onprem \
  --requests=cpu=500m,memory=1Gi --limits=cpu=2,memory=4Gi
oc set resources deploy/cost-onprem-koku-masu -n cost-onprem \
  --requests=cpu=500m,memory=512Mi --limits=cpu=2,memory=2Gi
oc set resources sts/cost-onprem-database -n cost-onprem \
  --requests=cpu=1,memory=2Gi --limits=cpu=4,memory=8Gi
```

---

## Step 8: Run the Test Suite

From your workstation (must be logged into the cluster via `oc login`):

```bash
cd /path/to/cost-onprem-chart

# Run all tests except UI
NAMESPACE=cost-onprem ./scripts/run-pytest.sh --no-ui

# Run specific suites
./scripts/run-pytest.sh --e2e
./scripts/run-pytest.sh --ros
./scripts/run-pytest.sh --helm
```

Expected: **210 passed, 7 skipped** (UI tests deselected with `--no-ui`).

---

## Credentials

### Keycloak Test User (realm: `kubernetes`)

| Field | Value |
|-------|-------|
| Username | `test` |
| Password | `test123` |
| org_id | `1234567` |
| account_number | `10001` |

### Keycloak Clients

| Client ID | Secret location |
|-----------|-----------------|
| `cost-management-ui` | `keycloak-client-secret-cost-management-ui` secret in `keycloak` namespace (key: `CLIENT_SECRET`) |
| `cost-management-operator` | `keycloak-client-secret-cost-management-operator` secret in `keycloak` namespace |

### PostgreSQL

```bash
oc get secret cost-onprem-db-credentials -n cost-onprem -o jsonpath='{.data.postgres-password}' | base64 -d
```

### MinIO

| Field | Value |
|-------|-------|
| Endpoint | `http://minio.cost-onprem.svc:9000` |
| Access key | `minioadmin` |
| Secret key | stored in `cost-onprem-storage-credentials` secret |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Listener logs `Received unexpected OCP report from [cluster_id]` | No source registered for that cluster | Create an OCP source via the API (Step 6) |
| Listener logs `No ROS reports to handle in the current payload` | Tarball has `./` prefixed entries | Repackage with `tar czf archive.tar.gz *` |
| ROS processor rejects messages: `Cluster_uuid failed on the 'uuid' tag` | Cluster ID is not UUID format | Use UUID-formatted cluster IDs in nise and sources |
| ROS API returns `503: column recommendation_sets.id does not exist` | Legacy Kruize fallback on native engine schema | Deploy the fixed `ros-ocp-backend` (removes broken fallback) |
| Ingress logs `Failed to resolve 'kafka:29092'` | Wrong Kafka env var name | Set `INGRESS_KAFKABROKERS` (no underscore) on the ingress deployment |
| Listener `Init:ImagePullBackOff` for `ubi9/ubi-minimal` | Red Hat registry unreachable | Patch init container `imagePullPolicy` to `IfNotPresent` |
| All costs `0.00` but usage non-zero | No cost model rates applied | Create/update cost model via API with actual rates |
| Kruize experiments table empty (Phase 6) | Native engine stores in `costonprem_ros.recommendation_sets`, not `costonprem_kruize` | Expected behavior — check `recommendation_sets` instead |

---

## Deployed Components

All images run natively on aarch64 — no QEMU emulation.

| Component | Image | Replicas |
|-----------|-------|----------|
| Koku API | `internal-registry/cost-onprem/koku:latest` | 1 |
| Koku Masu | `internal-registry/cost-onprem/koku:latest` | 1 |
| Koku Listener | `internal-registry/cost-onprem/koku:latest` | 1 |
| Celery Beat | `internal-registry/cost-onprem/koku:latest` | 1 |
| Celery Worker OCP | `internal-registry/cost-onprem/koku:latest` | 3 |
| Celery Worker Summary | `internal-registry/cost-onprem/koku:latest` | 3 |
| Celery Worker Priority | `internal-registry/cost-onprem/koku:latest` | 2 |
| Celery Worker Cost Model | `internal-registry/cost-onprem/koku:latest` | 2 |
| Celery Worker Default | `internal-registry/cost-onprem/koku:latest` | 2 |
| Envoy Gateway | chart default (multi-arch) | 2 |
| Insights Ingress | `internal-registry/cost-onprem/insights-ingress-go:latest` | 1 |
| ROS API | `internal-registry/cost-onprem/ros-ocp-backend:phase6` | 1 |
| ROS Processor | `internal-registry/cost-onprem/ros-ocp-backend:phase6` | 1 |
| ROS Rec Poller | `internal-registry/cost-onprem/ros-ocp-backend:phase6` | 1 |
| ROS Housekeeper | `internal-registry/cost-onprem/ros-ocp-backend:phase6` | 1 |
| Kruize/Autotune | `internal-registry/cost-onprem/kruize:latest` | 1 |
| PostgreSQL | `docker.io/library/postgres:16` | 1 |
| Valkey | `registry.redhat.io/rhel10/valkey-8` (multi-arch) | 1 |
| MinIO | `quay.io/minio/minio:latest` (multi-arch) | 1 |

### Storage

| PVC | Size | StorageClass |
|-----|------|--------------|
| `postgres-storage-cost-onprem-database-0` | 30 Gi | lvms-vg1 |
| `minio-data` | 10 Gi | lvms-vg1 |
| `cost-onprem-valkey-data` | 5 Gi | lvms-vg1 |
| Kafka broker/controller volumes | 10+5 Gi | lvms-vg1 |
| Keycloak DB | 5 Gi | lvms-vg1 |

---

## Useful Commands

```bash
# Hypervisor access
ssh root@hpe-apollo-cn99xx-16.khw.eng.rdu2.dc.redhat.com
export KUBECONFIG=/root/.kcli/clusters/sno/auth/kubeconfig

# Check all pods
oc get pods -n cost-onprem -l app.kubernetes.io/instance=cost-onprem
oc get pods -n kafka
oc get pods -n keycloak

# Component logs
oc logs -n cost-onprem -l app.kubernetes.io/component=listener --tail=50
oc logs -n cost-onprem -l app.kubernetes.io/component=ros-processor --tail=50
oc logs -n cost-onprem -l app.kubernetes.io/component=cost-worker --tail=50

# Restart a component
oc rollout restart deploy/cost-onprem-koku-api -n cost-onprem

# Flush cache
oc exec -n cost-onprem deploy/cost-onprem-valkey -- valkey-cli FLUSHALL

# Database shell
oc exec -it -n cost-onprem cost-onprem-database-0 -- psql -U postgres

# Check Kafka topics
oc exec -n kafka kafka-cluster-kafka-0 -- bin/kafka-topics.sh --list --bootstrap-server localhost:9092

# Helm status
helm status cost-onprem -n cost-onprem
```

---

## Resuming After Reboot

Data and cost models persist in PostgreSQL volumes across reboots:

```bash
ssh root@hpe-apollo-cn99xx-16.khw.eng.rdu2.dc.redhat.com
export KUBECONFIG=/root/.kcli/clusters/sno/auth/kubeconfig

# Verify cluster health
oc get nodes
oc get co

# All pods should recover automatically
oc get pods -n cost-onprem
oc get pods -n kafka
oc get pods -n keycloak

# Flush cache for fresh API responses
oc exec -n cost-onprem deploy/cost-onprem-valkey -- valkey-cli FLUSHALL
```
