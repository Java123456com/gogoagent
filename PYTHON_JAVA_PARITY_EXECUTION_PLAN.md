# GoGo Agent Python/Java Parity Execution Plan

## 1. Purpose

This document is the implementation plan for closing the remaining business-capability gaps between:

- Java baseline: `gogo-agent_java_version/src/main/java`
- Python target: `gogo-agent/backend`

The baseline is source code, not README claims. "Parity" means equivalent user-visible behavior, data changes, error contracts, and safety boundaries. Python does not need to duplicate Java class names or AgentScope event types when its LangChain/LangGraph implementation already provides the same behavior.

Audit date: 2026-09-02.

## 2. Audit Summary

| Area | Python status | Java baseline | Decision |
|---|---|---|---|
| Agent pipeline, sub-agent process isolation, session/memory, interruption | Implemented | Implemented | Keep; regression-test only |
| Dynamic time, tool progress, result compression, skill-result collapse | Implemented | Implemented through AgentScope Hooks | Keep Python hook wrappers |
| User API Key encryption, command-time injection, RGH user isolation | Implemented | Implemented through pre/post Hooks | Keep Python command preparation path |
| Search-result capture | Implemented | `SearchResultCaptureHook` | Keep; retain contract tests |
| Successful booking result persistence and payment-card SSE | Implemented | `BookingPersistenceHook` | Harden tests and malformed-result handling |
| Booking cancellation | Implemented and verified | `BookingWriteTools` | Python platform-first cancellation service; flight/train failures preserve local state, hotel remains explicit local-only |
| Travel-order conflict detection | Implemented and verified | `TravelOrderConflictTools` | Python-compatible implementation of Java HIGH/MEDIUM/LOW type, severity, date-window, and ordering semantics |
| Booking-result frontend display | Implemented | Java frontend equivalent | Keep; add end-to-end SSE coverage |
| Observability | Implemented and verified | Java has lifecycle logging/Hooks | Structured privacy-safe diagnostics across pipeline, tools, side effects, and external commands |
| Reimbursement / A2A | Not a parity target | Java is also a placeholder | Do not schedule as migration work |

Verification on 2026-09-02: focused migration tests passed (`16 passed`); full project regression passed (`102 passed`).

## 3. Verified Existing Python Design

### 3.1 Hook-equivalent booking persistence

Python has not copied Java's `BookingPersistenceHook` class literally. The equivalent call chain is:

```text
LangChain tool completes
  -> ProgressEventHook.wrap_tools()
  -> tool_result_side_effects.process()
  -> parse successful tuniu result
  -> create booking_record idempotently
  -> emit booking_result SSE
  -> frontend ChatWindow adds booking timeline item
```

Key implementation locations:

- Hook wiring: `backend/hooks/context_hooks.py`
- Search and booking side effects: `backend/services/tool_result_side_effects.py`
- Booking persistence service/repository: `backend/services/booking_service.py`, `backend/infrastructure/repositories.py`
- SSE event contract: `backend/services/sse.py`
- Frontend event consumption: `frontend/src/components/ChatWindow.tsx`
- Existing contract coverage: `tests/test_tool_result_hooks.py`, `tests/test_sse_contract.py`

This is a valid Python implementation of "LLM zero intrusion": the model executes its business command; persistence happens after the result without requiring the model to call a second persistence tool.

### 3.2 Command preparation is already integrated

`backend/tools/skills.py` executes the command-time preparation pipeline before `subprocess.run`:

1. clear inherited user credential variables;
2. set user-isolated `HOME`/`USERPROFILE`;
3. prepare and later persist RGH user token isolation when command contains `rgh`;
4. inject only the current user's Tuniu, Flight, or FlyAI credential when applicable;
5. execute within the skill-command allowlist.

Do not create a second API Key Hook layer. Improvements must be made in this canonical path and covered by `tests/test_external_integrations_enterprise.py` and `tests/test_skill_tools.py`.

