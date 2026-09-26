# 4. System High-Level Design

## 4.1 Component diagram

```mermaid
flowchart LR
    subgraph Client["React + TS SPA (Vite)"]
        UI["UI components"]
        WSClient["WS client<br/>reconnect + sync logic"]
        HTTPClient["REST client"]
    end

    subgraph API["FastAPI process (uvicorn, single instance in v1)"]
        REST["REST routers<br/>auth / users / conversations / messages / scheduled-messages"]
        WSGW["WebSocket gateway<br/>/ws endpoint"]
        ConnMgr["Connection manager<br/>user_id to connections"]
        AppSvc["Application services<br/>use cases"]
        Listener["Postgres LISTEN loop<br/>asyncio task"]
    end

    subgraph Worker["Scheduler worker process(es)<br/>same codebase, different entrypoint"]
        SchedLoop["Poll loop<br/>SELECT ... FOR UPDATE SKIP LOCKED"]
        WorkerSvc["Application services<br/>shared with API"]
    end

    subgraph DB["PostgreSQL"]
        Tables[("users, conversations, messages,<br/>scheduled_messages, ...")]
        Outbox[("event_outbox")]
        Notify[["pg_notify channel 'events'"]]
    end

    HTTPClient -->|HTTPS JSON| REST
    WSClient -->|WSS, ticket auth| WSGW
    REST --> AppSvc
    WSGW --> AppSvc
    AppSvc -->|SQLAlchemy async| Tables
    AppSvc -->|same txn| Outbox
    Outbox -->|trigger| Notify
    SchedLoop -->|claim rows| Tables
    WorkerSvc -->|same txn as claim| Tables
    WorkerSvc --> Outbox
    Notify -->|LISTEN| Listener
    Listener --> ConnMgr
    ConnMgr -->|fan-out to member sockets| WSClient
```

**Why one API process owns all WebSocket connections in v1:** presence and connection state are process-local. A single instance means "is this user online" is a simple in-memory set lookup, and fan-out is a direct in-process dict lookup — no need for a shared presence store. This is [ADR-002](../README.md#adr-002-single-api-instance-first). The `Listener` task exists so that events originating in the **worker process** (scheduled sends) still reach connected clients: the worker commits → Postgres fires `NOTIFY events` → the API's listener task wakes up → reads the new outbox row(s) → hands them to the Connection Manager exactly like an in-process event. This means REST-originated and worker-originated events flow through **one single code path** (outbox → notify → fan-out), which removes an entire class of "worked in testing, missed the worker case" bugs.

## 4.2 Request/event flow — the canonical write path

This is the flow the requirements doc mandates verbatim; every mutating endpoint and the scheduler follow it without exception.

```mermaid
sequenceDiagram
    participant C as Client
    participant API as FastAPI route
    participant Svc as Application service
    participant DB as PostgreSQL
    participant CM as Connection manager
    participant O as Other members' sockets

    C->>API: POST /conversations/{id}/messages (JWT)
    API->>API: authenticate (JWT dependency)
    API->>Svc: authorize + validate (Pydantic)
    Svc->>DB: BEGIN
    Svc->>DB: UPDATE conversations.last_message_seq, INSERT message
    Svc->>DB: INSERT event_outbox row
    Svc->>DB: COMMIT (NOTIFY delivered at commit)
    DB-->>CM: NOTIFY 'events' (outbox id)
    CM->>CM: read outbox row, look up recipients' connections
    CM-->>O: WS push message.created
    Svc-->>API: Message DTO
    API-->>C: 201 Created (same DTO)
```

The HTTP response and the WebSocket broadcast are **two independent projections of the same committed row** — the sender gets their own confirmation over REST (works even if their own socket is down), and every member (including other tabs of the sender) gets it over WS. Nothing is ever pushed over WS that wasn't already durably committed.

## 4.3 Sequence: offline receiver

```mermaid
sequenceDiagram
    participant A as User A (online)
    participant DB as PostgreSQL
    participant CM as Connection manager
    participant B as User B (offline)

    A->>DB: send message (commit + outbox)
    DB-->>CM: NOTIFY
    CM->>CM: look up B's connections: none
    Note over CM: nothing to push, nothing lost: the row is committed
    B->>DB: later: GET /conversations (REST)
    Note over B,DB: unread_count comes from last_read_seq vs last_message_seq,<br/>so no "pending delivery" table is needed
    B->>CM: WS connect + sync.request (if the tab stayed open)
```

Because unread state is derived from committed rows (`last_message_seq` vs `last_read_seq`), there is no separate "pending delivery" concept to reconcile — reconnecting and calling the ordinary REST endpoints already returns the correct state. The WS sync protocol (§ [08-websocket.md](08-websocket.md)) exists only to backfill the **live event stream** (so the open UI updates without a full refetch), not to determine correctness.

## 4.4 Deployment view (Docker Compose, v1)

```mermaid
flowchart TB
    subgraph compose["docker compose"]
        db[("postgres:16")]
        api["api<br/>uvicorn --workers 1"]
        worker["worker<br/>same image, python -m app.worker"]
        web["web<br/>vite dev / nginx serving the build"]
    end
    web -->|proxies /api and /ws| api
    api --> db
    worker --> db
```

`api` and `worker` are the **same Docker image**; only the container command differs (`uvicorn app.main:app` vs `python -m app.worker`). This keeps the codebase single-sourced and avoids duplicated dependency images. See [12-local-dev-docker.md](12-local-dev-docker.md).

## 4.5 Path to multi-instance (documented, not built)

If load requires more than one API instance:
1. Presence moves from an in-memory set to a `presence` row per `(user_id, instance_id)` or a Redis set — first real justification for introducing Redis.
2. `pg_notify`'s ~8KB payload and at-most-once, no-history-if-no-listener semantics become the bottleneck (each instance must independently `LISTEN`, which still works fine for fan-out — every instance gets every NOTIFY — but connection-affinity for a given user's sockets must be tracked so a REST call on instance A can know it needs no local action while instance B holds that user's socket). Because every instance independently listens and re-reads the outbox, this still works **without Redis** up to a moderate number of instances; it only breaks down at a NOTIFY-throughput ceiling, which is the actual trigger for introducing Redis pub/sub or NATS.
3. Rate limiting moves from in-process token buckets to a shared store.
This upgrade path requires no schema changes because the outbox table and `seq` columns are already the shared source of truth — only the delivery mechanism changes.
