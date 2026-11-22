# PostgreSQL Database Tuning

Sizing and tuning guide for the bundled PostgreSQL StatefulSet in the cost-onprem Helm chart.

## Overview

The chart deploys a single unified PostgreSQL 16 instance (`registry.redhat.io/rhel10/postgresql-16`) that hosts all service databases:

| Database | Service |
|----------|---------|
| `costonprem_ros` | ROS (Resource Optimization Service) |
| `costonprem_koku` | Koku (Cost Management) |
| `costonprem_kruize` | Kruize (optimization engine) |
| `costonprem_rbac` | insights-rbac |

All services share one PVC and one set of PostgreSQL memory/connection limits. Tune `database.resources`, `database.storage.size`, and `database.postgresqlConfiguration` together.

## Database Requirements (Connection Budget)

The chart default `max_connections` is **200**. Application pools must stay below this limit.

| Component | Connections (default config) |
|-----------|------------------------------|
| ROS API | `ros.dbMaxConns` (10) × HPA `maxReplicas` (4) = **up to 40** |
| ROS processor / poller / housekeeper / cleaner | **10 each** |
| Koku workers (Celery) | **~20** |
| Koku API / masu | **~10** |
| RBAC | **~5** |
| Admin / migrations | **~5** |
| **Estimated total** | **up to ~120 of 200** |
| **Headroom** | **~80** |

