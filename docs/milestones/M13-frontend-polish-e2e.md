# M13 — Frontend Polish and End-to-End Tests

## Goal
Bring the frontend to a quality people can demo. Every view gets loading, empty and error states. The connection-status banner reflects the WS state machine. Accessibility is reviewed (keyboard navigation, focus management, ARIA live regions for incoming messages and typing), and the layout works on narrow screens. Playwright E2E tests cover the main multi-user scenarios against the real compose stack. The backend changes only if the E2E tests find bugs.

## Dependencies
M08, M12 (so M00–M12 in practice).

## Files/modules expected
- `frontend/src/components/ConnectionBanner.tsx`: "Reconnecting…" / "Offline" / "Back online, syncing…", driven by the `ChatSocket` state
- Loading skeletons, empty states ("No conversations yet — start one"), and inline error states with retry across every feature
- Accessibility: roles and labels, `aria-live="polite"` for new messages in the open conversation and for typing indicators, keyboard shortcuts (Ctrl/Cmd+K to switch conversations, Esc to close dialogs, Up arrow to edit your last message), visible focus styles, contrast that passes WCAG AA
- Responsive layout: collapsible sidebar below 768 px, usable at 360 px wide
- `frontend/e2e/`: Playwright config that starts against `docker compose up` (a CI job with the compose services), plus a fixture that creates users through the API
- `docs/` updates: a README quickstart and screenshots

## Database changes
None.

## API changes
None planned. Any bug fix found by the E2E tests goes into the relevant module, with a regression test at the lowest layer that can reproduce it.

## WebSocket changes
None planned.

## Tests
Playwright, with two browser contexts (A and B), unless noted:
1. **Normal message:** A sends; B sees it live; B opens the conversation; A sees "Seen".
2. **Other conversation:** B is in conversation Y; A sends in X; B's X badge goes up; A mentions B; B gets a toast.
3. **Group:** three contexts; a message reaches both other members; the owner removes C; C's UI drops the group.
4. **Edit and delete:** both propagate live; the tombstone renders.
5. **Typing and presence:** A types and B sees the indicator; A closes → B sees offline after the grace period.
6. **Disconnect:** B goes offline (`context.setOffline(true)`); A sends 5 messages; B comes back online → all 5 appear in order, with no duplicates.
7. **Multi-tab:** B has two pages; a read in one clears the badge in the other; logout in one closes the other.
8. **Scheduled:** A schedules a message 40 s ahead; it shows as pending; it arrives at B live; A's list shows it as sent. A second one is cancelled and never arrives.
9. **Auth expiry:** with the access TTL set to 5 s in the E2E env, a session idle for longer than that still works through silent refresh; a revoked session goes to login.
- Plus: an axe-core accessibility scan on the main screens, with no serious or critical violations.

## Acceptance criteria
- All 9 E2E scenarios pass in CI against the compose stack, and each scenario is re-run 3 times without flaking.
- Each of the requirement doc's "Important User Scenarios" can be demonstrated by hand in the UI, following a short demo script added to the README.
- Lighthouse accessibility score of at least 90 on the main chat view.
