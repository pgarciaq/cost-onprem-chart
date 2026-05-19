# UI OAuth Flow Testing Guide

This guide explains how to test the UI authentication flow with Keycloak on OpenShift.

## Overview

The UI supports two deployment modes:

### OAuth Mode (with oauth2-proxy)

```
Browser → UI Route → oauth2-proxy → Keycloak Login → JWT Token → Session Cookie
```

Requires oauth2-proxy sidecar and Keycloak configuration. Login/logout flow tests
validate the full OIDC cycle.

### Standalone Mode (no OAuth)

```
Browser → UI Route → nginx → Static SPA (API calls get hardcoded X-Rh-Identity)
```

Used for development/testing without Keycloak. The UI is served directly by nginx
with a pre-configured identity header for API calls. No login is required.

**The test suite auto-detects the deployment mode** and skips OAuth-specific tests
(login flow, logout flow) when running against a standalone deployment.

## Prerequisites

1. **OpenShift Cluster Access**
   ```bash
   oc login <cluster-url>
   oc whoami  # Should show your username
   ```

2. **Deployed Components**
   - UI with oauth2-proxy (`cost-onprem-ui`)
   - Keycloak with `kubernetes` realm
   - Test user (created by `deploy-rhbk.sh`)

3. **Keycloak CA Certificate** (for self-signed certificates)
   
   If Keycloak uses a self-signed certificate, oauth2-proxy needs the CA certificate to trust it. The `install-helm-chart.sh` script creates this automatically when `ui.oauthProxy.tls.caCertEnabled=true`, but if deploying manually:
   
   ```bash
   # Extract the cluster CA (signs OpenShift route certificates)
   oc get secret router-ca -n openshift-ingress-operator \
     -o jsonpath='{.data.tls\.crt}' | base64 -d > ca.crt
   
   # Create secret in deployment namespace
   oc create secret generic keycloak-ca-cert \
     --from-file=ca.crt=./ca.crt \
     -n cost-onprem
   ```

## Quick Test

Run the pytest authentication tests:

```bash
# Run UI OAuth authentication tests
NAMESPACE=cost-onprem ./scripts/run-pytest.sh --auth

# Run specific UI OAuth test
NAMESPACE=cost-onprem ./scripts/run-pytest.sh -k "test_ui_oauth"
```

### What the Tests Validate

| Test | What it Validates |
|------|-------------------|
| UI Pod Health | Pod running, containers ready |
| OAuth Proxy TLS | No certificate errors in logs |
| OIDC Discovery | Keycloak accessible, realm configured |
| Token Acquisition | Password grant works, JWT returned |
| JWT Claims | Token contains required claims |

**See also:** [Test Suite Documentation](../tests/README.md) for detailed test options.

## Manual Testing

### 1. Verify UI is Accessible

```bash
# Get UI route
UI_ROUTE=$(oc get route -n cost-onprem -l app.kubernetes.io/component=ui -o jsonpath='{.items[0].spec.host}')
echo "UI URL: https://$UI_ROUTE"

# Open in browser - should redirect to Keycloak login
```

### 2. Browser Login Flow

1. Open `https://<UI_ROUTE>` in browser
2. You should be redirected to Keycloak login
3. Enter credentials: `test` / `test`
4. After login, you should see the Cost Management UI

### 3. Verify Token Claims

```bash
# Get Keycloak URL
KC_URL="https://$(oc get route -n keycloak -o jsonpath='{.items[0].spec.host}')"

# Get token
TOKEN=$(curl -sk -X POST "$KC_URL/realms/kubernetes/protocol/openid-connect/token" \
  -d "username=test" \
  -d "password=test" \
  -d "grant_type=password" \
  -d "client_id=cost-management-ui" \
  -d "scope=openid profile email" | jq -r '.access_token')

# Decode and view claims
echo "$TOKEN" | cut -d'.' -f2 | base64 -d 2>/dev/null | jq '.'
```

## Troubleshooting

### Keycloak Certificate Not Trusted

**Symptom:** oauth2-proxy fails with:
```
tls: failed to verify certificate: x509: certificate signed by unknown authority
```

**Cause:** The `keycloak-ca-cert` secret is missing or incorrect.

**Solution:**
```bash
# Verify secret exists
oc get secret keycloak-ca-cert -n cost-onprem

# If missing, create it:
oc get secret router-ca -n openshift-ingress-operator \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > ca.crt
oc create secret generic keycloak-ca-cert \
  --from-file=ca.crt=./ca.crt -n cost-onprem

# Restart the UI pod
oc rollout restart deployment -n cost-onprem -l app.kubernetes.io/component=ui
```

### Token Acquisition Fails

**Symptom:** 401 or `invalid_grant` error

**Check:**
- User exists in Keycloak `kubernetes` realm
- User is enabled
- Password is correct
- Client `cost-management-ui` exists

### Redirect Loop

**Symptom:** Browser keeps redirecting between UI and Keycloak

**Check:**
```bash
# Verify redirect URI in Keycloak matches route
oc get route -n cost-onprem -l app.kubernetes.io/component=ui -o jsonpath='{.items[0].spec.host}'
# This should match the Valid Redirect URI in Keycloak client settings
```

## Related Documentation

- [UI OAuth Authentication](ui-oauth-authentication.md) - Architecture details
- [Keycloak JWT Setup](keycloak-jwt-authentication-setup.md) - Keycloak configuration
