# 26. Sonnet Implementation Strategy

This section is written for the person driving the implementation agent (Claude Sonnet) and for the agent itself. The design is fixed. The agent's job is to implement it faithfully, one milestone at a time, and to raise any disagreement with the design explicitly rather than work around it silently.

## 26.1 Working model

- **One milestone per session or branch** (`m05-messages`, …). Don't start milestone N+1 until milestone N's acceptance criteria pass and have been reviewed.
- **Small commits inside a milestone:** migration → domain + unit tests → repository + integration tests → service → API + API tests → WS → frontend. Each commit leaves the tests green.
- **The human (or a reviewing Opus session) reviews each milestone PR** against the milestone file's acceptance criteria and the "non-negotiables" below before merging.

## 26.2 Context to load per milestone

Loading every doc wastes context and invites the agent to build ahead. For each milestone, give it:
1. `docs/README.md` (the ADRs and glossary: always, it's short)
2. `docs/milestones/Mxx-*.md` (the milestone being built)
3. The design sections the milestone file links to (for example, M11 → `09-scheduled-messages.md`, `05-database.md` §scheduled_messages, `07-rest-api.md` §Scheduled messages, `13-failure-and-concurrency.md` R-3/R-4)
4. `03-backend-architecture.md` (layering and directory rules: always)
5. The existing code the milestone touches (the agent reads it itself; point it at the module)

## 26.3 Prompt template (per milestone)

```
You are implementing milestone {Mxx} of the chat-app project.

Read, in order: docs/README.md, docs/design/03-backend-architecture.md,
docs/milestones/{Mxx}-*.md, and the design sections it links to.

Rules:
- The design docs are the spec. Implement exactly the tables, constraints, endpoints,
  status codes, error codes, event types, and payload shapes they define.
- If the spec is ambiguous, contradictory, or you believe it is wrong: STOP and write the
  question/objection in docs/decisions/open-questions.md with your proposed resolution,
  then continue with the proposal clearly marked "PROVISIONAL" in code comments.
  Do not silently deviate.
- Do not implement features from later milestones. Stubs that a later milestone replaces
  are fine only where this milestone's file says so.
- Follow the non-negotiables in docs/design/15-sonnet-strategy.md §26.4.
- Work in the commit order in §26.1. Run the full test suite before each commit.
- Finish by checking every acceptance criterion in the milestone file and reporting
  each as PASS/FAIL with evidence (test name, command output, or manual step).
```

## 26.4 Non-negotiables (reject the PR if any are violated)

These are the mistakes an implementation agent is most likely to make, because each is easier locally but breaks a guarantee elsewhere in the design.

| # | Rule | Why (design reference) |
|---|---|---|
| 1 | **No SQLite, no `metadata.create_all()` in tests.** Tests run Alembic migrations against real Postgres | Most correctness guarantees live in Postgres features ([11 §21.1](11-testing.md)) |
| 2 | **Commit and rollback only inside `UnitOfWork`.** No `session.commit()` in services, repositories or routes | Atomicity of message + outbox, and of the scheduler's single transaction ([10 §18](10-errors-logging-security.md)) |
| 3 | **Every durable WS event goes through the outbox in the same transaction as the state change.** Never `await websocket.send(...)` from a REST handler or service | "Commit → publish" ([02 §4.2](02-hld.md), ADR-004) |
| 4 | **WS client frames never mutate durable state** | [08 §14.1](08-websocket.md) |
| 5 | **One write path per entity.** The worker calls `MessagingService.create_message`; it must not re-implement sending | [09 §17.3](09-scheduled-messages.md) |
| 6 | **Race guards are SQL, not Python:** conditional `UPDATE … WHERE status='pending'`, `ON CONFLICT`, `FOR UPDATE [SKIP LOCKED]`, atomic increments. No "SELECT, check in Python, then UPDATE" for guarded transitions | [13 §24](13-failure-and-concurrency.md) |
| 7 | **No naive datetimes.** `datetime.now(UTC)` / the `Clock` port only; no `utcnow()`; DB comparisons use `now()` | [09 §16.3](09-scheduled-messages.md) |
| 8 | **No f-strings or `%` formatting in SQL.** Only `text()` with bind params, and allow-lists for identifiers | [10 §20](10-errors-logging-security.md) |
| 9 | **Never log passwords, tokens, tickets, cookies or message bodies.** Log ids and lengths | [10 §19.5](10-errors-logging-security.md) |
| 10 | **`uvicorn --workers 1` for the API.** Don't "fix" this | ADR-002 |
| 11 | **Match expected constraint violations by constraint name**, never by message text | [10 §18.1](10-errors-logging-security.md) |
| 12 | **Blocking CPU work (Argon2) runs through `asyncio.to_thread`** | [06 §11.4](06-auth-and-authorization.md) |
| 13 | **Tests for races use deterministic hooks (`asyncio.Event`), not `sleep`** | [11 §21.4](11-testing.md) |
| 14 | **Application and domain layers import no FastAPI or SQLAlchemy** (enforce with `import-linter` or a simple test that greps imports) | [03 §5](03-backend-architecture.md) |
| 15 | **No new infrastructure** (Redis, Celery, Kafka, APScheduler) without an ADR | ADR-001 |

## 26.5 Definition of done (every milestone)

- [ ] Migration(s) are hand-reviewed; the upgrade/downgrade/upgrade round trip passes
- [ ] `ruff`, `mypy --strict app/` and `tsc --noEmit` are clean
- [ ] Every test listed in the milestone file exists and passes; coverage on `application/` and `domain/` is ≥ 85%
- [ ] The authorization matrix test is extended for any new endpoint
- [ ] The constraint registry test is extended for any new constraint referenced by name
- [ ] The OpenAPI snapshot is updated, with the diff reviewed (from M12 on; before that, the endpoint list is checked by hand against [07](07-rest-api.md))
- [ ] New log events follow [10 §19.4](10-errors-logging-security.md), and no sensitive data is logged
- [ ] Every acceptance criterion is reported PASS with evidence
- [ ] `open-questions.md` entries raised in this milestone are resolved or explicitly carried forward

## 26.6 Suggested learning cadence (for the human)

Because this is a learning project, the human should **write the first version of the key pieces personally**, and use the agent for the surrounding scaffolding, tests and frontend:
- M01 `UnitOfWork` (context managers)
- M02 `get_current_user` + the token issuer (DI, JWT)
- M05 `create_message` with `seq` assignment (transactions, concurrency)
- M06 `ConnectionManager` (asyncio queues and tasks)
- M11 the worker claim loop (SKIP LOCKED, savepoints)

Then ask the agent to review these hand-written pieces against the spec and extend their tests. After each milestone, spend 15 minutes reading the agent's code for that milestone and writing down one Python idiom learned. The Python feature map in [03](03-backend-architecture.md) is the checklist.

## 26.7 Escalation triggers (bring in a design review)

Stop and revisit the design, rather than letting the agent improvise, if any of these happen:
- A test for one of the R-1…R-18 races turns out flaky or fails intermittently
- The outbox replay overlap window is exceeded in practice (a sync test misses an event)
- A second API instance becomes necessary
- Any need for a scheduled-message `processing` state or a lease column arises
- The agent proposes adding infrastructure
