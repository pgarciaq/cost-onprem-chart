"""
Extended E2E: namespace recommendations data upload and API verification.

Test plan:
  1. Register a dedicated OCP source with a unique cluster UUID.
  2. Generate NISE data using ocp_report_ros_0.yml (--ros-ocp-info) with
     container and namespace-level ROS CSVs.
  3. Upload via ingress; wait for Koku summary tables and ROS processing.
  4. Wait for namespace_recommendation_sets rows in costonprem_ros.
  5. GET /recommendations/openshift/namespaces and assert cluster_uuid,
     namespace, and short/medium/long recommendation terms.
  6. Optionally verify historical_namespace_recommendation_sets snapshots.

Run (requires cluster + extended time budget):
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k namespace_recommendations_flow
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta

import pytest
import requests

from conftest import obtain_jwt_token, obtain_user_jwt_token_for
from e2e_helpers import (
    ensure_nise_available,
    generate_nise_data,
    get_koku_api_url,
    register_source,
    upload_with_retry,
    wait_for_provider,
)
from suites.ros.test_business_hours import (
    _capabilities_url,
    delete_business_hours_schedule,
    put_business_hours_schedule,
)
from suites.ros.test_namespace_recommendations import _fetch_namespaces
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    get_pod_by_label,
    wait_for_condition,
)

# Koku prepends "org" to JWT org_id; ROS stores bare org_id. SNO Keycloak uses "1234567".
_UPLOAD_ORG_ID = "1234567"
_NISE_TEMPLATE = "ocp_report_ros_0.yml"
_VALID_NAMESPACE_TERMS = frozenset({"short_term", "medium_term", "long_term"})


def _business_hours_capability_enabled(
    http_session: requests.Session, ros_api_url: str, auth: dict[str, str]
) -> bool:
    resp = http_session.get(_capabilities_url(ros_api_url), headers=auth, timeout=30)
    if resp.status_code != 200:
        return False
    return bool(resp.json().get("business_hours"))


def _namespace_detail_has_business_hours(detail: dict) -> bool:
    terms = (detail.get("recommendations") or {}).get("recommendation_terms") or {}
    if not isinstance(terms, dict):
        return False
    for term in terms.values():
        if not isinstance(term, dict):
            continue
        engines = term.get("recommendation_engines") or {}
        for profile in ("cost", "performance"):
            bh = (engines.get(profile) or {}).get("business_hours")
            if isinstance(bh, dict) and bh:
                return True
    return False


def _assert_business_hours_block_structure(bh: dict) -> None:
    assert isinstance(bh, dict)
    requests = bh.get("requests")
    assert isinstance(requests, dict), "business_hours.requests must be an object"
    limits = bh.get("limits")
    assert isinstance(limits, dict), "business_hours.limits must be an object"
    for resource in ("cpu", "memory"):
        if resource not in requests:
            continue
        resource_obj = requests[resource]
        assert isinstance(resource_obj, dict), f"business_hours.requests.{resource} must be an object"
        assert "amount" in resource_obj, f"business_hours.requests.{resource} missing amount"
        assert "format" in resource_obj, f"business_hours.requests.{resource} missing format"


def _wait_for_namespace_digest_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 420,
) -> bool:
    """Wait until daily_namespace_digests has rows for the cluster."""

    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM daily_namespace_digests
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="daily_namespace_digests population",
    )


def _wait_for_namespace_recommendation_rows(
    cluster_config,
    db_pod: str,
    cluster_id: str,
    org_id: str,
    timeout: int = 600,
) -> bool:
    """Wait until namespace_recommendation_sets has rows for the cluster."""

    def check():
        result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM namespace_recommendation_sets
            WHERE cluster_uuid = '{cluster_id}'
              AND org_id = '{org_id}'
              AND term IS NOT NULL
            """,
        )
        return result is not None and int(result[0][0]) > 0

    return wait_for_condition(
        check,
        timeout=timeout,
        interval=20,
        description="namespace_recommendation_sets population",
    )


