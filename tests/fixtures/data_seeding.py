"""
Session-scoped E2E data seeding fixture.

Automatically generates and ingests NISE test data when the ROS database lacks
sufficient rows for test coverage. The fixture is idempotent: it queries current
counts and only uploads categories below their minimum thresholds.

Environment variables:
  E2E_SKIP_SEED=true   Bypass all seeding (fast iteration on clusters with data).

Thresholds (global row counts in costonprem_ros):

  daily_container_digests              >= 100
  daily_namespace_digests              >= 50
  daily_pvc_digests                    >= 20
  gpu_container_digests                >= 20
  node_gpu_timeslicing_recommendations >= 1
  cluster_quota_recommendation_sets    >= 2
  daily_container_digests (business_hours schedule_type) >= 30

Timeouts:
  5 minutes per seed category; 15 minutes total session budget.

Business hours digests (schedule_type = business_hours) are normally created by
BH tests via masu reship. This fixture only ensures base container data exists;
it does not trigger reship.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pytest
import requests

from conftest import ClusterConfig, obtain_jwt_token
from e2e_helpers import (
    _apply_nise_template_date_overrides,
    _cluster_quota_enrichment_from_yaml,
    ensure_nise_available,
    enrich_cluster_quota_csv_files,
    get_koku_api_url,
    register_source,
    upload_with_retry,
    wait_for_provider,
)
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    get_secret_value,
    run_oc_command,
    wait_for_condition,
)

LOG = logging.getLogger(__name__)

from nise_fixture_paths import get_seeding_templates_dir

SEED_TEMPLATES_DIR = str(get_seeding_templates_dir())

# Deterministic cluster UUID per org for session seed uploads.
SEED_CLUSTER_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def seed_cluster_id(org_id: str) -> str:
    """Return a stable cluster UUID for session seed data."""
    return str(uuid.uuid5(SEED_CLUSTER_NAMESPACE, f"cost-onprem-session-seed-{org_id}"))


CATEGORY_TIMEOUT_SECONDS = 300
SESSION_TIMEOUT_SECONDS = 900
POLL_INTERVAL_SECONDS = 20


@dataclass(frozen=True)
class SeedCategory:
    """One ROS table threshold and its NISE template."""

    name: str
    table: str
    min_rows: int
    template: str
    count_query: Optional[str] = None
    wait_query: Optional[str] = None
    package_builder: Optional[str] = None  # "default" | "cluster_quota" | "pvc"

    def sql_count(self) -> str:
        if self.count_query:
            return self.count_query
        return f"SELECT COUNT(*) FROM {self.table}"


SEED_CATEGORIES: tuple[SeedCategory, ...] = (
    SeedCategory(
        name="container",
        table="daily_container_digests",
        min_rows=100,
        template="seed_container.yml",
        count_query=(
            "SELECT COUNT(*) FROM daily_container_digests "
            "WHERE schedule_type = 'all_hours'"
        ),
    ),
    SeedCategory(
        name="namespace",
        table="daily_namespace_digests",
        min_rows=50,
        template="seed_container.yml",
        count_query=(
            "SELECT COUNT(*) FROM daily_namespace_digests "
            "WHERE schedule_type = 'all_hours'"
        ),
    ),
    SeedCategory(
        name="pvc",
        table="daily_pvc_digests",
        min_rows=20,
        template="seed_pvc.yml",
        package_builder="pvc",
    ),
    SeedCategory(
        name="gpu",
        table="gpu_container_digests",
        min_rows=20,
        template="seed_gpu.yml",
    ),
    SeedCategory(
        name="gpu_timeslicing",
        table="node_gpu_timeslicing_recommendations",
        min_rows=1,
        template="seed_gpu.yml",
        wait_query="SELECT COUNT(*) FROM node_gpu_timeslicing_recommendations",
    ),
    SeedCategory(
        name="cluster_quota",
        table="cluster_quota_recommendation_sets",
        min_rows=2,
        template="seed_cluster_quota.yml",
        count_query=(
            "SELECT COUNT(*) FROM cluster_quota_recommendation_sets "
            "WHERE recommendation_type != 'none'"
        ),
        wait_query=(
            "SELECT COUNT(*) FROM cluster_quota_recommendation_sets "
            "WHERE recommendation_type != 'none'"
        ),
        package_builder="cluster_quota",
    ),
    SeedCategory(
        name="business_hours",
        table="daily_container_digests",
        min_rows=30,
        template="seed_container.yml",
        count_query=(
            "SELECT COUNT(*) FROM daily_container_digests "
            "WHERE schedule_type = 'business_hours'"
        ),
        wait_query=(
            "SELECT COUNT(*) FROM daily_container_digests "
            "WHERE schedule_type = 'all_hours'"
        ),
    ),
)


def _ros_db_credentials(cluster_config: ClusterConfig, db_pod: str) -> dict:
    """Resolve ROS PostgreSQL connection parameters."""
    secret_name = f"{cluster_config.helm_release_name}-db-credentials"
    user = get_secret_value(cluster_config.namespace, secret_name, "ros-user")
    password = get_secret_value(cluster_config.namespace, secret_name, "ros-password")
    if not user:
        user = "postgres"
    if not password:
        password = get_secret_value(cluster_config.namespace, secret_name, "postgres-password")

    db_name_result = run_oc_command([
        "get", "deployment", f"{cluster_config.helm_release_name}-ros-api",
        "-n", cluster_config.namespace,
        "-o", "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='DB_NAME')].value}",
    ], check=False)
    db_name = db_name_result.stdout.strip() if db_name_result.returncode == 0 else ""
    if not db_name:
        db_name = "costonprem_ros"

    db_namespace_result = run_oc_command([
        "get", "pod", db_pod, "-n", cluster_config.namespace,
        "-o", "jsonpath={.metadata.namespace}",
    ], check=False)
    db_namespace = (
        db_namespace_result.stdout.strip()
        if db_namespace_result.returncode == 0 and db_namespace_result.stdout.strip()
        else cluster_config.namespace
    )

    return {
        "pod_name": db_pod,
        "namespace": db_namespace,
        "database": db_name,
        "user": user,
        "password": password,
    }


def _query_ros_count(db: dict, sql: str) -> Optional[int]:
    rows = execute_db_query(
        namespace=db["namespace"],
        pod_name=db["pod_name"],
        database=db["database"],
        user=db["user"],
        query=sql,
        password=db.get("password"),
    )
    if not rows or not rows[0]:
        return None
    try:
        return int(rows[0][0])
    except (TypeError, ValueError):
        return None


def _seed_template_path(template_name: str) -> str:
    path = os.path.join(SEED_TEMPLATES_DIR, template_name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Seed NISE template not found: {path}")
    return path


def _generate_from_seed_template(
    cluster_id: str,
    template_name: str,
    start_date: datetime,
    end_date: datetime,
    output_dir: str,
) -> dict:
    """Generate NISE data from a fixture template with date overrides."""
    template_path = _seed_template_path(template_name)
    with open(template_path, encoding="utf-8") as handle:
        yaml_content = handle.read()
    yaml_content = _apply_nise_template_date_overrides(yaml_content, start_date, end_date)

    yaml_path = os.path.join(output_dir, "static_report.yml")
    with open(yaml_path, "w", encoding="utf-8") as handle:
        handle.write(yaml_content)

    nise_output = os.path.join(output_dir, "nise_output")
    os.makedirs(nise_output, exist_ok=True)

    cmd = [
        "nise", "report", "ocp",
        "--static-report-file", yaml_path,
        "--ocp-cluster-id", cluster_id,
        "-w",
        "--ros-ocp-info",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=nise_output,
    )
    if result.returncode != 0:
        raise RuntimeError(f"NISE seed generation failed: {result.stderr}")

    files = {
        "pod_usage_files": [],
        "gpu_usage_files": [],
        "ros_usage_files": [],
        "namespace_usage_files": [],
        "cluster_quota_files": [],
        "node_label_files": [],
        "namespace_label_files": [],
        "storage_usage_files": [],
    }
    for root, _, filenames in os.walk(nise_output):
        for filename in filenames:
            if not filename.endswith(".csv"):
                continue
            full_path = os.path.join(root, filename)
            if "pod_usage" in filename:
                files["pod_usage_files"].append(full_path)
            elif "gpu_usage" in filename:
                files["gpu_usage_files"].append(full_path)
            elif "ros_namespace" in filename or "namespace_usage" in filename:
                files["namespace_usage_files"].append(full_path)
            elif "cluster-quota" in filename or "cluster_quota" in filename:
                files["cluster_quota_files"].append(full_path)
            elif "ros_usage" in filename:
                files["ros_usage_files"].append(full_path)
            elif "node_label" in filename:
                files["node_label_files"].append(full_path)
            elif "namespace_label" in filename:
                files["namespace_label_files"].append(full_path)
            elif "storage_usage" in filename:
                files["storage_usage_files"].append(full_path)

    if not files["ros_usage_files"]:
        files["ros_usage_files"] = list(files["pod_usage_files"])
    if files["namespace_usage_files"]:
        files["ros_usage_files"] = list(
            dict.fromkeys(files["ros_usage_files"] + files["namespace_usage_files"])
        )
    if files["storage_usage_files"]:
        files["ros_usage_files"] = list(
            dict.fromkeys(files["ros_usage_files"] + files["storage_usage_files"])
        )
    if files["cluster_quota_files"]:
        enrich_cluster_quota_csv_files(
            files["cluster_quota_files"],
            _cluster_quota_enrichment_from_yaml(yaml_path),
        )
    return files


def _build_upload_package(
    category: SeedCategory,
    files: dict,
    cluster_id: str,
    start_date: datetime,
    end_date: datetime,
) -> str:
    """Create tarball for upload based on category needs."""
    ros_files = list(files.get("ros_usage_files") or [])
    pod_files = files.get("pod_usage_files") or ros_files
    builder = category.package_builder or "default"

    if builder == "cluster_quota":
        ros_files = list(dict.fromkeys(ros_files + (files.get("cluster_quota_files") or [])))
        return create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files or pod_files,
            cluster_id=cluster_id,
            start_date=start_date,
            end_date=end_date,
            node_label_files=files.get("node_label_files") or None,
            namespace_label_files=files.get("namespace_label_files") or None,
        )

    if builder == "pvc":
        storage_files = files.get("storage_usage_files") or []
        return create_upload_package_from_files(
            pod_usage_files=pod_files,
            ros_usage_files=ros_files or pod_files,
            cluster_id=cluster_id,
            start_date=start_date,
            end_date=end_date,
            storage_usage_files=storage_files or None,
            node_label_files=files.get("node_label_files") or None,
            namespace_label_files=files.get("namespace_label_files") or None,
        )

    return create_upload_package_from_files(
        pod_usage_files=pod_files,
        ros_usage_files=ros_files or pod_files,
        cluster_id=cluster_id,
        start_date=start_date,
        end_date=end_date,
        gpu_usage_files=files.get("gpu_usage_files") or None,
        storage_usage_files=files.get("storage_usage_files") or None,
        node_label_files=files.get("node_label_files") or None,
        namespace_label_files=files.get("namespace_label_files") or None,
    )


def _ensure_seed_source(
    cluster_config: ClusterConfig,
    ingress_pod: str,
    cluster_id: str,
    org_id: str,
) -> None:
    """Register the session seed source if not already present."""
    koku_url = get_koku_api_url(cluster_config.helm_release_name, cluster_config.namespace)
    identity = create_rh_identity_header(org_id)
    register_source(
        namespace=cluster_config.namespace,
        pod=ingress_pod,
        api_url=koku_url,
        rh_identity_header=identity,
        cluster_id=cluster_id,
        org_id=org_id,
        source_name=f"e2e-session-seed-{cluster_id[-8:]}",
        container="ingress",
    )


def _upload_seed_package(
    keycloak_config,
    ingress_url: str,
    package_path: str,
) -> None:
    upload_url = f"{ingress_url.rstrip('/')}/v1/upload"
    session = requests.Session()
    session.verify = False
    token = obtain_jwt_token(keycloak_config)
    response = upload_with_retry(
        session, upload_url, package_path, token.authorization_header
    )
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"Seed upload failed: HTTP {response.status_code} {response.text[:300]}")


def _wait_for_category(db: dict, category: SeedCategory, timeout: int) -> bool:
    wait_sql = category.wait_query or category.sql_count()

    def check() -> bool:
        count = _query_ros_count(db, wait_sql)
        if count is None:
            return False
        if category.name == "cluster_quota":
            return count >= category.min_rows
        if category.name == "business_hours":
            return count >= 100
        return count >= category.min_rows

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=POLL_INTERVAL_SECONDS,
        description=f"{category.name} seed processing",
    )


def _categories_needing_seed(db: dict) -> dict[str, list[SeedCategory]]:
    """Return categories below threshold grouped by template filename."""
    needed_by_template: dict[str, list[SeedCategory]] = {}

    for category in SEED_CATEGORIES:
        count = _query_ros_count(db, category.sql_count())
        if count is None:
            LOG.warning("[E2E Seed] Could not query %s — skipping category check", category.table)
            continue
        if count >= category.min_rows:
            LOG.info(
                "[E2E Seed] %s: %d rows (>= %d) — OK",
                category.name,
                count,
                category.min_rows,
            )
            continue
        LOG.info(
            "[E2E Seed] %s: %d rows (< %d) — needs seeding",
            category.name,
            count,
            category.min_rows,
        )
        needed_by_template.setdefault(category.template, []).append(category)

    return needed_by_template


def _seed_template_group(
    template_name: str,
    categories: list[SeedCategory],
    cluster_config: ClusterConfig,
    keycloak_config,
    ingress_url: str,
    ingress_pod: str,
    db_pod: str,
    org_id: str,
    cluster_id: str,
    timeout: int,
) -> bool:
    """Generate, upload once, and wait for all categories sharing a template."""
    primary = categories[0]
    db = _ros_db_credentials(cluster_config, db_pod)

    if all(c.name == "business_hours" for c in categories):
        base_count = _query_ros_count(
            db,
            "SELECT COUNT(*) FROM daily_container_digests WHERE schedule_type = 'all_hours'",
        )
        if base_count is not None and base_count >= 100:
            LOG.info(
                "[E2E Seed] business_hours: base container data present (%d all_hours rows); "
                "BH digests will be created by reship in BH tests",
                base_count,
            )
            return True

    names = ", ".join(c.name for c in categories)
    LOG.info("[E2E Seed] Seeding %s from %s...", names, template_name)
    temp_dir = tempfile.mkdtemp(prefix=f"e2e-seed-{primary.name}-")
    try:
        now = datetime.utcnow()
        start_date = now - timedelta(days=30)
        end_date = now - timedelta(days=1)

        _ensure_seed_source(cluster_config, ingress_pod, cluster_id, org_id)
        if not wait_for_provider(
            cluster_config.namespace, db_pod, cluster_id, timeout=180
        ):
            LOG.warning("[E2E Seed] Provider not ready for %s upload", template_name)
            return False

        files = _generate_from_seed_template(
            cluster_id=cluster_id,
            template_name=template_name,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
        )
        if not files.get("ros_usage_files") and not files.get("pod_usage_files"):
            LOG.warning("[E2E Seed] NISE produced no files for %s", template_name)
            return False

        builder_priority = {"cluster_quota": 3, "pvc": 2, "default": 1}
        primary_builder = max(
            categories,
            key=lambda c: builder_priority.get(c.package_builder or "default", 0),
        )
        package_path = _build_upload_package(
            primary_builder, files, cluster_id, start_date, end_date
        )
        _upload_seed_package(keycloak_config, ingress_url, package_path)

        all_ok = True
        per_category_timeout = max(60, timeout // max(len(categories), 1))
        for category in categories:
            if not _wait_for_category(db, category, timeout=per_category_timeout):
                LOG.warning("[E2E Seed] %s did not reach threshold in time", category.name)
                all_ok = False
            else:
                LOG.info("[E2E Seed] %s seed complete", category.name)
        return all_ok
    except Exception as exc:
        LOG.warning("[E2E Seed] %s seed failed: %s", template_name, exc)
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_session_data_seed(
    cluster_config: ClusterConfig,
    keycloak_config,
    ingress_url: str,
    org_id: str,
) -> None:
    """Check ROS counts and seed missing data categories."""
    if os.environ.get("E2E_SKIP_SEED", "").lower() in ("true", "1", "yes"):
        LOG.info("[E2E Seed] E2E_SKIP_SEED set — skipping session data seed")
        return

    ingress_pod = get_pod_by_label(
        cluster_config.namespace, "app.kubernetes.io/component=ingress"
    )
    db_pod = get_pod_by_label(
        cluster_config.namespace, "app.kubernetes.io/component=database"
    )
    if not ingress_pod or not db_pod:
        LOG.info("[E2E Seed] Cluster not available (no ingress/db pod) — skipping seed")
        return

    if not ensure_nise_available():
        LOG.warning("[E2E Seed] NISE not available — skipping session data seed")
        return

    db = _ros_db_credentials(cluster_config, db_pod)
    needed_by_template = _categories_needing_seed(db)
    if not needed_by_template:
        LOG.info("[E2E Seed] All data present — skipping seed")
        return

    cluster_id = seed_cluster_id(org_id)
    session_deadline = time.monotonic() + SESSION_TIMEOUT_SECONDS

    for template_name, categories in needed_by_template.items():
        remaining = session_deadline - time.monotonic()
        if remaining <= 0:
            LOG.warning("[E2E Seed] Session seed budget exhausted (15 min)")
            break
        category_timeout = min(CATEGORY_TIMEOUT_SECONDS, int(remaining))
        _seed_template_group(
            template_name=template_name,
            categories=categories,
            cluster_config=cluster_config,
            keycloak_config=keycloak_config,
            ingress_url=ingress_url,
            ingress_pod=ingress_pod,
            db_pod=db_pod,
            org_id=org_id,
            cluster_id=cluster_id,
            timeout=category_timeout,
        )


def _optional_cluster_fixtures(request) -> tuple[Optional[object], Optional[str]]:
    """Resolve Keycloak/ingress fixtures without failing helm-only test runs."""
    try:
        keycloak_config = request.getfixturevalue("keycloak_config")
        ingress_url = request.getfixturevalue("ingress_url")
        return keycloak_config, ingress_url
    except Exception:
        return None, None


@pytest.fixture(scope="session", autouse=True)
def e2e_session_data_seed(
    request,
    cluster_config: ClusterConfig,
    org_id: str,
) -> None:
    """Autouse session fixture: seed ROS test data when counts are below thresholds."""
    keycloak_config, ingress_url = _optional_cluster_fixtures(request)
    if not keycloak_config or not ingress_url:
        LOG.info("[E2E Seed] Keycloak/ingress unavailable — skipping session data seed")
        return
    try:
        run_session_data_seed(cluster_config, keycloak_config, ingress_url, org_id)
    except Exception as exc:
        LOG.warning("[E2E Seed] Session seed encountered an error: %s", exc)
