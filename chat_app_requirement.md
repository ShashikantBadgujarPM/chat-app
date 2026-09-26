You are the lead software architect for a new production-style real-time chat application.

Do NOT write implementation code yet.

Your job is to analyze the product requirements, challenge the assumptions where necessary, and produce the High-Level Design (HLD), Low-Level Design (LLD), database design, API contracts, WebSocket architecture, and implementation roadmap that will later be implemented by another coding agent using Claude Sonnet.

## Product

We are building a real-time team chat application, similar in concept to a lightweight Slack/Teams.

The purpose is not just to build a chat UI. This is a serious Python learning project intended to develop production-level Python and backend engineering skills.

## Technology Stack

Backend:

* Python 3.12+
* FastAPI
* PostgreSQL
* SQLAlchemy 2.x
* Alembic
* Pydantic v2
* asyncio
* WebSockets
* JWT authentication
* Secure password hashing such as Argon2
* pytest
* httpx

Frontend:

* React
* TypeScript
* Vite

Infrastructure:

* Docker
* Docker Compose

Use Python's standard logging infrastructure initially. The architecture should support structured/JSON logging and correlation/request IDs.

Do not introduce Redis, Celery, Kafka, or other infrastructure unless you can clearly justify why it is required.

## Authentication

Authentication must be implemented by our own FastAPI backend.

We are NOT using Supabase/Auth0/Clerk or another managed authentication provider.

The system should support:

* Registration
* Login
* Logout
* Access tokens
* Refresh tokens
* Password hashing
* Token expiration
* Refresh token revocation
* Authenticated REST requests
* Authenticated WebSocket connections

## Core Chat Features

Users should be able to:

1. Register and log in.
2. Maintain a user profile.
3. Search for other users.
4. Start a one-to-one conversation.
5. Create group conversations.
6. Add members to groups.
7. Remove members from groups.
8. Send messages.
9. Edit their own messages.
10. Delete their own messages.
11. Reply to messages.
12. See message history.
13. Search messages.
14. See unread message counts.
15. Mark messages as read.
16. See online/offline presence.
17. See typing indicators.
18. Receive real-time messages.
19. Receive notifications.
20. Mention users using @mentions.

## Scheduled Messages

Scheduled messages are a CORE feature.

A user must be able to:

* Schedule a message for a future date/time.
* View scheduled messages.
* Edit scheduled messages.
* Cancel scheduled messages.
* Send scheduled messages when their scheduled time arrives.

Initially support one-time scheduled messages.

Design the architecture so recurring scheduled messages can be added later.

Example:

User:

"Hey Rahul, please check the deployment."

Schedule:

2026-10-01 18:00

The system stores the scheduled message.

A background scheduling/processing mechanism detects when it is due.

The system then:

1. Validates that it is still scheduled.
2. Sends/creates the actual message.
3. Persists the message.
4. Marks the scheduled message as sent.
5. Publishes a WebSocket event.
6. Creates any required notifications.

The implementation must consider:

* Race conditions
* Duplicate execution
* Retry behavior
* Transaction boundaries
* Failed scheduled messages
* Idempotency
* Application restarts
* Time zones
* UTC storage
* Observability/logging

Do not prematurely introduce a distributed task queue unless justified.

## Real-Time Requirements

WebSockets are a core part of the system.

REST should be used for:

* Authentication
* User management
* Conversation management
* Message history
* Search
* Scheduled message management
* Other request/response operations

WebSockets should be used for real-time events such as:

* New messages
* Message edits
* Message deletions
* Typing indicators
* Presence changes
* Read receipts
* Notifications
* Conversation changes
* Scheduled message delivery

IMPORTANT:

PostgreSQL is the source of truth.

WebSockets are only the real-time delivery mechanism.

For a message mutation:

Request
→ authentication
→ authorization
→ validation
→ business logic
→ PostgreSQL transaction
→ commit
→ publish WebSocket event
→ clients update their UI

Do NOT make WebSocket state the source of truth.

## Important User Scenarios

Design the system around real scenarios including:

### Normal message

User A sends a message to User B while both are online.

### Offline receiver

User A sends a message while User B is offline.

User B should receive the message when they reconnect.

### Receiver in another conversation

User B is online but currently viewing a different conversation.

They should receive a notification/unread update.

### Group chat

A message is sent to a group containing multiple users.

### Typing indicator

Typing status should be real-time and should NOT be persisted unnecessarily.

### Presence

Users should be able to see online/offline state and last-seen information.

### Read receipts

A user opens a conversation and messages become read.

### Message editing

The sender edits their own message and other connected users receive the update.

### Message deletion

The sender deletes their own message and connected users receive the update.

### WebSocket disconnect

A user's connection drops.

The client reconnects and synchronizes anything it may have missed.

### Multiple browser tabs

The same user has multiple active WebSocket connections.

Events should be handled correctly.

### Scheduled message