### 3.3 Booking card UI already exists

The backend emits `booking_result`; `ChatWindow.tsx` parses it and adds a `booking` timeline item. This is not a migration gap. The gap is test depth, not event wiring.

## 4. P0 Work Package A: Complete Travel-Order Conflict Detection

### Objective

Bring `backend/tools/conflict.py` to Java behavior and make the response safe for the itinerary-management prompt's required conflict gate.

### Java references

- `agent/tools/TravelOrderConflictTools.java`
- `agent/tools/model/CityTransitTimeService.java`
- `agent/tools/model/ConflictType.java`
- `agent/tools/model/ConflictSeverity.java`

### Python files

- Modify: `backend/tools/conflict.py`
- Add: `backend/services/city_transit_time.py` or a small private module adjacent to `conflict.py`
- Modify only if needed: `backend/config/settings.py`
- Add: `tests/test_conflict_tools.py`
- Extend: `tests/test_migration_contract.py`

### Required response contract

```json
{
  "has_conflict": true,
  "total_conflicts": 2,
  "conflicts": [
    {
      "type": "TIME_OVERLAP_DIFF_CITY",
      "severity": "HIGH",
      "order_id": "to_001",
      "order_summary": {},
      "description": "...",
      "suggestion": "..."
    }
  ],
  "summary": "命中 2 条冲突（HIGH=1, MEDIUM=1, LOW=0），请逐条阅读 description 与 suggestion。"
}
```

Keep the current snake_case JSON keys in Python unless the frontend/API already requires otherwise. The enum string values must match Java exactly:

- `TIME_OVERLAP_SAME_CITY`
- `TIME_OVERLAP_DIFF_CITY`
- `TRANSIT_TOO_TIGHT`
- `DISCONNECTED_ROUTE`
- severity: `HIGH`, `MEDIUM`, `LOW`

### Required algorithm

1. Validate `user_id`, both cities, both dates, and `departure_date <= return_date`.
2. Consider only `DRAFT`, `SUBMITTED`, and `APPROVED` orders; exclude `exclude_order_id`.
3. Include records one day before and after the candidate date range so adjacent-day checks cannot be missed.
4. Normalize city values by trimming and removing a trailing `市`.
5. Evaluate each candidate/existing pair in this exact order:
   - Same-day forward handoff: existing ends on candidate departure day.
   - Same-day reverse handoff: candidate ends on existing departure day.
   - Date overlap.
   - Next-day forward handoff.
   - Next-day reverse handoff.
6. For date overlap:
   - identical city pair -> `TIME_OVERLAP_SAME_CITY`, `LOW`;
   - otherwise -> `TIME_OVERLAP_DIFF_CITY`, `HIGH`.
7. For same-day cross-city handoff:
   - same city -> no conflict;
   - transit time `> 24h` -> `HIGH`;
   - transit time `> 0` and `<= 24h` -> `MEDIUM`, with wording that distinguishes normal-tight and very-tight handoffs.
8. For adjacent-day cross-city handoff:
   - same city -> no conflict;
   - transit time `> 8h` -> `DISCONNECTED_ROUTE`, `MEDIUM`;
   - transit time `<= 8h` -> no conflict.
9. Sort results by severity `HIGH`, `MEDIUM`, `LOW`, then `order_id`.
10. Produce a count-based summary and never throw for malformed historical order dates; skip malformed records and log a warning.

### Transit-time service behavior

Match the Java precedence:

1. explicit `city_pair_minutes` configuration, in either direction;
2. city-tier estimate;
3. conservative default.

Required defaults:

| City pairing | Minutes |
|---|---:|
| Same city | 0 |
| Tier 1 / new Tier 1 combination | 270 |
| Includes Tier 2 | 300 |
| Other or unknown city | 360 |
| Missing/invalid historical city | 240 |

Use existing city-tier settings. Do not introduce a live external transit query in this P0 package; Java does not do that either.