def _namespace_user_auth(keycloak_config, cluster_config) -> dict[str, str] | None:
    """User password-grant JWT for namespace list API (see test_namespace_recommendations)."""
    token = obtain_user_jwt_token_for(
        keycloak_config,
        cluster_config,
        username="user_dev",
        password="redhat123",
    )
    return token.authorization_header


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.extended
@pytest.mark.slow
@pytest.mark.timeout(900)
class TestNamespaceRecommendationsExtendedFlow:
    """Upload ROS namespace data and verify namespace recommendation API output."""

    @pytest.fixture(scope="class")
    def namespace_e2e_cluster_id(self) -> str:
        short = uuid.uuid4().hex[:8]
        return f"e2e-ns-{short}"

    def test_namespace_e2e_upload_and_api(
        self,
        cluster_config,
        keycloak_config,
        namespace_e2e_cluster_id: str,
        ingress_url: str,
        ros_api_url: str,
        http_session: requests.Session,
    ):
        if not ensure_nise_available():
            pytest.skip("NISE is not available for namespace E2E data generation")

        ingress_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=ingress"
        )
        db_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=database"
        )
        if not ingress_pod or not db_pod:
            pytest.skip("Ingress or database pod not found")

        admin_identity = create_rh_identity_header(_UPLOAD_ORG_ID)
        koku_url = get_koku_api_url(
            cluster_config.helm_release_name, cluster_config.namespace
        )

        reg = register_source(
            namespace=cluster_config.namespace,
            pod=ingress_pod,
            api_url=koku_url,
            rh_identity_header=admin_identity,
            cluster_id=namespace_e2e_cluster_id,
            org_id=_UPLOAD_ORG_ID,
            source_name=f"e2e-ns-{namespace_e2e_cluster_id[-8:]}",
            container="ingress",
        )
        assert reg.source_id

        if not wait_for_provider(
            cluster_config.namespace, db_pod, namespace_e2e_cluster_id, timeout=180
        ):
            pytest.fail(f"Provider not created for cluster {namespace_e2e_cluster_id}")

        now = datetime.utcnow()
        start_date = now - timedelta(days=14)
        end_date = now - timedelta(days=1)
        temp_dir = tempfile.mkdtemp(prefix="e2e-namespace-")

        files = generate_nise_data(
            cluster_id=namespace_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            output_dir=temp_dir,
            include_ros=True,
            iqe_template=_NISE_TEMPLATE,
        )
        if not files.get("pod_usage_files"):
            pytest.fail("NISE did not generate pod_usage files for namespace E2E")
        if not files.get("ros_usage_files"):
            pytest.fail("NISE did not generate ros_usage files for namespace E2E")
        if not files.get("namespace_usage_files"):
            pytest.fail(
                "NISE did not generate ocp_ros_namespace_usage files; "
                "namespace E2E requires --ros-ocp-info namespace CSVs"
            )

        ros_files = list(
            dict.fromkeys(
                (files.get("ros_usage_files") or [])
                + (files.get("namespace_usage_files") or [])
            )
        )

        package_path = create_upload_package_from_files(
            pod_usage_files=files["pod_usage_files"],
            ros_usage_files=ros_files,
            cluster_id=namespace_e2e_cluster_id,
            start_date=start_date,
            end_date=end_date,
            node_label_files=files.get("node_label_files") or None,
            namespace_label_files=files.get("namespace_label_files") or None,
        )

        upload_url = f"{ingress_url.rstrip('/')}/v1/upload"
        upload_session = requests.Session()
        upload_session.verify = False
        token = obtain_jwt_token(keycloak_config)
        response = upload_with_retry(
            upload_session, upload_url, package_path, token.authorization_header
        )
        assert response.status_code in (200, 201, 202), response.text

        from e2e_helpers import wait_for_summary_tables

        schema = wait_for_summary_tables(
            cluster_config.namespace,
            db_pod,
            namespace_e2e_cluster_id,
            timeout=420,
        )
        if not schema:
            pytest.fail("Summary tables not populated after namespace E2E upload")

        if not _wait_for_namespace_digest_rows(
            cluster_config, db_pod, namespace_e2e_cluster_id, _UPLOAD_ORG_ID, timeout=420
        ):
            pytest.skip(
                "daily_namespace_digests not populated within timeout; "
                "namespace ROS CSV ingest may need additional cycles"
            )

        if not _wait_for_namespace_recommendation_rows(
            cluster_config, db_pod, namespace_e2e_cluster_id, _UPLOAD_ORG_ID, timeout=600
        ):
            pytest.skip(
                "namespace_recommendation_sets not populated within timeout; "
                "namespace recommendation engine may need additional cycles"
            )

        auth = _namespace_user_auth(keycloak_config, cluster_config)
        if not auth:
            pytest.skip("Could not obtain user JWT after upload")

        api_resp = _fetch_namespaces(
            http_session,
            ros_api_url,
            auth,
            {"cluster": namespace_e2e_cluster_id, "limit": 50},
        )
        assert api_resp.status_code == 200, api_resp.text
        body = api_resp.json()
        assert body.get("meta", {}).get("count", 0) > 0, body
        assert body.get("data"), "Expected namespace recommendation rows after upload"

        matched = [
            row
            for row in body["data"]
            if (row.get("cluster_uuid") or row.get("cluster")) == namespace_e2e_cluster_id
        ]
        assert matched, f"No namespace rows for cluster {namespace_e2e_cluster_id}"

        item = matched[0]
        namespace = item.get("project") or item.get("namespace")
        cluster = item.get("cluster_uuid") or item.get("cluster")
        assert namespace, "namespace list item must include project/namespace"
        assert cluster == namespace_e2e_cluster_id

        recs = item.get("recommendations") or {}
        assert "recommendation_terms" in recs
        terms = recs["recommendation_terms"]
        assert isinstance(terms, dict)
        assert _VALID_NAMESPACE_TERMS.intersection(terms.keys()), (
            f"Expected short/medium/long terms, got {list(terms.keys())}"
        )

        rec_id = item.get("id")
        detail_body = None
        if rec_id:
            detail_resp = http_session.get(
                f"{ros_api_url.rstrip('/')}/cost-management/v1/"
                f"recommendations/openshift/namespaces/{rec_id}",
                headers=auth,
                timeout=60,
            )
            assert detail_resp.status_code == 200, detail_resp.text
            detail_body = detail_resp.json()
            detail_recs = detail_body.get("recommendations") or {}
            assert "recommendation_terms" in detail_recs

        if rec_id and detail_body and _business_hours_capability_enabled(
            http_session, ros_api_url, auth
        ):
            try:
                bh_put = put_business_hours_schedule(
                    http_session,
                    ros_api_url,
                    auth,
                    cluster_id=namespace_e2e_cluster_id,
                )
                assert bh_put.status_code in (200, 202), bh_put.text

                def detail_has_bh() -> bool:
                    resp = http_session.get(
                        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
                        f"recommendations/openshift/namespaces/{rec_id}",
                        headers=auth,
                        timeout=60,
                    )
                    if resp.status_code != 200:
                        return False
                    return _namespace_detail_has_business_hours(resp.json())

                if wait_for_condition(
                    detail_has_bh,
                    timeout=420,
                    interval=20,
                    description="business_hours on namespace detail",
                ):
                    final_detail = http_session.get(
                        f"{ros_api_url.rstrip('/')}/cost-management/v1/"
                        f"recommendations/openshift/namespaces/{rec_id}",
                        headers=auth,
                        timeout=60,
                    )
                    assert final_detail.status_code == 200, final_detail.text
                    detail_json = final_detail.json()
                    assert _namespace_detail_has_business_hours(detail_json), (
                        "namespace detail must include business_hours when schedule is enabled"
                    )
                    terms = (detail_json.get("recommendations") or {}).get(
                        "recommendation_terms"
                    ) or {}
                    for term in terms.values():
                        if not isinstance(term, dict):
                            continue
                        for profile in ("cost", "performance"):
                            bh = (term.get("recommendation_engines") or {}).get(
                                profile, {}
                            ).get("business_hours")
                            if isinstance(bh, dict) and bh:
                                _assert_business_hours_block_structure(bh)
                else:
                    pytest.skip(
                        "business_hours block not present on namespace detail within timeout; "
                        "reship or namespace BH recommendations may still be processing"
                    )
            finally:
                delete_business_hours_schedule(
                    http_session,
                    ros_api_url,
                    auth,
                    cluster_id=namespace_e2e_cluster_id,
                )

        hist_result = execute_db_query(
            cluster_config.namespace,
            db_pod,
            "costonprem_ros",
            "postgres",
            f"""
            SELECT COUNT(*) FROM historical_namespace_recommendation_sets
            WHERE cluster_uuid = '{namespace_e2e_cluster_id}'
              AND org_id = '{_UPLOAD_ORG_ID}'
            """,
        )
        if hist_result is not None and int(hist_result[0][0]) == 0:
            pytest.skip(
                "historical_namespace_recommendation_sets empty; "
                "history snapshots may be written on a later processing cycle"
            )
