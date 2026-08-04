# Per-user LLM routing specification

## Goal

Each authenticated user owns one LLM service configuration. `flavor-code`
receives only public runtime metadata and a signed OAuth access token; the API
gateway resolves the token subject to the user's encrypted upstream URL and
API key before proxying requests.

## Security boundaries

- The upstream API key is encrypted at rest and is never returned by public
  APIs, OAuth responses, audit APIs, or logs.
- `llm_config.base_url` is the public gateway URL, not the upstream URL.
- The OAuth access token is the only credential stored by `flavor-code`.
- Gateway-to-auth configuration lookup requires `INTERNAL_SERVICE_TOKEN`.
- Gateway rejects models outside the user's configured `models` allow-list.
- A token whose `config_version` differs from the current user configuration
  is rejected with `configuration_changed`.

## Stored configuration

`user_llm_configs` has one row per user with: `provider_id`, `service_name`,
`api_type`, `upstream_url`, encrypted `upstream_api_key`,
`upstream_auth_type`, `default_model`, `cheap_model`, `models`,
`max_output_tokens`, monotonically increasing `config_version`, and timestamps.

## Public API

- `GET /api/me/llm-config`: session-authenticated, returns public fields and
  `api_key_configured`, never the API key.
- `PUT /api/me/llm-config`: session-authenticated full update. Omitting
  `upstream_api_key` preserves the current key; `clear_api_key=true` removes it.
- `GET /api/me`: returns the signed-in user for the settings UI.

## Internal API

`GET /internal/users/{user_id}/llm-config?version=N` requires the internal
service token and returns the decrypted routing configuration. Version mismatch
returns HTTP 409.

## OAuth extension

The JWT contains `config_version`. A successful `/token` response includes:

```json
{
  "config_version": 4,
  "llm_config": {
    "provider_id": "deepseek",
    "service_name": "Enterprise DeepSeek",
    "api_type": "anthropic",
    "base_url": "https://gateway.example.com",
    "default_model": "deepseek-v4-pro",
    "cheap_model": "deepseek-v4-flash",
    "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
    "max_output_tokens": 65536
  }
}
```

Users without a complete LLM configuration receive `llm_config_required`.

## Gateway behavior

After JWT verification, resolve routing by `(sub, config_version)` (optional
short-lived caching is disabled by default), validate the requested model, strip client credentials, add the
configured upstream credential header, then proxy streaming or regular traffic.

## Compatibility

Existing global upstream environment variables seed/fallback the default test
user only during migration. Once a user saves a personal configuration, the
personal row is authoritative.