**Per-pod rule (checked by the product at startup, ros-ocp-backend #898):** `ManifestDownloadWorkers × KafkaWorkers ≤ ros.dbMaxConns − 2` — two connections are reserved for recommendations and health checks. The product's default fan-out is 2 × 3 = 6 in-flight, so `ros.dbMaxConns` must be **≥ 8**; the chart default (10) matches the product's own `defaultDBMaxConns`. Dropping `ros.dbMaxConns` below 8 makes every processor start log a connection-pool warning that is then ignored — the chart's CI asserts the rendered value satisfies the rule.

If using **external PostgreSQL (DBaaS)**:

1. Set `database.deploy: false` and point `database.server.host` at your instance.
2. Ensure `max_connections >= 200` (or raise it to match your fleet size).
3. Lower `ros.api.autoscaling.maxReplicas` and/or `ros.dbMaxConns` if your DBaaS connection cap is lower.
4. Tag sync uses HTTP push by default (`ros.api.tagsSource: api`); direct Koku DB grants are only needed when `tagsSource: db`.

See `cost-onprem/values.yaml` (`database.postgresqlConfiguration` comment block) for the authoritative budget notes.

## Helm Values

| Value | Purpose |
|-------|---------|
| `database.resources` | Container CPU/memory requests and limits |
| `database.storage.size` | PVC size for `/var/lib/postgresql/data` |
| `database.postgresqlConfiguration` | Curated `postgresql.conf` settings — applied on **both** image families (see below) |

### How `postgresqlConfiguration` applies (#899)

The settings travel two paths, both carrying the same values:

1. **ALTER SYSTEM init SQL** (image-family independent): the chart renders `init-tuning.sh` into the database init-scripts directory (`database.server.initScriptsMountPath`). Every image family runs `.sh` init scripts — sclorg/RHEL from `/opt/app-root/src/postgresql-init`, the library `postgres` image from `/docker-entrypoint-initdb.d`. The settings persist to `postgresql.auto.conf` on the PVC and survive restarts.
2. **The sclorg `postgresql-cfg/` hook** (sclorg/RHEL images only): `custom-postgresql.conf` is appended to `postgresql.conf` on every container start. On the library `postgres` image this hook does not exist (upstream ships `include_dir` commented out), which is why path 1 exists.

**Init scripts run on first initialization only** (empty PVC). Changing `postgresqlConfiguration` on an existing database does **not** re-apply it — apply the change by hand instead:

```bash
oc exec -it cost-onprem-database-0 -- psql -U postgres \
  -c "ALTER SYSTEM SET shared_buffers = '1GB';"
# then restart the database pod for restart-only settings
```

`ALTER SYSTEM` is image-independent and persists in `postgresql.auto.conf` — the same mechanism path 1 uses. Verify what is live with `SHOW shared_buffers;` / `SHOW max_connections;` (in `psql`, or via `oc exec`).

Example production override (`production-values.yaml`):

```yaml
database:
  resources:
    requests:
      memory: "4Gi"
      cpu: "1000m"
    limits:
      memory: "4Gi"
      cpu: "4000m"
  storage:
    size: "100Gi"
  postgresqlConfiguration:
    shared_buffers: "1GB"
    work_mem: "32MB"
    effective_cache_size: "3GB"
    max_connections: "200"
    maintenance_work_mem: "256MB"
    random_page_cost: "1.1"
    autovacuum_max_workers: "4"
    log_autovacuum_min_duration: "1000"
```

Install with:

```bash
helm upgrade cost-onprem ./cost-onprem -n cost-onprem -f production-values.yaml
```

Or individual overrides:

```bash
helm upgrade cost-onprem ./cost-onprem -n cost-onprem \
  --set database.resources.limits.memory=4Gi \
  --set database.storage.size=100Gi \
  --set database.postgresqlConfiguration.work_mem=32MB
```

## Sizing Profiles

Choose a profile based on monitored container count (ROS workloads) and retention settings.

| Fleet size | Container memory | PVC size | `shared_buffers` | `work_mem` | `max_connections` |
|------------|------------------|----------|------------------|------------|-------------------|
| Demo (<1k) | 512Mi | 30Gi | 128MB | 4MB | 200 |
| Small (1–5k) | 2Gi | 50Gi | 512MB | 16MB | 150 |
| Medium (5–15k) | 4Gi | 100Gi | 1GB | 32MB | 200 |
| Large (15k+) | 8Gi+ | 200Gi+ | 2GB | 64MB | 300+ |

### Tuning rules of thumb

- **`shared_buffers`**: ~25% of container memory limit
- **`effective_cache_size`**: ~75% of container memory limit
- **`work_mem`**: Increase when ROS list/aggregation queries spill to disk or time out; each concurrent sort/hash operation can allocate up to `work_mem`
- **`maintenance_work_mem`**: Increase for faster `VACUUM`/`CREATE INDEX` on large partitioned tables
- **`max_connections`**: Must exceed the sum of all application connection pools (ROS API HPA × `ros.dbMaxConns`, processor, poller, housekeeper, Koku Gunicorn workers, Celery, RBAC, Kruize). See [Database Requirements (Connection Budget)](#database-requirements-connection-budget).
- **`random_page_cost`**: Use `1.1` on SSD/NVMe (Ceph RBD, local SSD); keep default `4.0` only on spinning disks

## Sample Retention Impact (E-2)

ROS raw usage samples (`container_usage_samples`) are the primary disk growth driver. With `ros.sampleRetentionDays: 45` (E-2), disk requirements are roughly **60–80% lower** than the previous 180-day default.

At 10k containers with 45-day retention, `container_usage_samples` is on the order of tens of millions of rows — still requiring Medium or Large profiles, not the 512Mi/30Gi demo defaults.

Monitor actual growth:

```bash
kubectl exec -n cost-onprem statefulset/cost-onprem-database -- \
  psql -U postgres -d costonprem_ros -c \
  "SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) AS size
   FROM pg_class WHERE relkind = 'r' ORDER BY pg_total_relation_size(oid) DESC LIMIT 10;"
```

## Verifying Applied Configuration

After upgrade, confirm settings are active:

```bash
kubectl exec -n cost-onprem statefulset/cost-onprem-database -- \
  psql -U postgres -c "SHOW shared_buffers; SHOW work_mem; SHOW max_connections;"
```

Check that the ConfigMap was mounted:

```bash
kubectl get configmap -n cost-onprem -l app.kubernetes.io/component=database
kubectl exec -n cost-onprem statefulpod/cost-onprem-database-0 -- \
  cat /opt/app-root/src/postgresql-cfg/custom-postgresql.conf
```

## External PostgreSQL

When `database.deploy: false`, these Helm values do not apply. Configure tuning on your external PostgreSQL instance directly and ensure connection limits align with application pool settings.

See [Configuration Reference](configuration.md#external-infrastructure-byoi) for external database setup.

## Related Documentation

- [Resource Requirements](resource-requirements.md) — cluster-wide CPU/memory/storage minimums
- [Performance Sizing Guide](../performance/sizing-guide.md) — component sizing from load testing
- [Helm Templates Reference](../architecture/helm-templates-reference.md) — database StatefulSet and ConfigMap templates

[← Back to Operations Index](README.md)
