---
status: accepted
date: 2026-11-16
decision-makers: project owner
---

# Slim admin that calls services

## Context and Problem Statement

The sqladmin panel's actions wrote to the database directly. They bypassed the service layer and therefore the audit log, and they duplicated the lock rules (the order in which rows are locked) that the services own. The panel also offered more views than the boilerplate needs.

Does the boilerplate keep an admin panel, and how does it change data?

## Decision Drivers

* Every state change is audited.
* Business and locking rules live in one place, the services.
* The panel stays small enough to maintain.

## Considered Options

* Keep sqladmin, slimmed down, with actions that call the auth service functions.
* Remove the admin panel.
* Keep the admin panel as it is.

## Decision Outcome

Chosen option: "keep sqladmin, slimmed down, actions through services", because operators need the panel and the audit trail must stay complete.

* The panel has the User, Session and AuditLog views. AuditLog is read-only.
* Actions call the auth service functions, so they are audited and follow the lock order.
* Modules add views through `Module.admin_views` ([ADR 0001](0001-core-and-module-contract.md)).

### Consequences

* Good, because admin changes appear in the audit log.
* Good, because lock rules exist once.
* Good, because the panel has fewer views to test and secure.
* Bad, because an action the services do not offer needs a service function first.
* Bad, because actions that wrote directly are removed or rewritten as service calls.

## Pros and Cons of the Options

### Remove the admin

* Good, because there is no admin attack surface or code to maintain.
* Bad, because operators lose a ready-made support tool.

### Keep as is

* Good, because there is no work.
* Bad, because the audit-log bypass and the duplicated lock rules remain.

## More Information

Open decision D2 (admin panel) in the README is related; this ADR records how the panel works while it stays. Implemented in S1–S3 of the 2026-11 cleanup.
