# Administrator LLM configuration management specification

## Goal

An authenticated administrator can see every registered user and centrally
create or update that user's LLM route. Ordinary users continue to manage only
their own route and cannot read the administrator APIs or page.

## Roles and bootstrap

- `users.role` is `user` or `admin`; existing databases migrate existing rows
  to `user` without losing accounts, tokens, or LLM configurations.
- `ADMIN_USERNAME` and `ADMIN_PASSWORD` seed or rotate one administrator at
  startup. Production deployments must provide both values through `.env`.
- New registrations always receive the `user` role.
- Authorization is checked against the current database role on every admin
  API request, rather than trusting a role cached in the browser session.

## APIs

- `GET /api/me` includes the signed-in user's current role.
- `GET /api/admin/users` requires `admin` and returns users plus public route
  metadata. It never returns encrypted or plaintext API keys.
- `PUT /api/admin/users/{user_id}/llm-config` requires `admin`, accepts the
  same validated payload as the self-service route, and supports blank-key
  preservation through omission of `upstream_api_key`.
- Unknown users return 404; authenticated non-admin users receive 403.

## User interface

`/admin/llm-configs` presents a user roster and one focused route editor. The
roster distinguishes configured and unconfigured users. Selecting a user loads
only public fields; saving increments that user's configuration version and
instructs that user to run `/login` again in Flavor Code.

The normal `/settings/llm` page links to the admin console only when
`/api/me.role` is `admin`. Direct navigation remains protected by the API.

## Security and audit boundaries

- API keys remain write-only in both self-service and administrator views.
- Admin list/update responses use the same public redaction helper as the
  self-service API.
- Administrator actions never expose another user's OAuth access tokens.
- Updating a route increments `config_version`, so previously issued user
  tokens fail closed at the gateway until the user authenticates again.
