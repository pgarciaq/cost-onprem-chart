"""
Extended E2E: VM recommendation savings fields and fleet rollup.

Run:
  NAMESPACE=cost-onprem ./scripts/run-pytest.sh --extended -k vm_savings_flow
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
import requests

from suites.ros.test_recommendations import get_fresh_token
from suites.ros.test_savings_summary import _plugin_sum, _savings_summary_url
from suites.ros.test_vm_recommendations import (
    _fetch_vm_list,
    skip_if_vm_plugin_disabled,
)
from utils import assert_structured_savings, parse_savings_value


def _parse_vm_savings(item: dict[str, Any]) -> Optional[float]:
    savings = item.get("savings")
    if savings is None:
        return None
    assert_structured_savings(savings)
    return parse_savings_value(savings)


@pytest.fixture
def vm_savings_auth(keycloak_config, cluster_config, http_session):
    auth = get_fresh_token(keycloak_config, cluster_config, http_session)
    if not auth:
        pytest.skip("Could not obtain JWT token")
    return auth


@pytest.mark.extended
@pytest.mark.ros
@pytest.mark.vm
@pytest.mark.integration
class TestVMSavingsFlowE2E:
    """VM per-row savings and fleet savings-summary vm plugin."""

    @pytest.mark.timeout(60)
    def test_vm_list_includes_savings_field(
        self,
        ros_api_url: str,
        vm_savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = _fetch_vm_list(http_session, ros_api_url, vm_savings_auth, {"limit": 5})
        skip_if_vm_plugin_disabled(resp)
        if resp.status_code != 200:
            pytest.skip(f"VM list unavailable: {resp.status_code} {resp.text}")
        data = resp.json().get("data", [])
        if not data:
            pytest.skip("No VM recommendations in cluster")
        for item in data:
            assert "savings" in item

    @pytest.mark.timeout(60)
    def test_vm_savings_null_or_structured_when_no_rates(
        self,
        ros_api_url: str,
        vm_savings_auth: dict,
        http_session: requests.Session,
    ):
        """Each row has savings null or a valid SavingsObject (disabled masu / no rates)."""
        resp = _fetch_vm_list(http_session, ros_api_url, vm_savings_auth, {"limit": 20})
        skip_if_vm_plugin_disabled(resp)
        if resp.status_code != 200:
            pytest.skip(f"VM list unavailable: {resp.status_code}")
        data = resp.json().get("data", [])
        if not data:
            pytest.skip("No VM recommendations in cluster")
        for item in data:
            savings = item.get("savings")
            if savings is not None:
                _parse_vm_savings(item)

    @pytest.mark.timeout(60)
    def test_idle_vm_savings_not_less_than_active_downsize(
        self,
        ros_api_url: str,
        vm_savings_auth: dict,
        http_session: requests.Session,
    ):
        """Idle VMs should not show lower savings than typical downsize rows when both have values."""
        idle_resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_savings_auth,
            {"filter[is_idle]": "true", "limit": 20},
        )
        down_resp = _fetch_vm_list(
            http_session,
            ros_api_url,
            vm_savings_auth,
            {"filter[is_oversized]": "true", "limit": 20},
        )
        skip_if_vm_plugin_disabled(idle_resp)
        if idle_resp.status_code != 200 or down_resp.status_code != 200:
            pytest.skip("VM list unavailable")

        idle_amounts = [
            v
            for item in idle_resp.json().get("data", [])
            if (v := _parse_vm_savings(item)) is not None
        ]
        down_amounts = [
            v
            for item in down_resp.json().get("data", [])
            if (v := _parse_vm_savings(item)) is not None
        ]
        if not idle_amounts or not down_amounts:
            pytest.skip("Need idle and downsize VM rows with non-null savings")

        assert max(idle_amounts) >= min(down_amounts), (
            f"Expected idle max {max(idle_amounts)} >= downsize min {min(down_amounts)}"
        )

    @pytest.mark.timeout(60)
    def test_fleet_savings_summary_includes_vm_plugin(
        self,
        ros_api_url: str,
        vm_savings_auth: dict,
        http_session: requests.Session,
    ):
        resp = http_session.get(
            _savings_summary_url(ros_api_url),
            headers=vm_savings_auth,
            timeout=60,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        by_plugin = body.get("by_plugin", {})
        assert "vm" in by_plugin
        assert_structured_savings(by_plugin["vm"])
        total = parse_savings_value(body["estimated_monthly_savings"])
        assert total is not None
        plugin_total = _plugin_sum(by_plugin)
        assert total == pytest.approx(plugin_total, abs=0.02)
