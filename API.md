# SnailStamp REST API

The API is built with Django REST Framework and uses DRF's built-in
`URLPathVersioning`. The current and default version is `v1`; all API routes live under
`/api/v1/`. Add a new version by implementing its URL module and adding that version to
`REST_FRAMEWORK.ALLOWED_VERSIONS` in `core/settings/base.py`.

Endpoints live with their Django domain app: ledger resources are implemented under
`src/snailstamp/apps/ledger/api/v1/`, and association membership endpoints under
`src/snailstamp/apps/tenant/api/v1/`. Their app-level `api/urls.py` modules are composed by
`src/snailstamp/api/urls.py`; API-wide authentication, schema, and exception handling stay there.

## Authentication

Protected endpoints require a JWT bearer token. Obtain a token pair:

```http
POST /api/v1/auth/token/
Content-Type: application/json

{"username": "your-username", "password": "your-password"}
```

Use the returned `access` token in `Authorization: Bearer <access>`. Refresh an expired
access token with `POST /api/v1/auth/token/refresh/` and `{"refresh": "<refresh-token>"}`.
The access token is required on association, registry, item, history, and transaction routes.

## API reference

- Interactive Swagger UI: `/api/docs/`
- OpenAPI schema: `/api/v1/schema/` (JSON; add `.yaml` for YAML)

Both documentation endpoints are public. The schema uses `drf-spectacular` and the API's
version is included in the generated document.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/auth/token/` | Obtain JWT access and refresh tokens |
| `POST` | `/api/v1/auth/token/refresh/` | Refresh an access token |
| `GET` | `/api/v1/me/associations/` | List the signed-in user's active association memberships |
| `GET` | `/api/v1/ledger/kinds/` | List registered item kinds |
| `GET` | `/api/v1/ledger/actions/` | List registered actions |
| `GET` | `/api/v1/ledger/items/?association_id=<uuid>` | List items currently owned by an association (paginated) |
| `GET` | `/api/v1/ledger/items/<item_id>/history/` | Read an item's append-only history |
| `POST` | `/api/v1/ledger/transactions/` | Enqueue a ledger operation; returns `202 Accepted` |
| `GET` | `/api/v1/ledger/transactions/<transaction_id>/` | Read queue status/result |

Association/member IDs supplied for a write must identify an active membership belonging to the
authenticated user. Reads of items and transactions are scoped to associations where that user has
an active membership. The item list and history only expose the current owner's items.

## Queue a transaction

Writes are asynchronous through the database-backed transaction queue and Celery/Redis. For
example, create 5 pens:

```http
POST /api/v1/ledger/transactions/
Authorization: Bearer <access>
Content-Type: application/json

{
  "association_id": "<association-uuid>",
  "member_id": "<member-uuid>",
  "operation": "create_item",
  "priority": 10,
  "payload": {
    "reason": "Initial inventory",
    "quantity": 5,
    "kind_code": "pen",
    "metadata": {},
    "prefix": "PEN-"
  }
}
```

The response includes the queue transaction `id` and its `pending` status. Poll the transaction
detail route until it reaches `succeeded` or `failed`. Priority is from 0 to 100; higher values
run first and equal priorities are FIFO. Enqueues are limited to 60 per association per 60 seconds
by default (configurable with `LEDGER_QUEUE_RATE_LIMIT` and
`LEDGER_QUEUE_RATE_PERIOD_SECONDS`).

Supported operations and payloads:

| Operation | Required payload fields | Optional payload fields |
|---|---|---|
| `create_item` | `reason`, `quantity`, `kind_code` | `metadata`, `prefix` |
| `send` | `collection_id` | `payload` |
| `claim_transfer` | `token` | — |
| `cancel_send` | `collection_id` | — |
| `assign` | `collection_id` | `new_holder_id` |
| `use` | `collection_id` | `action_code`, `payload` |
| `act` | `action_code`, `tool_id`, `target_id` | `content`, `content_hash`, `payload` |

## Run workers

Configure `REDIS_URL`, apply database migrations, then run a Celery worker and Beat scheduler:

```bash
celery -A snailstamp worker -l info
celery -A snailstamp beat -l info
```

Batch size defaults to 100 and is configured with `LEDGER_QUEUE_BATCH_SIZE`. Beat schedules a
recovery sweep every 5 seconds in case Redis was unavailable just after a committed enqueue.
The legacy polling worker remains available with `python manage.py process_ledger_queue`.
