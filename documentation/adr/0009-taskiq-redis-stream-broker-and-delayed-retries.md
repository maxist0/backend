---
status: accepted
date: 2026-11-17
decision-makers: project owner
---

# Taskiq on a Redis stream with acknowledgements and delayed retries

## Context and Problem Statement

Background jobs (transactional mails, the heartbeat, later cleanup jobs) ran on taskiq-redis' `ListQueueBroker`. Its worker takes a message off the list with `BRPOP` before it runs the task, so a worker that crashes or is killed mid-task loses the message: delivery was at most once.

Retries never ran. The mail tasks set the label `retry_on_error=True`, but no retry middleware was registered. Registering taskiq's `SmartRetryMiddleware` alone would not help either. Without a schedule source it re-sends the task at once with a `delay` label, and neither the list broker nor the receiver reads that label (taskiq 0.13.0, `middlewares/smart_retry_middleware.py`, `on_send`).

How should tasks be delivered so that a crashed worker does not lose them, and how should a failed task be retried later rather than at once?

## Decision Drivers

* A task must not be lost when a worker stops mid-task (deploy, OOM kill, crash).
* Retries need a real delay, and only for errors that may pass (network, a busy SMTP server).
* Use the mechanisms taskiq and taskiq-redis already ship; no new dependency.
* Redis memory stays bounded.

## Considered Options

* `RedisStreamBroker` (Redis stream, consumer group, acknowledgements) with `SmartRetryMiddleware` and a Redis schedule source.
* `ListQueueBroker` with `SmartRetryMiddleware` and a Redis schedule source.
* Another broker (RabbitMQ through taskiq-aio-pika, NATS).

## Decision Outcome

Chosen option: "`RedisStreamBroker` with `SmartRetryMiddleware` and a Redis schedule source", because it gives at-least-once delivery on the Redis the stack already runs.

* **Broker:** taskiq-redis `RedisStreamBroker` on the stream `taskiq:tasks`. That is a new key: XADD on the former list key `taskiq` would fail with `WRONGTYPE`. A worker reads with `XREADGROUP` and acknowledges after the result is stored (`when_saved`, the worker CLI's default). The acknowledgement runs `XACK` and `XDEL` in one `MULTI`/`EXEC` transaction, so a processed entry (task arguments included) leaves Redis at once. Redis 7 has no single command for both ([XACKDEL](https://redis.io/docs/latest/commands/xackdel/) exists since Redis 8.2.0). Entries that stay unacknowledged longer than 10 minutes (`XAUTOCLAIM` min-idle-time) are handed to another worker. That limit exceeds the longest task: a mail is at most about seven SMTP steps of 60 s each.
* **Consumer group:** created from ID `0`, not taskiq-redis' default `$`. `$` makes the group skip every entry that is already in the stream, so tasks added before the group exists (for example after Redis lost its data) would never run ([XGROUP CREATE](https://redis.io/docs/latest/commands/xgroup-create/)). The API process declares the group at startup, before it enqueues anything.
* **Bounded stream:** with the delete on acknowledgement, the stream holds only unprocessed and in-flight entries. As a safety net every `XADD` still trims approximately (`MAXLEN ~ 100000`, [XADD](https://redis.io/docs/latest/commands/xadd/)). Trimming also drops unprocessed entries, so the bound sits far above any expected backlog.
* **Retries:** `SmartRetryMiddleware` with `TASKIQ_RETRY_COUNT` retries, `TASKIQ_RETRY_DELAY_SECONDS` apart, plus up to 1 s of jitter. A retry is a one-off schedule in taskiq-redis' `ListRedisScheduleSource`, stored as JSON, never pickled, because the scheduler loads it. The scheduler reads this source next to the label source of the cron tasks. Only tasks labelled `retry_on_error=True` that raise `TransientTaskError` are retried. Mail errors split into `TransientMailDeliveryError` (network errors, timeouts, SMTP 4xx replies, the "transient negative completion" of [RFC 5321 §4.2.1](https://www.rfc-editor.org/rfc/rfc5321#section-4.2.1)) and `MailDeliveryError` (5xx replies, template and programming errors).
* **Results:** stored for one hour as JSON, not with taskiq-redis' default pickle. An error is stored as type, module and arguments and comes back as the same exception class.
* **Outages:** the worker waits and reads again when Redis is unreachable or the consumer group has vanished (`NOGROUP`, for example after `FLUSHALL`). Wrong credentials (`AuthenticationError`) and other error replies end the worker, because waiting does not fix them.
* **Worker-only middleware:** the retry and Prometheus middlewares exist only in the worker (`app.worker`), and the metrics server listens on `TASKIQ_METRICS_HOST` (loopback by default). The API's broker only enqueues.

### Consequences

* Good, because a worker that dies mid-task no longer loses the message: another worker receives it after the idle timeout.
* Good, because processed tasks do not stay in Redis. A consumer group created again from `0` (for example after it was deleted) replays only entries that were never acknowledged, not old mails.
* Good, because transient failures are retried with a real delay, and permanent failures fail at once.
* Bad, because a task can run more than once. A worker can die after the task's side effect but before the acknowledgement, and a retry can be stored just before a crash that also leads to redelivery. **Every task must be idempotent.** The mail tasks become idempotent in the next step: the worker creates the token itself, and a new attempt invalidates the previous token of the same purpose.
* Bad, because retries depend on the scheduler. Without a running scheduler, retries wait in Redis. `run.py dev` therefore starts the scheduler, and deployments must run exactly one scheduler with both sources.
* Bad, because a retry runs up to the scheduler's update interval later than its delay (taskiq's default is 60 s). The scheduler runs with `--update-interval 10`.
* Neutral, because Redis must not evict the broker's keys. A shared Redis with `allkeys-lru` can drop the stream or the retry schedules under memory pressure.
* Neutral, because each worker process is a new stream consumer with a random name, and Redis keeps the names of stopped consumers. Their pending entries are claimed by the idle timeout.

## Pros and Cons of the Options

### `ListQueueBroker` with retries

* Good, because it is the smallest change.
* Bad, because delivery stays at most once: `BRPOP` removes the message before the task runs.

### Another broker

* Good, because RabbitMQ has acknowledgements, redelivery and delayed messages built in.
* Bad, because it adds a service to operate and a dependency, while Redis streams cover the need.

## More Information

* Redis streams, consumer groups and the pending entries list: <https://redis.io/docs/latest/develop/data-types/streams/>; [XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/).
* taskiq middlewares, including `SmartRetryMiddleware` and its `schedule_source`: <https://taskiq-python.github.io/available-components/middlewares.html> (checked 2026-11-17).
* Installed sources (checked 2026-11-17): taskiq 0.13.0 `middlewares/smart_retry_middleware.py`, `receiver/receiver.py` (acknowledgement after `set_result`); taskiq-redis 1.2.4 `redis_broker.py` (`RedisStreamBroker`), `list_schedule_source.py`.
* taskiq-redis 1.2.4 lets every redis-py error escape `RedisStreamBroker.listen`, so a worker died and restarted when Redis was briefly unreachable. The app's `ReconnectingStreamBroker` waits and reads again.
* Conventions: [AGENTS.md → Caching and background jobs](../../AGENTS.md#caching-and-background-jobs-target).