### Tests and acceptance criteria

- invalid/missing date and reversed date return a valid report with a readable summary;
- exact same route and overlapping dates -> `LOW`;
- different route and overlapping dates -> `HIGH`;
- same-day city handoff with 0 minutes -> no conflict;
- same-day 4.5-hour handoff -> `MEDIUM`;
- same-day >24-hour configured handoff -> `HIGH`;
- next-day 4.5-hour handoff -> no conflict;
- next-day 9-hour handoff -> `MEDIUM`;
- forward and reverse handoff cases both work;
- self-exclusion works;
- inactive orders and malformed historical records do not produce false errors;
- multiple results are ordered `HIGH > MEDIUM > LOW`;
- full focused suite passes: `pytest tests/test_conflict_tools.py tests/test_migration_contract.py`.

### Rollout and rollback

The tool is advisory, so no database migration is required. Release behind the existing tool endpoint, monitor conflict severity counts, and roll back by restoring the previous function if unexpected false positives occur. Do not change the prompt's requirement to invoke conflict detection.

## 5. P0 Work Package B: Platform-First Booking Cancellation

### Objective

Make `cancel_booking` match the Java safety invariant: flight/train local state changes only after a successful platform cancellation. Hotel remains explicitly local-only until a hotel cancellation integration exists.

### Java reference

- `agent/tools/BookingWriteTools.java`
- `agent/tools/BookingWriteToolsTest.java`

### Python files

- Modify: `backend/tools/booking.py`
- Modify: `backend/services/booking_service.py`
- Add: `backend/services/booking_cancellation.py`
- Reuse, do not duplicate: `backend/tools/skills.py`, `backend/services/api_key_service.py`
- Add: `tests/test_booking_cancellation.py`
- Extend: `tests/test_tool_result_hooks.py`
- Extend: `tests/test_agent_tool_boundaries.py` if public tool behavior changes

### Required behavior

1. Validate non-empty internal `booking_id`.
2. Load the record by its internal ID and verify it belongs to the requesting user.
3. A record already in `CANCELLED` returns successful idempotent output.
4. For `FLIGHT` and `TRAIN`:
   - require the user-scoped Tuniu API key;
   - call the corresponding Tuniu cancel command through the canonical restricted command executor;
   - never log or return the API key;
   - parse both current wrapped-MCP responses and legacy direct business responses;
   - accept `success: true` or `successCode: true`;
   - on timeout, nonzero exit, malformed output, or business failure, return `PLATFORM_CANCEL_FAILED` and preserve local status.
5. For `HOTEL` and unsupported types:
   - do not make a fictitious platform call;
   - update local status to `CANCELLED`;
   - return `platform_cancelled: false` and a clear user-facing explanation.
6. On successful/internal-only cancellation:
   - persist `CANCELLED`;
   - persist `reason` as the record remark;
   - return `success`, `booking_id`, `biz_type`, `new_status`, `platform_cancelled`, and `message`.

### Important design decision

`ToolResultSideEffects._cancel_booking()` is a compatibility path for a successful raw cancellation command. It must not be the authoritative cancellation workflow because it cannot enforce ownership, idempotency response semantics, API-key checks, or "platform before local update".

The public `cancel_booking` tool is the authoritative path. The booking-agent prompt must continue to prohibit direct shell cancellation.

### Tests and acceptance criteria

- empty ID, missing booking, and cross-user booking return distinct safe errors;
- repeated cancel is idempotent and does not call platform again;
- flight and train build the expected command and execute it through the existing allowlisted executor;
- successful wrapped result and successful legacy result update local status;
- platform missing API key, failure response, malformed response, timeout, and command failure leave status unchanged;
- hotel cancellation changes only local status and reports `platform_cancelled=false`;
- cancellation reason is saved;
- API key never appears in tool output, logs captured by test, progress event, or stored detail;
- hook side-effect cancellation only marks an already-owned matching record after a successful result;
- focused suite passes: `pytest tests/test_booking_cancellation.py tests/test_tool_result_hooks.py`.