A user schedules a message for later.

### Scheduled message cancellation

A user cancels a scheduled message before execution.

### Application restart

The application restarts while scheduled messages exist.

Scheduled messages must not be lost.

### Duplicate scheduler execution

Two workers/processes must not accidentally send the same scheduled message twice.

### Invalid/expired authentication

REST and WebSocket authentication failures must be handled correctly.

## Architecture

Use a practical Clean Architecture / layered architecture.

Separate:

* API
* Application
* Domain
* Infrastructure

Avoid unnecessary abstraction.

The architecture should be understandable to an experienced Node.js/Express developer learning Python.

## Python Learning Goals

The project must naturally provide opportunities to learn:

* Python OOP
* Type hints
* Dataclasses where appropriate
* Exceptions and custom exception hierarchies
* Decorators
* Generators
* Iterators
* Context managers
* async/await
* asyncio
* concurrency
* dependency injection
* FastAPI
* SQLAlchemy
* PostgreSQL
* WebSockets
* background processing
* testing
* logging
* production configuration

Do not artificially introduce a Python feature just to demonstrate it.

Each feature should have a genuine engineering purpose.

## Database Design

Design the initial PostgreSQL schema.

At minimum consider:

* users
* refresh_tokens
* conversations
* conversation_members
* messages
* message_reads
* notifications
* scheduled_messages
* user_presence
* activity/audit records

For each table define:

* columns
* types
* primary keys
* foreign keys
* indexes
* uniqueness constraints
* nullable/non-nullable fields
* timestamps
* soft-delete requirements
* cascade behavior

Pay particular attention to:

* message history performance
* unread counts
* conversation membership
* scheduled-message lookup
* concurrent scheduler execution
* indexes

## REST API Design

Design versioned REST APIs.

Example structure:

/api/v1/auth/...
/api/v1/users/...
/api/v1/conversations/...
/api/v1/messages/...
/api/v1/scheduled-messages/...

Do not blindly follow these examples.

Design the proper resource boundaries and explain your reasoning.

Define:

* HTTP method
* endpoint
* request schema
* response schema
* status codes
* authorization requirements
* error cases

## WebSocket Design

Define:

* WebSocket endpoint
* authentication
* connection lifecycle
* connection manager
* user connections
* conversation subscriptions
* event envelope
* event types
* server-to-client messages
* client-to-server messages
* authorization
* disconnect handling
* reconnect behavior
* missed-event synchronization
* heartbeat/connection health if required

Define a consistent event envelope.

Example concept:

{
"type": "message.created",
"conversation_id": "...",
"payload": {...},
"timestamp": "..."
}

But improve this design if necessary.

## Background Processing

Design the scheduled-message processing architecture.

Initially prefer a simple architecture that can run reliably with PostgreSQL and Python.

Consider whether we need:

* scheduler
* polling interval
* database locking
* SELECT FOR UPDATE SKIP LOCKED
* worker processes
* retry state
* idempotency
* failure states

Explain the design choice.

The architecture should allow us to evolve to Redis/Celery or another queue later if scale requires it.

## Security

Cover:

* password hashing
* JWT security
* refresh-token security
* token revocation
* brute-force protection
* authorization
* WebSocket authentication
* message authorization
* conversation membership validation
* input validation
* SQL injection prevention
* sensitive logging
* rate limiting considerations
* CORS
* secure configuration
* secret management

## Testing

Design:

* unit tests
* integration tests
* API tests
* WebSocket tests
* authentication tests
* authorization tests
* scheduler tests
* concurrency/race-condition tests
* failure/retry tests

## Logging and Observability

Design production-oriented logging.

We want:

* structured logs
* log levels
* request IDs/correlation IDs
* user context where appropriate
* WebSocket connection lifecycle logs
* authentication/security events
* scheduler execution logs
* message processing logs
* exception stack traces
* no passwords/tokens/secrets in logs

## HLD + LLD Output

Your response must be structured as:

1. Product scope
2. Functional requirements
3. Non-functional requirements
4. System HLD
5. Backend architecture
6. Module boundaries
7. Directory structure
8. Domain model
9. Database ER design
10. Database indexes and constraints
11. Authentication architecture
12. Authorization model
13. REST API contract
14. WebSocket architecture
15. WebSocket event contracts
16. Scheduled-message architecture
17. Background processing
18. Error handling
19. Logging/observability
20. Security architecture
21. Testing architecture
22. Docker/local development architecture
23. Failure scenarios
24. Concurrency/race-condition analysis
25. Development milestones
26. Sonnet implementation strategy

For every development milestone provide:

* Goal
* Dependencies
* Files/modules expected
* Database changes
* API changes
* WebSocket changes
* Tests
* Acceptance criteria

Do NOT write implementation code.

Do NOT start generating FastAPI files.

Do NOT create boilerplate.

First solve the architecture and design problems.

Challenge the design wherever necessary before finalizing it.