### Rollout and rollback

This changes a write path. Release with structured cancellation outcome logging containing only booking ID, business type, platform outcome, and error code. If the external platform contract is unstable, disable flight/train cancellation at configuration level and return a retriable failure; never silently switch back to local cancellation.

## 6. P1 Work Package C: Harden the Booking Hook Contract

### Objective

Keep the existing Hook-equivalent implementation, but make result parsing and event publication resilient enough for real third-party payload drift.

### Files

- Modify: `backend/services/tool_result_side_effects.py`
- Extend: `tests/test_tool_result_hooks.py`
- Extend: `tests/test_sse_contract.py`
- Optional browser-level test: frontend chat SSE integration test if the project test tooling already supports it

### Required cases

- nonzero / `ok=false` shell result creates no booking and emits no card;
- malformed JSON creates no booking and does not crash the agent;
- `success=false` business payload creates no booking;
- missing external order number creates no booking;
- repeated successful order result creates one record and one card;
- all booking types preserve business type, user, session, travel order linkage, and payment fields;
- `booking_result` remains parseable by `ChatWindow.tsx`;
- search capture remains unaffected by booking parser changes.

### Acceptance criteria

The full hook/SSE suite passes, and a captured representative external payload for flight, hotel, and train is retained as a fixture rather than embedded repeatedly in test code.

## 7. P1 Work Package D: Production Observability

### Objective

Replace the current small `backend/observability.py` trace seam with structured, privacy-safe diagnostics across pipeline, tool, Hook-side-effect, and external command boundaries.

### Files

- Modify: `backend/observability.py`
- Modify: `backend/hooks/context_hooks.py`
- Modify: `backend/tools/skills.py`
- Modify: `backend/services/tool_result_side_effects.py`
- Add: `tests/test_observability.py`

### Required telemetry fields

- `request_id`, `session_id`, hashed/stable `user_id` correlation value, agent, tool, tool-call ID;
- duration, result class, cancellation state, retry/circuit-breaker state;
- booking business type and external result code only, never full response bodies by default;
- no API key, authorization header, phone number, contact name, payment URL query secrets, or raw token.

### Acceptance criteria

Tests prove redaction for nested dictionaries, command environments, exceptions, and `booking_result` payloads. Telemetry failures must never fail user business flow.

## 8. Deferred Items and Non-Goals

- Do not introduce an A2A reimbursement implementation solely for Java/Python parity; Java's reimbursement path is not a completed business baseline.
- Do not move API-key injection out of `execute_shell_command` into a second competing Hook mechanism.
- Do not make conflict checks automatically block submission. Java treats the report as advice; the Agent/User confirmation flow decides whether to continue.
- Do not add live traffic/transit APIs to satisfy conflict parity. This would be a new product capability, not a Java migration.
- Do not refactor unrelated agent architecture, database schema, or frontend visual design in these packages.

## 9. Execution Order

1. Establish fixtures and regression tests for current booking Hook/SSE behavior.
2. Implement P0-A conflict detection and complete its unit tests.
3. Implement P0-B platform-first cancellation and test every failure invariant.
4. Run P0 regression suite and manually exercise one flight, hotel, and train path in a non-production account.
5. Harden booking Hook parsing and SSE contracts.
6. Add observability/redaction tests and production telemetry.
7. Update the migration-status docs only after source and tests confirm completion.

## 10. Definition of Done

The migration is complete for this scope only when:

- conflict reports exhibit the Java severity/type/order semantics;
- flight/train cancellation cannot locally cancel an order after platform cancellation failure;
- hotel local-only behavior is explicit and user-visible;
- booking persistence remains transparent to the LLM and idempotent;
- frontend receives and renders booking cards from the existing SSE contract;
- all focused tests pass, followed by the project regression suite;
- documentation states the actual verified behavior and does not claim unimplemented features.
