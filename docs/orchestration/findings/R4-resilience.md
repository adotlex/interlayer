# R4 — Resilience Algorithm Semantics

**Stream:** Wave 1 / R4  **Status:** complete  **Target:** Node 22 (verified on v22.22.2), TypeScript, **zero runtime dependencies**

Every algorithm below is hand-rolled. Every literal implementation in §5 was **executed and validated in this
environment** — 26 behavioural assertions, all passing (§5.7). One genuine design bug was found and fixed during
validation (breaker trip condition must be evaluated on *every* recorded outcome, not only on failures — see §3.4).

> **Determinism contract (constraint C3).** Every primitive takes an injected `now()`, an injected
> `{ setTimeout, clearTimeout }`, and an injected `random()`. No primitive may call the globals directly.
> This is non-negotiable: Wave 3 cannot write deterministic tests otherwise. It is repeated as a rule in §6.0.

---

## 0. Summary of decisions

| Primitive | Chosen algorithm | Key default |
|---|---|---|
| Retry | Exponential backoff + **full jitter** | `maxAttempts: 3` (**total**, incl. the first) |
| Timeout | Manual `AbortController` + injected timers | `attemptTimeoutMs: 10_000`, `totalTimeoutMs: 30_000` |
| Circuit breaker | **Rolling time-window failure ratio + minimum-throughput guard** | `failureRatio: 0.5`, `minimumThroughput: 10` |
| Rate limiter | **Token bucket**, monotonic clock, bounded FIFO queue | `capacity: 10`, `refillPerSec: 10` |
| Nesting | `TotalTimeout → Retry → RateLimit → Breaker → AttemptTimeout` | see §5.6 |

---

## 1. Retry with backoff

### 1.1 Options considered — backoff strategies

Let `base` = `baseDelayMs`, `cap` = `maxDelayMs`, `n` = 1-based **retry index** (`n = 1` is the first retry, i.e. the
wait *before* attempt 2), `factor` = multiplier, `rand()` ∈ [0, 1).

Define `exp(n) = min(cap, base × factor^(n−1))`.

| Strategy | Formula | Spread | Server load under contention | Total time | Verdict |
|---|---|---|---|---|---|
| Fixed | `min(cap, base)` | none | worst — perfectly synchronised retry waves | low | Reject as default |
| Exponential, no jitter | `exp(n)` | none | bad — clients stay in lockstep after a shared outage | medium | Reject as default |
| **Exponential + full jitter** | `rand() × exp(n)` | `[0, exp(n))` | **best** — least total client work | slightly higher than decorrelated | **DEFAULT** |
| Exponential + equal jitter | `exp(n)/2 + rand() × exp(n)/2` | `[exp(n)/2, exp(n)]` | slightly more work than full | *much* longer | Offer, not default |
| Decorrelated jitter | `min(cap, randBetween(base, prev × 3))`, `prev₀ = base` | unbounded-ish, stateful | more total work than full | slightly faster than full | Offer, not default |

**Canonical formulas** (AWS Architecture Blog, Marc Brooker, *Exponential Backoff And Jitter*):

```
sleep = min(cap, base * 2 ** attempt)                      # no jitter
sleep = random_between(0, min(cap, base * 2 ** attempt))   # full jitter
temp  = min(cap, base * 2 ** attempt)                      # equal jitter
sleep = temp / 2 + random_between(0, temp / 2)
sleep = min(cap, random_between(base, sleep * 3))          # decorrelated (stateful; sleep starts at base)
```

**Recommendation: full jitter, as the default.** AWS's own experiments found that under contention the jittered
approaches cut client call count by more than half versus un-jittered exponential backoff; between the jittered
variants, **full jitter does the least total work** (lowest load on the upstream, which is the thing we are trying to
protect). Equal jitter does slightly more work *and* takes much longer, so it is dominated. Decorrelated jitter
finishes marginally sooner but does more total work, and — decisively for us — it is **stateful across retries**,
which makes it harder to reason about, harder to cap, and harder to test. Full jitter is a pure function of
`(n, base, cap, factor, rand())`, which makes every Wave-3 assertion a one-liner with a seeded `random()`.

All four are implemented and selectable; only the default is opinionated.

### 1.2 `maxAttempts` semantics — **DECIDED, do not re-litigate**

> **`maxAttempts` is the TOTAL number of invocations of the operation, including the first one.**
> `maxAttempts: 3` ⇒ at most **3** calls to the underlying function ⇒ at most **2** retries ⇒ at most **2** sleeps.

| Value | Invocations | Retries | Sleeps | Behaviour |
|---|---|---|---|---|
| `0` | — | — | — | **`RangeError` at construction time.** Not silently coerced. |
| `1` | 1 | 0 | 0 | Retry disabled. The original error propagates **unwrapped**. |
| `3` (default) | ≤3 | ≤2 | ≤2 | |

Rationale: this matches **cockatiel** (`maxAttempts` = "the number of attempts to make before giving up") and reads
naturally in config. It deliberately **differs from Polly**, whose `MaxRetryAttempts` (default `3`) is documented as
"the maximum number of retries to use, **in addition to the original call**" — i.e. 4 total invocations. Two major
libraries disagree on this exact word, which is precisely why it must be nailed down here. We choose *total*, and the
name `maxAttempts` (not `maxRetries`) encodes it. Wave 2 must add the doc comment
`/** Total attempts including the first. maxAttempts: 3 ⇒ at most 2 retries. */`.

### 1.3 Retryable-error predicate

Expressed as a **user-supplied predicate**, not a status-code list, so the transport layer stays decoupled:

```ts
type RetryPredicate = (error: unknown, attempt: number) => boolean;
```

Default predicate (`isTransient`), in order:

1. `CancelledError` → **never retryable** (checked before the predicate; see §1.6).
2. `CircuitOpenError` → **never retryable** (spinning the loop against an open breaker is pure waste; the
   routing/fallback layer above should catch it and try another provider — see §5.6).
3. Error carries `retryable === false` → not retryable.
4. Error carries `retryable === true` → retryable.
5. `TimeoutError` (from the **attempt** timeout) → retryable.
6. Node network error codes → retryable: `ECONNRESET`, `ECONNREFUSED`, `ETIMEDOUT`, `EPIPE`, `EAI_AGAIN`,
   `ENOTFOUND`, `EHOSTUNREACH`, `ENETUNREACH`, `EBUSY`.
7. HTTP-ish `status` on the error → retryable iff `408`, `429`, `500`, `502`, `503`, `504`.
8. Everything else → **not** retryable (fail fast; a `TypeError` in our own code must not be retried 3×).

**`Retry-After`.** If the error carries `retryAfterMs`, the retry loop uses `max(computedDelay, retryAfterMs)` for
that sleep, still capped by `maxDelayMs` and still subject to the budget check. Servers know better than our
backoff curve.

**Non-idempotent operations.** Retrying a non-idempotent write can double-apply it. Rule:

- Every operation descriptor carries `idempotent: boolean` (Wave 2 / R3 to place this on the capability descriptor).
- When `idempotent === false`, the retry policy **is not applied at all** unless the call supplies an
  `idempotencyKey`, in which case retries are permitted and the key is forwarded to the provider.
- A **connection-level** failure where the request provably never left the client (`ECONNREFUSED`, `ENOTFOUND`,
  `EAI_AGAIN`, or a request-not-yet-written state) is safe to retry even for non-idempotent operations. This is an
  explicit opt-in list, `retryNonIdempotentOnConnectFailure` (default `true`), not a general exemption.
- **Never** retry a non-idempotent operation on `TimeoutError` — a timeout is exactly the case where the write may
  have succeeded invisibly.

### 1.4 Caps, budget, and the overall timeout

| Knob | Default | Meaning |
|---|---|---|
| `baseDelayMs` | `100` | delay before the first retry |
| `factor` | `2` | exponential multiplier |
| `maxDelayMs` | `30_000` | per-sleep cap, applied **before** jitter |
| `budgetMs` | `undefined` | total wall-clock ceiling for the whole retry loop |

**Order of operations is load-bearing: cap first, then jitter.** `rand() × min(cap, base × factor^(n−1))` — not
`min(cap, rand() × …)`. Capping after jittering would compress the distribution against the cap and destroy the
decorrelation the jitter exists to provide.

**Budget vs. overall timeout — these are two different things and both exist:**

- `budgetMs` (retry-loop-local) is a **cooperative** check: before sleeping, if `elapsed + delay >= budgetMs`, the
  loop stops *now* and surfaces the accumulated error, rather than sleeping into a deadline it cannot meet. This
  avoids the classic "slept 8s, woke up, immediately got killed by the overall timeout" waste.
- The **overall timeout** (§2, sitting outside the retry loop) is **pre-emptive** and authoritative. It aborts
  mid-sleep via the signal.

Because the total timeout is outside the loop, `budgetMs` defaults to `undefined` and Wave 2 should derive it from
the remaining deadline when a deadline is present (`budgetMs = deadline − now()`). Both mechanisms must exist;
`budgetMs` is the optimisation, the timeout is the guarantee.

### 1.5 Cancellation mid-backoff

The backoff sleep must be interruptible. A `setTimeout` that is not cleared on abort holds the Node event loop open
for its full duration — **verified empirically**: an uncleared 300 ms timer kept the process alive for exactly 300 ms.
With a 30 s cap that is a 30 s hang on shutdown. See `sleep()` in §5.1.

### 1.6 Error surfaced when everything fails

| Situation | Thrown |
|---|---|
| 1 attempt configured / non-retryable error / predicate says stop after 1 failure | the **original error**, unwrapped |
| ≥2 attempts all failed | `RetryExhaustedError` with `.attempts`, `.errors: unknown[]` (all of them, in order), and `.cause` = **last** error |
| Aborted (caller signal) at any point, including mid-sleep | `CancelledError` — **never** wrapped in `RetryExhaustedError` |

Rationale for aggregate-**and**-last: the last error is what a caller usually wants (`.cause` gives it, and Node's
default error printing follows `cause`), but the earlier errors are frequently the diagnostic ones — the first
failure is often a real error and the later ones are `ECONNREFUSED` from a now-dead process. Discarding them is a
debuggability regression. We keep both. Not `AggregateError`: its `.errors` is standard but it has no `.cause` slot
convention and its message formatting is poor; a named subclass is clearer and costs nothing.

Wrapping a *single* error in an aggregate is user-hostile (`maxAttempts: 1` should behave exactly like no retry
wrapper at all), hence the special case.

### 1.7 Edge cases

| Case | Required behaviour |
|---|---|
| `maxAttempts: 0` | `RangeError` at construction, not at call time |
| `maxAttempts: 1` | exactly 1 invocation, 0 sleeps, original error unwrapped |
| Success on the final attempt | resolves normally; **no** error thrown; `attempt` count == `maxAttempts` |
| All attempts fail | `RetryExhaustedError` (see §1.6) |
| Abort **during** sleep | `CancelledError`, timer cleared, no further invocation |
| Abort **before** first attempt | `CancelledError`, **0** invocations |
| Predicate throws | treat as "not retryable", propagate the *original* error, and attach the predicate error as `.cause` on a wrapper — never let a buggy predicate mask the real failure |
| `maxDelayMs < baseDelayMs` | `maxDelayMs` wins (cap always applies) |
| Non-`Error` throw (`throw 'x'`) | preserved verbatim in `.errors`; predicate must tolerate `unknown` |

---

## 2. Timeout

### 2.1 Node 22 API surface — **verified empirically on v22.22.2**

| API | Status in Node 22 | Verified behaviour |
|---|---|---|
| `new AbortController()` / `.abort(reason)` | Stable (reason since v17.2.0) | — |
| `AbortSignal.abort(reason)` | Stable (v15.12.0) | returns an already-aborted signal |
| `AbortSignal.timeout(ms)` | Stable (v17.3.0) | reason is `DOMException`, **`name === 'TimeoutError'`, `code === 23`**, message `"The operation was aborted due to timeout"` |
| `AbortSignal.any(signals)` | Stable (v20.3.0) | aborts **synchronously** if any input is already aborted; **propagates the originating signal's `reason` by identity** (`any.reason === sentinel` — confirmed `true`) |
| `signal.throwIfAborted()` | Stable (v17.3.0) | throws `signal.reason` **as-is** (a raw string reason throws a string) |
| `signal.reason` / `.aborted` / `'abort'` event / `.onabort` | Stable | `.abort()` with no argument ⇒ `DOMException`, **`name === 'AbortError'`, `code === 20`** |

**Two findings that change the design:**

1. **`AbortSignal.timeout()` uses an unref'd timer — it does not keep the event loop alive.** Verified: a script whose
   only pending work was `AbortSignal.timeout(1)` exited *before* the signal fired. This is usually desirable in an
   app, but it makes `AbortSignal.timeout()` **untestable under fake timers** and unobservable in isolation — a
   direct conflict with constraint C3.
2. **`AbortSignal.timeout(0)` does not abort synchronously.** `AbortSignal.timeout(0).aborted === false` immediately
   after construction; it fires on a later macrotask. So `timeoutMs: 0` cannot be delegated to it.

**Decision: do not use `AbortSignal.timeout()` in the core timeout path.** Use a manual `AbortController` plus the
**injected** `setTimeout`/`clearTimeout`. `AbortSignal.timeout()` may be offered in the public convenience API for
users who want it. `AbortSignal.any()` *is* acceptable for pure signal composition (no timer involved), but the core
wrapper wires listeners manually so it can remove them deterministically in `finally`.

Node does **not** emit `MaxListenersExceededWarning` for `AbortSignal` by default (`defaultMaxListeners` has no effect
on `AbortSignal` instances), so listener accumulation on a long-lived caller signal is silent. That makes explicit
`removeEventListener` in `finally` mandatory rather than merely tidy.

### 2.2 The leak, precisely characterised

The common claim that `Promise.race` causes unhandled rejections is **wrong**, and we verified it:

```js
Promise.race([Promise.resolve('winner'), rejectsLaterPromise])   // → NO unhandled rejection
```

`Promise.race` subscribes to *every* input, so the loser's rejection is already handled. The **real** defects of the
naive race are:

1. **Timer leak.** The `setTimeout` is never cleared, so the event loop is held open for the full timeout even when
   the operation resolved in 1 ms. Verified: 300 ms timer ⇒ process exited at exactly 300 ms.
2. **No cancellation propagation.** The underlying work keeps running (and keeps holding a connection, a breaker
   half-open slot, a rate-limit token).
3. Building the rejecting timeout promise on a path where it is *not* raced **does** produce an unhandled rejection —
   verified. So the timer promise must never be constructed speculatively.

The implementation in §5.2 fixes all three: `clearTimeout` in `finally`, an `AbortSignal` handed to the operation,
and a rejection handler attached to the inner promise unconditionally (`.then(resolve, reject)`), so a late rejection
after the timeout is absorbed silently — **verified: no unhandled rejection**.

### 2.3 Distinguishing caller-abort from timeout-abort

Two distinct error types, both carrying a stable `code`:

| Cause | Error | `name` | `code` |
|---|---|---|---|
| Our timer fired | `TimeoutError` | `'TimeoutError'` | `ERR_INTERLAYER_TIMEOUT` |
| Caller's signal aborted | `CancelledError` | `'CancelledError'` | `ERR_INTERLAYER_CANCELLED` |

The wrapper keeps a private `timedOut: boolean` latch set **before** it calls `ac.abort()`, so the abort listener can
tell which of the two happened without inspecting `reason`. `CancelledError.cause` is set to the caller's original
`signal.reason`, preserving whatever the caller passed to `abort()`.

This distinction is semantically essential, not cosmetic: `TimeoutError` is retryable by default, `CancelledError`
never is (§1.3).

### 2.4 Per-attempt vs. overall

Both exist; see §5.6. `attemptTimeoutMs` bounds one provider call. `totalTimeoutMs` bounds the whole logical
operation including all retries and all backoff sleeps. Defaults `10_000` / `30_000` — matching Microsoft's standard
resilience handler, whose attempt timeout is likewise 10 s.

### 2.5 Edge cases

| Case | Required behaviour |
|---|---|
| Signal already aborted at entry | reject `CancelledError` **immediately**, do **not** invoke the operation, do **not** create a timer |
| `timeoutMs === 0` | reject `TimeoutError` immediately, operation **not** invoked. (Do *not* delegate to `AbortSignal.timeout(0)`, which is async — §2.1.) |
| `timeoutMs === Infinity` | no timer at all; pass the caller's signal straight through |
| Timeout fires in the same tick as resolution | **first settlement wins, deterministically.** The promise is already settled, so the later `reject` is a no-op. Under fake timers this must be exercised in both orders. |
| Operation rejects *after* the timeout already fired | swallowed; no unhandled rejection (verified) |
| Operation ignores the signal and runs forever | the wrapper still rejects on time; the orphaned work is the operation's problem, but the wrapper must not hold a timer for it |
| Negative / `NaN` `timeoutMs` | `RangeError` at construction |

---

## 3. Circuit breaker

### 3.1 States and transitions

```
                 failureRatio breached AND total >= minimumThroughput
      ┌──────────────────────────────────────────────────────────────┐
      │                                                              ▼
 ┌─────────┐                                                    ┌─────────┐
 │ CLOSED  │                                                    │  OPEN   │
 │ pass    │◄──── halfOpenSuccessesToClose successes ────┐      │ fail    │
 │ through │                                             │      │ fast    │
 └─────────┘                                        ┌────┴──────┴─────────┘
                                                    │  HALF_OPEN  │  ▲
                                                    │ ≤ N trials  │  │
                                                    └──────┬──────┘  │
                                                           │         │
                                    any single failure ────┴─────────┘
      OPEN → HALF_OPEN when  now() - openedAt >= resetMs   (lazy, on next call)
```

| From | Trigger | To |
|---|---|---|
| `CLOSED` | after **any** recorded outcome: `total >= minimumThroughput` **and** `failures / total >= failureRatio` | `OPEN` |
| `CLOSED` | outcome recorded, condition not met | `CLOSED` |
| `OPEN` | a call arrives and `now() − openedAt >= resetMs` | `HALF_OPEN`, then the call proceeds as a trial |
| `OPEN` | a call arrives and `now() − openedAt < resetMs` | stays `OPEN`, throw `CircuitOpenError` |
| `HALF_OPEN` | trial succeeds, and cumulative half-open successes `>= halfOpenSuccessesToClose` | `CLOSED` (window cleared) |
| `HALF_OPEN` | trial succeeds, threshold not yet reached | stays `HALF_OPEN` |
| `HALF_OPEN` | **any** trial fails | `OPEN`, `openedAt = now()` (full `resetMs` restarts) |
| `HALF_OPEN` | a call arrives while trial slots are exhausted | throw `CircuitOpenError` |

The `OPEN → HALF_OPEN` transition is **lazy** (evaluated when a call arrives), not driven by a background timer.
This is deliberate: a timer would keep the event loop alive and would require cleanup on disposal. Resilience4j calls
the timer-driven variant `automaticTransitionFromOpenToHalfOpen` and defaults it to **`false`** for the same reason.
Consequence to document: `breaker.state` is only accurate when read through a method that takes `now()`; a bare
getter can report a stale `OPEN`. The implementation in §5.3 exposes `currentState(now)` for this reason.

### 3.2 Failure counting — options

| Approach | Trips on | Pros | Cons |
|---|---|---|---|
| Consecutive failures | N in a row | trivial; zero memory; obvious to test | blind to a 45 %-failing provider that never fails twice consecutively; a single success resets it |
| Count-based sliding window | last N calls | bounded memory; no time dependence | a low-traffic breaker holds stale outcomes for hours — it can trip on failures from yesterday |
| **Rolling time-window ratio + min-throughput** | failures/total ≥ ratio within `windowMs`, gated on `total ≥ minimumThroughput` | reflects *current* health; time-bounded so old data expires; the guard prevents tripping on 1–2 requests | needs a clock (injected anyway) and bucket bookkeeping |

**Recommendation: rolling time-window failure ratio with a minimum-throughput guard.**

The guard is the whole point and must not be optional: without it, `failureRatio: 0.5` trips on the **first single
failed request** (1/1 = 100 % ≥ 50 %), which is catastrophic behaviour for a low-traffic client and the single most
common circuit-breaker misconfiguration. `minimumThroughput` must be **≥ 2** (Polly enforces exactly this floor) and
we default it to **10**.

Implementation: a ring of fixed-width buckets (`bucketMs: 1_000`, `windowMs: 30_000` ⇒ 30 buckets). Bucket
granularity bounds memory and cost at O(windowMs/bucketMs) regardless of throughput, and expiry is a cheap
`shift()` of buckets older than the cutoff. Exact per-call timestamp lists are rejected: unbounded memory under load.

Consecutive-count mode is still offered as `mode: 'consecutive'` for users who want it (cockatiel ships both as
`ConsecutiveBreaker`/`SamplingBreaker`), but is not the default.

### 3.3 Ecosystem defaults surveyed (all read from primary sources)

| Library | Failure criterion | Min throughput | Window | Open duration | Half-open trials |
|---|---|---|---|---|---|
| resilience4j (`CircuitBreakerConfig.java`) | 50 % failure rate | `minimumNumberOfCalls` **100** | `slidingWindowSize` **100**, `COUNT_BASED` | **60 s** | **10** permitted calls |
| Polly v8 (`CircuitBreakerStrategyOptions`) | `FailureRatio` **0.1** | `MinimumThroughput` **100** (min 2) | `SamplingDuration` **30 s** | `BreakDuration` **5 s** | 1 |
| opossum | `errorThresholdPercentage` **50** | `volumeThreshold` | 10 s rolling / 10 buckets | `resetTimeout` | 1 |
| cockatiel | `ConsecutiveBreaker(n)` or `SamplingBreaker{threshold,duration,minimumRps}` | `minimumRps` | user-set | `halfOpenAfter` | 1 |
| **interlayer** | **0.5** | **10** | **30 s / 1 s buckets** | **10 s** | **1 concurrent, 1 success to close** |

We deliberately drop `minimumThroughput` from 100 to **10**: Polly's and resilience4j's defaults are tuned for a
high-QPS service mesh, where 100 calls arrive in well under a second. An interop library is frequently used at
single-digit QPS, where a threshold of 100 means the breaker **never trips**. 10 is high enough that a couple of
unlucky failures cannot trip it, low enough that a genuinely dead provider is detected within seconds.

### 3.4 HALF_OPEN policy

- **Concurrency: `halfOpenMaxConcurrent: 1`.** Exactly one trial call is admitted at a time. Calls arriving while the
  trial slot is occupied get `CircuitOpenError`. This is the point of half-open: send *one probe*, not a thundering
  herd, at a service you believe is sick.
- **Promotion: `halfOpenSuccessesToClose: 1`.** One successful trial closes the circuit and clears the window.
  Matches opossum and cockatiel. The cost of closing optimistically is low — the next failure re-opens immediately —
  whereas requiring several sequential successes multiplies recovery latency by the round-trip time.
- **Demotion: any single failure re-opens**, and `openedAt` is reset to `now()`, so the full `resetMs` cooldown
  restarts. There is no "3 strikes in half-open".
- On both `CLOSED` and `OPEN` entry the rolling window is **cleared**. Carrying pre-outage failure counts into a
  freshly closed breaker would re-trip it on the first hiccup.

> **Design bug found during validation.** An initial implementation evaluated the trip condition **only inside the
> failure path**. With `minimumThroughput: 10` and a 5-fail-then-5-succeed sequence, `total` was still 5 when the last
> failure arrived, so the condition was never re-checked and the breaker silently failed to trip at a true 50 % rate.
> **The trip condition must be evaluated after every recorded outcome, success and failure alike** — which is what
> resilience4j does. Fixed and re-verified (§5.7).

### 3.5 Clock

All timing goes through an injected `now(): number` returning **monotonic milliseconds** (`performance.now()` in
production). Wave 3 injects a counter. No `Date.now()`, no `setTimeout`-driven state transitions.

### 3.6 Error thrown when open

```ts
class CircuitOpenError extends Error {
  name = 'CircuitOpenError';
  code = 'ERR_INTERLAYER_CIRCUIT_OPEN';
  readonly openUntil: number;   // now() timestamp when HALF_OPEN becomes reachable
  readonly provider?: string;
}
```

`openUntil` lets the routing layer (R3) make an informed choice about which provider to fall back to, and lets a
caller compute a sensible `Retry-After`. Thrown **synchronously-ish** (as a rejected promise, without invoking the
operation) so fail-fast is genuinely fast.

### 3.7 Edge cases

| Case | Required behaviour |
|---|---|
| Concurrent calls racing an `OPEN → HALF_OPEN` transition | exactly `halfOpenMaxConcurrent` are admitted; the rest get `CircuitOpenError`. Node is single-threaded, so the admission check + counter increment is atomic **provided it happens synchronously before any `await`** — this is a hard implementation requirement |
| Breaker opens while a call is in flight | the in-flight call is **not** cancelled; its eventual outcome is **discarded**, not recorded |
| Stale settlement after a state change | guarded by a **generation counter** captured at admission and compared at settlement. Without it, a slow half-open trial that resolves after the breaker already re-opened would wrongly close the circuit |
| Success and failure arriving "simultaneously" in half-open | impossible to be truly simultaneous in Node; they are serialised by the microtask queue. With `halfOpenMaxConcurrent: 1` only one trial exists, so the question is moot; with N>1 the **first** settlement to arrive decides, and the generation counter voids the rest |
| Error excluded by `isFailure` (e.g. HTTP 404) | recorded as a **success** for breaker purposes, and rethrown. A breaker must not trip on the caller's bad input |
| `CircuitOpenError` itself | never recorded (the operation never ran) |
| Clock jumps backwards | window buckets are keyed on `now()`; a backwards jump must not resurrect expired buckets — monotonic clock makes this unreachable, but the guard stays |
| Exact boundary `now() − openedAt === resetMs` | transitions to `HALF_OPEN` (`>=`, not `>`) |
| Ratio exactly equal to `failureRatio` | trips (`>=`, not `>`) |

---

## 4. Rate limiter

### 4.1 Options considered

| Algorithm | Burst | Memory | Boundary accuracy | Can compute "wait until allowed"? | Verdict |
|---|---|---|---|---|---|
| **Token bucket** | **yes, up to `capacity`** | O(1) — 2 numbers | exact | **yes, in closed form** | **CHOSEN** |
| Leaky bucket (queue) | no — perfectly smooth output | O(queue) | exact | yes | Rejected: forbids bursts |
| Fixed window | yes, but 2× at the boundary | O(1) | **poor** — 2× the limit across a window edge | yes | Rejected: boundary bug |
| Sliding window log | no | **O(n)** — a timestamp per request | exact | yes | Rejected: unbounded memory |
| Sliding window counter | partial | O(1) — 2 counters | approximate (Cloudflare measured ~0.003 % error over 400 M requests) | approximate | Rejected: approximation buys nothing in-process |

**Recommendation: token bucket.**

Reasoning specific to an *in-process, client-side* limiter:

- **Bursts are a feature, not a bug.** Real client workloads are bursty (a page load fires 8 calls at once). Providers
  size their quotas expecting this. Leaky bucket's perfectly smooth output would serialise that burst for no benefit.
- **O(1) memory and O(1) per call**, with no per-request allocation — sliding window log's per-request timestamp array
  is unacceptable in a hot path.
- **Closed-form "time until N tokens available"** (`msUntil`, §5.4). This is what makes queue-and-wait implementable
  with a *single* timer per waiter instead of polling. Sliding-window variants can only approximate this.
- The sliding-window-counter approximation exists to save memory in a **distributed** limiter with millions of keys.
  In-process with a handful of provider keys, we have no memory pressure to trade accuracy for.

### 4.2 Exact refill math

State: `tokens: number` (fractional), `last: number` (monotonic ms).
Config: `capacity: number` (max burst), `refillPerSec: number`.

```
refill(t):
    if t <= last:            # monotonic clock guard; never mint tokens on a backwards jump
        last = t
        return
    elapsedSec = (t - last) / 1000
    last = t                                    # advance FIRST, unconditionally
    if refillPerSec <= 0: return                # rate 0 => never refills
    tokens = min(capacity, tokens + elapsedSec * refillPerSec)
```

**Two anti-drift rules, both load-bearing:**

1. **Accrue in continuous fractional tokens, never in whole-token steps.** `tokens` is a float. A design that only
   adds tokens once a whole token has accrued loses the remainder on every call and drifts systematically slow —
   at 10 calls/s against a 10 tok/s bucket it would deliver ~0 tokens forever.
2. **Advance `last` to the observed `t` on every refill, before any early return.** Do *not* advance it by a computed
   whole-token quantum (`last += k / rate`) and do *not* skip the update when `rate <= 0`. Both variants accumulate
   error. Verified: 1000 successive 1 ms ticks against a 10 tok/s bucket accrue exactly 10 tokens (capped at
   capacity), with zero drift.

Time-until-available (used by the queue, closed form, no polling):

```
msUntil(n):
    refill(now())
    if tokens >= n: return 0
    if refillPerSec <= 0: return Infinity
    return ceil(((n - tokens) / refillPerSec) * 1000)
```

`ceil` is deliberate: rounding down would wake the waiter a fraction of a millisecond early, it would find
insufficient tokens, and it would re-queue — a busy-wait loop. Round up, always.

### 4.3 Exhaustion behaviour

| Option | For a client-side limiter |
|---|---|
| Reject immediately | Surprising. The user configured "10/s" as a *pacing* instruction, not as an error condition. Forces every call site to implement its own retry loop. |
| **Queue and wait** | Matches intent: the limiter *shapes* traffic. **CHOSEN as default.** |

**Recommendation: `onExhaustion: 'wait'` (default), with `'reject'` available.** Guard rails, all mandatory:

- **Bounded queue**: `maxQueueDepth: 100`. On overflow, reject immediately with `RateLimitExceededError` carrying
  `retryAfterMs`. An unbounded queue is a memory leak and converts a rate problem into an OOM.
- **Bounded wait**: `maxQueueWaitMs: 30_000`. A waiter that would need longer than this is rejected at enqueue time
  (computable up front from `msUntil` + queue position), rather than being admitted and later disappointed.
- **Ordering: strict FIFO.** Fairness is more valuable than throughput here, and FIFO is the only ordering that is
  deterministically testable. Explicitly **no** priority lanes in v1 — they invite starvation.
- **Head-of-line rule**: a waiter requesting `n > capacity` can never be satisfied ⇒ reject at enqueue with
  `RangeError`. Otherwise it blocks the queue forever.
- **Abort while queued**: the waiter's promise rejects with `CancelledError`, it is removed from the queue, its timer
  is cleared, **and no tokens are consumed**. The next waiter must be re-evaluated immediately — dropping a waiter
  can unblock the one behind it.
- **Wake-up is timer-driven, not polled**: one timer for the head of the queue, rearmed on each dequeue.

### 4.4 Clock source

| Source | Monotonic | Resolution | Verdict |
|---|---|---|---|
| `Date.now()` | **no** — NTP steps and DST can move it backwards | 1 ms | **Rejected** |
| `performance.now()` | **yes** (verified monotonic over 200 000 consecutive samples) | sub-ms float | **CHOSEN** |
| `process.hrtime.bigint()` | yes | ns | Rejected: `bigint` arithmetic is slower, mixes badly with float token math, and needs conversion everywhere |

Monotonicity is not academic: with `Date.now()`, an NTP correction that steps the clock **forward** mints a huge
batch of tokens instantly (a burst that violates the provider's quota — the exact failure the limiter exists to
prevent), and a backwards step stalls the limiter. `performance.now()` is monotonic by specification and is what the
token bucket, the breaker, and the retry budget all share as `now()`.

The backwards guard in `refill` stays anyway, because tests inject clocks and a test may legitimately rewind one.

### 4.5 Edge cases

| Case | Required behaviour |
|---|---|
| Burst at `t = 0` | bucket starts **full**: exactly `capacity` immediate grants, the next one waits |
| `capacity: 0` | never grants; `tryRemove` always `false`; with `'wait'`, reject at enqueue (`n > capacity`) |
| `refillPerSec: 0` | never refills; `msUntil` returns `Infinity`; `'wait'` mode must reject rather than schedule an infinite timer |
| Clock jumps backwards | `last` is advanced, **no** tokens minted; limiter recovers on the next forward tick (verified) |
| Clock jumps far forwards | tokens clamp at `capacity` — a jump cannot mint more than one full burst |
| Exact boundary | at exactly `msUntil` elapsed, the token **is** available (`tokens >= n`, `>=` not `>`) |
| `n > capacity` | `RangeError` — unsatisfiable |
| Fractional `n` | permitted (weighted costs); the math is float throughout |
| Queue drained by aborts | the head timer must be re-armed or cleared; no orphan timers |

---

## 5. Concrete implementations (all executed and validated)

### 5.0 Shared types

```ts
export interface Clock { now(): number; }                       // monotonic ms
export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}
export type Random = () => number;                              // [0, 1)

export class TimeoutError extends Error {
  override readonly name = 'TimeoutError';
  readonly code = 'ERR_INTERLAYER_TIMEOUT';
  constructor(readonly timeoutMs: number) { super(`Operation timed out after ${timeoutMs}ms`); }
}
export class CancelledError extends Error {
  override readonly name = 'CancelledError';
  readonly code = 'ERR_INTERLAYER_CANCELLED';
  constructor(reason?: unknown) { super('Operation was cancelled', { cause: reason }); }
}
export class RetryExhaustedError extends Error {
  override readonly name = 'RetryExhaustedError';
  readonly code = 'ERR_INTERLAYER_RETRY_EXHAUSTED';
  constructor(readonly attempts: number, readonly errors: readonly unknown[]) {
    super(`All ${attempts} attempt(s) failed`, { cause: errors[errors.length - 1] });
  }
}
export class CircuitOpenError extends Error {
  override readonly name = 'CircuitOpenError';
  readonly code = 'ERR_INTERLAYER_CIRCUIT_OPEN';
  constructor(readonly openUntil: number) { super('Circuit breaker is open'); }
}
export class RateLimitExceededError extends Error {
  override readonly name = 'RateLimitExceededError';
  readonly code = 'ERR_INTERLAYER_RATE_LIMITED';
  constructor(readonly retryAfterMs: number) { super('Rate limit exceeded'); }
}
```

### 5.1 Interruptible sleep

```ts
export function sleep(ms: number, signal?: AbortSignal, timers: Timers = globalThis): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) return reject(new CancelledError(signal.reason));
    const handle = timers.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    function onAbort(): void {
      timers.clearTimeout(handle);              // MUST clear, or the loop is held open (verified)
      reject(new CancelledError(signal!.reason));
    }
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}
```

### 5.2 Timeout wrapper — the tricky one

```ts
export function withTimeout<T>(
  fn: (signal: AbortSignal) => T | Promise<T>,
  timeoutMs: number,
  outer?: AbortSignal,
  timers: Timers = globalThis,
): Promise<T> {
  // Entry guards, before any timer or listener is created.
  if (outer?.aborted)          return Promise.reject(new CancelledError(outer.reason));
  if (timeoutMs === 0)         return Promise.reject(new TimeoutError(0));
  if (!Number.isFinite(timeoutMs)) {
    return Promise.resolve().then(() => fn(outer ?? new AbortController().signal));
  }

  const ac = new AbortController();
  let timedOut = false;                                   // latch: distinguishes the two abort causes

  const handle = timers.setTimeout(() => {
    timedOut = true;                                      // set BEFORE abort() so the listener sees it
    ac.abort(new TimeoutError(timeoutMs));
  }, timeoutMs);

  const onOuterAbort = () => ac.abort(outer!.reason);
  outer?.addEventListener('abort', onOuterAbort, { once: true });

  const settled = new Promise<T>((resolve, reject) => {
    ac.signal.addEventListener('abort', () => {
      reject(timedOut ? new TimeoutError(timeoutMs) : new CancelledError(outer?.reason));
    }, { once: true });

    // `.then(resolve, reject)` attaches a rejection handler unconditionally, so a LATE rejection
    // arriving after the timeout is absorbed instead of becoming an unhandled rejection. Verified.
    Promise.resolve().then(() => fn(ac.signal)).then(resolve, reject);
  });

  return settled.finally(() => {
    timers.clearTimeout(handle);                          // no timer leak on the fast path
    outer?.removeEventListener('abort', onOuterAbort);    // no listener accumulation (Node does not warn)
  });
}
```

Why not `AbortSignal.any([outer, AbortSignal.timeout(ms)])`: it is elegant but (a) the timeout timer is unref'd and
therefore invisible to fake timers, breaking C3; (b) the timer cannot be cleared early; (c) `timeoutMs: 0` does not
abort synchronously. All three verified in §2.1.

### 5.3 Circuit breaker

```ts
type BreakerState = 'CLOSED' | 'OPEN' | 'HALF_OPEN';
interface Bucket { t: number; s: number; f: number; }

export class CircuitBreaker {
  private state: BreakerState = 'CLOSED';
  private openedAt = 0;
  private buckets: Bucket[] = [];
  private halfOpenInFlight = 0;
  private halfOpenSucc = 0;
  private generation = 0;                      // voids settlements from a superseded state

  constructor(private readonly o: {
    now(): number; windowMs: number; bucketMs: number;
    failureRatio: number; minimumThroughput: number; resetMs: number;
    halfOpenMaxConcurrent: number; halfOpenSuccessesToClose: number;
    isFailure(e: unknown): boolean;
  }) {}

  currentState(t = this.o.now()): BreakerState {
    if (this.state === 'OPEN' && t - this.openedAt >= this.o.resetMs) this.toHalfOpen();  // lazy, >=
    return this.state;
  }

  async execute<T>(fn: () => Promise<T>): Promise<T> {
    const t = this.o.now();
    const st = this.currentState(t);

    // Admission check + counter mutation are synchronous: no `await` may appear between them.
    if (st === 'OPEN') throw new CircuitOpenError(this.openedAt + this.o.resetMs);
    if (st === 'HALF_OPEN') {
      if (this.halfOpenInFlight >= this.o.halfOpenMaxConcurrent) {
        throw new CircuitOpenError(this.openedAt + this.o.resetMs);
      }
      this.halfOpenInFlight++;
    }

    const gen = this.generation;
    try {
      const r = await fn();
      this.onSuccess(gen);
      return r;
    } catch (e) {
      if (this.o.isFailure(e)) this.onFailure(gen); else this.onSuccess(gen);
      throw e;
    }
  }

  private onSuccess(gen: number): void {
    if (gen !== this.generation) return;                       // stale settlement — discard
    if (this.state === 'HALF_OPEN') {
      this.halfOpenInFlight--; this.halfOpenSucc++;
      if (this.halfOpenSucc >= this.o.halfOpenSuccessesToClose) this.close();
      return;
    }
    if (this.state === 'CLOSED') { this.bucket().s++; this.evaluate(); }
  }

  private onFailure(gen: number): void {
    if (gen !== this.generation) return;
    if (this.state === 'HALF_OPEN') { this.halfOpenInFlight--; this.open(); return; }
    if (this.state === 'CLOSED')    { this.bucket().f++; this.evaluate(); }
  }

  /** MUST run after EVERY recorded outcome, success and failure alike. See §3.4. */
  private evaluate(): void {
    const t = this.o.now(); this.prune(t);
    let s = 0, f = 0;
    for (const b of this.buckets) { s += b.s; f += b.f; }
    const total = s + f;
    if (total >= this.o.minimumThroughput && f / total >= this.o.failureRatio) this.open();
  }

  private prune(t: number): void {
    const cutoff = t - this.o.windowMs;
    while (this.buckets.length && this.buckets[0]!.t <= cutoff) this.buckets.shift();
  }
  private bucket(): Bucket {
    const t = this.o.now();
    const id = Math.floor(t / this.o.bucketMs) * this.o.bucketMs;
    this.prune(t);
    let b = this.buckets[this.buckets.length - 1];
    if (!b || b.t !== id) { b = { t: id, s: 0, f: 0 }; this.buckets.push(b); }
    return b;
  }
  private open():       void { this.state = 'OPEN'; this.openedAt = this.o.now(); this.reset(); }
  private close():      void { this.state = 'CLOSED'; this.reset(); }
  private toHalfOpen(): void { this.state = 'HALF_OPEN'; this.reset(); }
  private reset(): void {
    this.buckets = []; this.halfOpenSucc = 0; this.halfOpenInFlight = 0; this.generation++;
  }
}
```

### 5.4 Token bucket

```ts
export class TokenBucket {
  private tokens: number;
  private last: number;

  constructor(private readonly o: {
    capacity: number; refillPerSec: number; now(): number;
  }) {
    this.tokens = o.capacity;      // starts FULL: a burst of `capacity` is allowed at t=0
    this.last   = o.now();
  }

  private refill(): void {
    const t = this.o.now();
    if (t <= this.last) { this.last = t; return; }        // backwards clock: advance, mint nothing
    const elapsedSec = (t - this.last) / 1000;
    this.last = t;                                        // advance FIRST, unconditionally
    if (this.o.refillPerSec <= 0) return;
    this.tokens = Math.min(this.o.capacity, this.tokens + elapsedSec * this.o.refillPerSec);
  }

  tryRemove(n = 1): boolean {
    this.refill();
    if (this.tokens >= n) { this.tokens -= n; return true; }   // >= not >: exact boundary grants
    return false;
  }

  msUntil(n = 1): number {
    this.refill();
    if (this.tokens >= n) return 0;
    if (this.o.refillPerSec <= 0) return Infinity;
    return Math.ceil(((n - this.tokens) / this.o.refillPerSec) * 1000);   // ceil: never wake early
  }
}
```

### 5.5 Retry loop

```ts
export async function retry<T>(
  fn: (ctx: { attempt: number; signal?: AbortSignal }) => Promise<T>,
  o: {
    maxAttempts: number;                                  // TOTAL, including the first
    isRetryable(e: unknown, attempt: number): boolean;
    delayFor(retryIndex: number): number;                 // retryIndex is 1-based
    now(): number; signal?: AbortSignal; budgetMs?: number; timers?: Timers;
  },
): Promise<T> {
  if (!Number.isInteger(o.maxAttempts) || o.maxAttempts < 1) {
    throw new RangeError('maxAttempts must be an integer >= 1');
  }
  const start = o.now();
  const errors: unknown[] = [];

  for (let attempt = 1; ; attempt++) {
    if (o.signal?.aborted) throw new CancelledError(o.signal.reason);
    try {
      return await fn({ attempt, signal: o.signal });
    } catch (err) {
      if (err instanceof CancelledError) throw err;       // never swallowed, never aggregated
      errors.push(err);
      if (attempt >= o.maxAttempts) break;                // TOTAL-attempts semantics
      if (!o.isRetryable(err, attempt)) break;
      const delay = o.delayFor(attempt);
      if (o.budgetMs !== undefined) {
        const remaining = o.budgetMs - (o.now() - start);
        if (remaining <= 0 || delay >= remaining) break;  // don't sleep past a deadline we can't meet
      }
      await sleep(delay, o.signal, o.timers);             // throws CancelledError if aborted mid-sleep
    }
  }
  throw errors.length === 1 ? errors[0] : new RetryExhaustedError(errors.length, errors);
}

export function computeDelay(
  retryIndex: number,                                     // 1-based: 1 = the wait before attempt 2
  o: { strategy: 'fixed' | 'exponential' | 'full' | 'equal';
       baseDelayMs: number; factor: number; maxDelayMs: number },
  rand: Random,
): number {
  const exp = Math.min(o.maxDelayMs, o.baseDelayMs * Math.pow(o.factor, retryIndex - 1));  // CAP FIRST
  switch (o.strategy) {
    case 'fixed':       return Math.min(o.maxDelayMs, o.baseDelayMs);
    case 'exponential': return exp;
    case 'full':        return rand() * exp;                        // [0, exp)
    case 'equal':       return exp / 2 + rand() * (exp / 2);        // [exp/2, exp]
  }
}

// Decorrelated is stateful: the caller threads `prev`, seeded with baseDelayMs.
export function decorrelated(prev: number, o: { baseDelayMs: number; maxDelayMs: number }, rand: Random): number {
  return Math.min(o.maxDelayMs, o.baseDelayMs + rand() * (prev * 3 - o.baseDelayMs));
}
```

### 5.6 Canonical nesting order — **DECIDED, do not guess**

```
  ┌─ TotalTimeout        (outermost)  overall deadline for the whole logical operation
  │  ┌─ Retry                         the retry loop
  │  │  ┌─ RateLimit                  every physical attempt spends a token
  │  │  │  ┌─ CircuitBreaker          every physical attempt is recorded
  │  │  │  │  ┌─ AttemptTimeout       bounds ONE provider call
  │  │  │  │  │  └── provider call    (innermost)
```

Answers to the three questions implementers will otherwise get wrong:

| Question | Answer | Why |
|---|---|---|
| **Is the timeout per-attempt or overall?** | **Both.** `attemptTimeoutMs: 10_000` innermost, `totalTimeoutMs: 30_000` outermost. | With only a total timeout, one hung attempt eats the entire budget and no retry ever happens. With only a per-attempt timeout, `maxAttempts × attemptTimeout + backoff` is unbounded from the caller's perspective. You need both, and the per-attempt one must be strictly smaller. |
| **Do retries consume rate-limit tokens?** | **Yes.** | The limiter's job is to keep interlayer's *physical* call rate under the provider's quota. A retry is a physical call. Retry storms are precisely when quota protection matters most — a limiter that exempts retries is disabled exactly when it is needed. |
| **Does each retry's failure count toward the breaker?** | **Yes.** | The breaker is inside the loop, so 3 attempts against a dead provider record 3 failures. This is the desired behaviour: a dead provider should trip the breaker fast, not 3× slower because the loop hid the failures. |

**Why `RateLimit` is inside `Retry`, unlike Polly.** Microsoft's standard resilience handler orders its pipeline
`Bulkhead → Total Request Timeout → Retry → Circuit Breaker → Attempt Timeout` (quoted verbatim from
`HttpStandardResilienceOptions.cs`) — with the limiter **outermost**. That is correct for *their* limiter, which is a
**concurrency** limiter (a bulkhead) admitting *logical* operations. Ours is a **quota** limiter counting *physical*
calls, so it must sit inside the loop. This divergence is deliberate; it is recorded here so Wave 2 does not "fix" it
back to Polly's order. Everything else matches Microsoft's ordering exactly, including the 10 s attempt timeout.

**Why `CircuitBreaker` is inside `RateLimit`.** If the breaker were outside, a call sitting in the rate-limiter queue
would occupy a breaker in-flight slot — and in `HALF_OPEN`, where there is exactly one trial slot, a single queued
call would block all probing for the length of the queue wait. Keeping the breaker inner means its accounting covers
only real provider calls. The cost is that when the breaker is open we spend a token before discovering it; this is
mitigated because `CircuitOpenError` is **non-retryable** (§1.3), so the loop exits after one wasted token rather
than `maxAttempts` of them. Wave 2 may add a cheap non-consuming `breaker.currentState()` peek before token
acquisition as a pure optimisation — it must not change semantics.

**Boundary with R3 (routing/fallback).** The resilience pipeline above is **per-provider**. Provider
selection/fallback wraps it:

```
CallerDeadline → Routing/Fallback → [ per-provider: Retry → RateLimit → Breaker → AttemptTimeout ]
```

When routing is present, the per-provider `TotalTimeout` should be replaced by a **remaining-deadline budget** derived
from the caller's deadline, so that trying three providers cannot take 3 × `totalTimeoutMs`. `CircuitOpenError` and
`RetryExhaustedError` are the two signals the routing layer uses to move to the next provider. Flagged for the
orchestrator as the R3/R4 seam.

### 5.7 Validation performed

Both implementation files were executed under Node v22.22.2 in this environment. **26 behavioural assertions, all
passing**, covering: timeout fast path, slow-op timeout, pre-aborted signal, caller-abort vs timeout distinction,
`timeoutMs: 0`, signal forwarding, no-wait-on-fast-resolve, late-rejection absorption; exponential/fixed/full/equal
jitter values and `maxDelay` capping; token-bucket burst, exhaustion, `msUntil` exactness, 1000-tick fractional
accrual with zero drift, `capacity: 0`, `refillPerSec: 0`, backwards clock; retry total-attempts semantics,
`maxAttempts` 0/1/3, aggregate-vs-bare error surfacing, non-retryable short-circuit, abort during backoff, budget
enforcement; breaker minimum-throughput guard, exact-ratio boundary, `CircuitOpenError` with `openUntil`, `resetMs`
boundary, half-open concurrency limiting, promotion, re-open, stale-generation discard, window expiry, and
`isFailure` exclusion.

---

## 6. Testable-behaviour checklist

> **6.0 — Global rules that apply to every test below.** No wall-clock `sleep`. Inject `now()` as a mutable counter,
> inject `{ setTimeout, clearTimeout }` as a controllable fake, inject `random()` as a seeded/scripted stub. Every
> assertion below must be reachable without real time passing, with the sole exception of RT-14 and TO-13, which
> exist to prove the timers are genuinely cleared.

### Retry (RT)

1. `maxAttempts: 3`, operation always fails ⇒ the operation is invoked **exactly 3 times**.
2. `maxAttempts: 3`, operation succeeds on invocation 3 ⇒ resolves with that value; **no** error thrown.
3. `maxAttempts: 1`, operation fails ⇒ invoked **exactly once**, **0** sleeps.
4. `maxAttempts: 1`, operation fails ⇒ the **original** error is thrown, **not** `RetryExhaustedError`.
5. `maxAttempts: 0` ⇒ `RangeError` thrown at construction, before the operation is touched.
6. `maxAttempts: -1`, `1.5`, `NaN` ⇒ `RangeError`.
7. Operation succeeds on invocation 1 ⇒ invoked once, **0** sleeps, **0** timers created.
8. All attempts fail with ≥2 attempts ⇒ `RetryExhaustedError` with `.attempts === maxAttempts`,
   `.errors.length === maxAttempts`, `.errors` in **chronological order**, `.cause === last error`.
9. Predicate returns `false` on attempt 1 ⇒ invoked once, bare error propagates, **0** sleeps.
10. Predicate returns `false` on attempt 2 of 5 ⇒ invoked exactly twice.
11. Exponential, `base 100 / factor 2 / cap 30_000` ⇒ delays are exactly `[100, 200, 400, 800, 1600]`.
12. `maxDelayMs: 1000` with the same config ⇒ delays are `[100, 200, 400, 800, 1000, 1000, …]` (cap holds).
13. Fixed strategy ⇒ every delay is exactly `baseDelayMs`, regardless of retry index.
14. Full jitter with `random() → 0` ⇒ delay `0`; with `random() → 0.999…` ⇒ delay `< exp(n)`; with
    `random() → 0.5` ⇒ delay exactly `exp(n)/2`. Assert the **cap is applied before jitter** by setting
    `maxDelayMs` low and checking the jittered value never exceeds it.
15. Equal jitter with `random() → 0` ⇒ exactly `exp(n)/2`; with `random() → 1` ⇒ exactly `exp(n)`.
16. Decorrelated: every value `>= baseDelayMs` and `<= maxDelayMs`; the sequence is a function of the previous value
    (feeding a fixed `prev` twice yields the same result).
17. Abort **before** the first attempt ⇒ `CancelledError`, operation invoked **0** times.
18. Abort **during** a backoff sleep ⇒ `CancelledError`; the pending timer is cleared (assert via the fake timer's
    outstanding-timer count reaching 0); the operation is **not** invoked again.
19. `CancelledError` from inside the operation is rethrown **unwrapped** and is **never** retried.
20. `CircuitOpenError` is **not** retried by the default predicate.
21. `budgetMs` exceeded ⇒ the loop stops early, before `maxAttempts` is reached.
22. `budgetMs` where the *next delay* would overrun the remaining budget ⇒ the loop stops **without** sleeping.
23. Error carrying `retryAfterMs: 5000` with a computed delay of 100 ⇒ actual sleep is `5000`.
24. `retryAfterMs` above `maxDelayMs` ⇒ still capped at `maxDelayMs`.
25. Non-`Error` throws (`throw 'oops'`, `throw null`) ⇒ preserved verbatim in `.errors`; no crash.
26. Non-idempotent operation without an `idempotencyKey` ⇒ retry policy **not applied**; invoked exactly once.
27. Non-idempotent operation, `ECONNREFUSED` ⇒ **is** retried (connect-failure exemption).
28. Non-idempotent operation, `TimeoutError` ⇒ **not** retried.
29. Default predicate returns `true` for each of `408/429/500/502/503/504` and each listed `E*` code.
30. Default predicate returns `false` for `400`, `401`, `403`, `404`, `422`, and for a plain `TypeError`.
31. A throwing predicate does not mask the original error.
32. Zero timers remain outstanding after every terminal state (success, exhaustion, cancellation).

### Timeout (TO)

1. Operation resolving before the timeout ⇒ resolves with its value.
2. Operation slower than the timeout ⇒ rejects with `TimeoutError`, `.name === 'TimeoutError'`,
   `.code === 'ERR_INTERLAYER_TIMEOUT'`, `.timeoutMs` equal to the configured value.
3. Caller signal already aborted at entry ⇒ `CancelledError`; operation invoked **0** times; **0** timers created.
4. Caller aborts mid-flight ⇒ `CancelledError`, **not** `TimeoutError`.
5. `CancelledError.cause` is `===` the value the caller passed to `abort(reason)`.
6. `timeoutMs: 0` ⇒ `TimeoutError` immediately; operation invoked **0** times.
7. `timeoutMs: Infinity` ⇒ no timer created; the caller's signal passes straight through to the operation.
8. `timeoutMs: -1` / `NaN` ⇒ `RangeError`.
9. The operation receives an `AbortSignal`; on timeout that signal becomes `aborted` and its `reason` is the
   `TimeoutError`.
10. Operation rejects **after** the timeout already fired ⇒ no unhandled rejection (assert via an
    `unhandledRejection` listener) and the surfaced error remains `TimeoutError`.
11. Operation resolves **after** the timeout already fired ⇒ the surfaced error remains `TimeoutError`.
12. Timeout scheduled to fire in the same tick as resolution ⇒ **first settlement wins**; test both orderings
    explicitly under fake timers and assert determinism.
13. Fast-resolving operation with a 5 000 ms timeout ⇒ the timer is cleared (assert 0 outstanding timers, and that
    the promise settles without advancing the fake clock).
14. Caller-signal listener is removed after settlement (assert the listener count on the caller signal returns to its
    prior value) — for both the success and the failure path.
15. Nested `withTimeout(withTimeout(fn, 10), 100)` ⇒ the **inner** timeout wins and surfaces.
16. Nested with the outer smaller ⇒ the outer wins.
17. Synchronously-throwing `fn` ⇒ rejects with that error (not a `TimeoutError`), and the timer is cleared.
18. `AbortSignal.timeout()` regression guard: assert `AbortSignal.timeout(x).aborted === false` immediately after
    construction, documenting why the core path does not use it.

### Circuit breaker (CB)

1. Fresh breaker is `CLOSED`; calls pass through.
2. `minimumThroughput: 10`, 9 consecutive failures ⇒ **still `CLOSED`** (the anti-trip-on-one-request guard).
3. The 10th failure ⇒ `OPEN`.
4. 10 calls, 5 fail (ratio exactly `0.5` with `failureRatio: 0.5`) ⇒ `OPEN` (`>=`, not `>`).
5. 10 calls, 4 fail (ratio `0.4`) ⇒ `CLOSED`.
6. The trip condition is re-evaluated on **successes** too: a fail-then-succeed interleaving that reaches the ratio
   only on a success still trips (the §3.4 regression).
7. While `OPEN`, a call rejects with `CircuitOpenError` and the operation is invoked **0** times.
8. `CircuitOpenError.openUntil === openedAt + resetMs`.
9. At `now - openedAt === resetMs - 1` ⇒ still `OPEN`.
10. At `now - openedAt === resetMs` exactly ⇒ `HALF_OPEN` (`>=`, not `>`).
11. In `HALF_OPEN` with `halfOpenMaxConcurrent: 1`, a second concurrent call gets `CircuitOpenError`.
12. With `halfOpenMaxConcurrent: 2`, two concurrent trials are admitted and the third is rejected.
13. A successful trial with `halfOpenSuccessesToClose: 1` ⇒ `CLOSED`.
14. With `halfOpenSuccessesToClose: 2`, one success keeps it `HALF_OPEN`; the second closes it.
15. A single failed trial in `HALF_OPEN` ⇒ `OPEN`, regardless of prior half-open successes.
16. Re-opening resets `openedAt` to the current time (full `resetMs` restarts, not the remainder).
17. On `CLOSED` entry the rolling window is empty ⇒ a single failure immediately afterwards does not re-trip.
18. Failures older than `windowMs` expire ⇒ two failures separated by more than the window do not trip a
    `minimumThroughput: 2` breaker.
19. Bucket count stays bounded at `ceil(windowMs / bucketMs)` under sustained load (memory guard).
20. An error excluded by `isFailure` is recorded as a **success** and still rethrown to the caller.
21. 20 excluded errors in a row ⇒ breaker remains `CLOSED`.
22. A settlement arriving from a superseded generation is ignored: start a half-open trial, force a state change,
    then settle the trial ⇒ state is unchanged by that settlement.
23. Concurrent calls racing the `OPEN → HALF_OPEN` transition ⇒ exactly `halfOpenMaxConcurrent` admitted, all others
    `CircuitOpenError`; assert with N simultaneous `execute()` calls.
24. A call in flight when the breaker opens is **not** cancelled, and its outcome is discarded.
25. `CircuitOpenError` itself is never recorded as an outcome.
26. State transitions are driven only by `now()`; advancing the injected clock with no calls does **not** by itself
    move `OPEN → HALF_OPEN` in a way observable before the next call (lazy transition).
27. `minimumThroughput: 1` ⇒ `RangeError` (floor is 2).

### Rate limiter (RL)

1. Fresh bucket with `capacity: 5` grants exactly 5 immediate acquisitions at `t = 0`.
2. The 6th at `t = 0` is refused (`tryRemove` `false` / queued in wait mode).
3. `refillPerSec: 10`, empty bucket ⇒ `msUntil(1) === 100` exactly.
4. After advancing the clock by exactly 100 ms ⇒ exactly one token is available; a second is not.
5. At exactly the boundary (`tokens === n`) the acquisition **succeeds** (`>=`, not `>`).
6. 1000 successive 1 ms clock advances against a 10 tok/s bucket accrue exactly 10 tokens — **no drift**.
7. Tokens never exceed `capacity` however far the clock advances.
8. `capacity: 0` ⇒ every acquisition refused; in wait mode, `RangeError` at enqueue.
9. `refillPerSec: 0` ⇒ never refills; `msUntil` returns `Infinity`; wait mode rejects rather than scheduling.
10. Clock moving **backwards** mints no tokens, and the limiter recovers on the next forward advance.
11. Clock jumping far **forwards** clamps at `capacity` (cannot mint more than one burst).
12. `n > capacity` ⇒ `RangeError`, and it does **not** enter the queue.
13. Fractional `n` (e.g. `0.5`) is honoured.
14. Wait mode: waiters are released in strict **FIFO** order (assert with distinct tagged waiters).
15. Wait mode: a waiter is released at exactly `msUntil` — not earlier (proving `ceil`), not later.
16. `maxQueueDepth: 100` exceeded ⇒ immediate `RateLimitExceededError` carrying a sensible `retryAfterMs`.
17. `maxQueueWaitMs` exceeded at enqueue time ⇒ immediate rejection, not a late one.
18. Abort while queued ⇒ `CancelledError`, the waiter is removed, **no** token is consumed, and the timer is cleared.
19. Aborting the **head** waiter promotes the next one and re-arms the timer correctly.
20. Aborting the **only** waiter leaves **0** outstanding timers.
21. Reject mode (`onExhaustion: 'reject'`) ⇒ `RateLimitExceededError` immediately, never queues.
22. `retryAfterMs` on the rejection equals `msUntil(n)`.
23. Token accounting is exact after a mixed sequence of grants, refusals, aborts, and refills (assert the internal
    token count directly via a test hook).
24. Two independent limiter instances do not share state.

### Ordering / integration (ORD)

1. A hung operation is killed by the **attempt** timeout, and the retry loop then makes the next attempt — proving
   the attempt timeout is inside the loop.
2. `totalTimeoutMs` shorter than `maxAttempts × attemptTimeoutMs` ⇒ the whole call rejects with `TimeoutError` at the
   total deadline, mid-retry.
3. Abort mid-backoff propagates out as `CancelledError` through every enclosing layer, unwrapped.
4. `maxAttempts: 3` against a failing provider consumes **3** rate-limit tokens (retries pay).
5. `maxAttempts: 3` against a failing provider records **3** breaker failures (retries count).
6. A breaker that opens on attempt 2 causes attempt 3 to fail fast with `CircuitOpenError`, and the retry loop
   **stops** (non-retryable) rather than exhausting `maxAttempts`.
7. Rate-limiter queue wait does **not** consume the attempt timeout (the attempt timeout starts after admission).
8. Rate-limiter queue wait **does** consume the total timeout.
9. A queued call whose total timeout expires while queued is removed from the queue and consumes no token.
10. With every layer enabled and a provider that succeeds immediately, exactly 1 token is consumed, 1 breaker
    success is recorded, 0 sleeps occur, and 0 timers remain outstanding.
11. Errors surface with the correct precedence: `CancelledError` > `TimeoutError` (total) > `TimeoutError` (attempt,
    wrapped by retry) > `CircuitOpenError` > `RateLimitExceededError` > `RetryExhaustedError`.
12. After any terminal state of a full pipeline run, **0** timers and **0** abort listeners remain (leak guard).

---

## 7. Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | `maxAttempts` off-by-one — Polly means retries, cockatiel means total attempts, and reviewers will "know" the other one | Silent 33 % extra load on providers; the single most likely bug in this stream | §1.2 is normative. Doc comment on the field. Tests RT-1/RT-3 assert the invocation count directly, not the delay count |
| R2 | Someone "simplifies" the timeout wrapper to `AbortSignal.timeout()` + `AbortSignal.any()` | Timers become unref'd and invisible to fake timers ⇒ C3 broken; `timeoutMs: 0` silently becomes async | §2.1 records the empirical evidence; test TO-18 is a standing regression guard |
| R3 | Breaker trip condition evaluated only on the failure path | Breaker silently never trips at the configured ratio — **this bug was actually written and caught in validation** | §3.4 + test CB-6 exist specifically for it |
| R4 | Half-open admission check separated from its counter increment by an `await` | The interleaving lets N calls through the single trial slot; a thundering herd hits a sick provider | §5.3 comment marks it; test CB-23 asserts it with concurrent calls |
| R5 | Whole-token (integer) accrual in the bucket refill | Systematic drift; at moderate call rates the limiter delivers ~0 tokens | §4.2 rule 1; test RL-6 (1000-tick zero-drift) |
| R6 | Unbounded rate-limiter queue | Memory growth converts a rate problem into an OOM | `maxQueueDepth: 100` + `maxQueueWaitMs: 30_000`, both mandatory; tests RL-16/RL-17 |
| R7 | `minimumThroughput` default copied from Polly (100) | Breaker never trips for a low-QPS consumer — worse than having no breaker, because it creates false confidence | Defaulted to **10** with the reasoning in §3.3 |
| R8 | Timer/listener leaks under abort | Process hangs on shutdown for up to `maxDelayMs`; listener accumulation is **silent** because Node does not warn for `AbortSignal` | `clearTimeout`/`removeEventListener` in `finally` everywhere; ORD-12 and RT-32 are explicit leak guards |
| R9 | Retrying non-idempotent writes | Duplicate side effects — a correctness bug, not a performance one | Retry disabled for `idempotent: false` unless an `idempotencyKey` is supplied; never retry those on timeout (§1.3) |
| R10 | Wave 2 "corrects" the nesting order to match Polly's docs | Retries stop consuming rate-limit tokens exactly when a retry storm is underway | §5.6 records the divergence and the reason, with the Polly ordering quoted so the difference is visibly intentional |
| R11 | Fake-timer library interaction with `AbortSignal` | Some fake-timer implementations do not patch the timer used internally by `AbortSignal.timeout()` | Avoided entirely by injecting timers (§5.0). Coordinate with **R2** on the fake-timer API |
| R12 | Per-provider vs per-process limiter/breaker scope is unspecified here | Two providers sharing one breaker would trip each other | R3/R4 seam noted in §5.6. Recommendation: keyed **per provider instance**, created by the registry |
| R13 | `performance.now()` origin differs per worker thread | Cross-thread comparison of timestamps is meaningless | All primitives are per-instance and never compare timestamps across instances; do not serialise `now()` values |

---

## 8. Sources

- [AWS Architecture Blog — *Exponential Backoff And Jitter* (Marc Brooker)](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) — canonical full/equal/decorrelated jitter formulas and the contention experiments *(primary URL egress-blocked from this environment; formulas cross-checked against three independent reproductions and against the `expbackoff` reference implementation)*
- [`nathforge/expbackoff`](https://github.com/nathforge/expbackoff) — reference full-jitter implementation citing the AWS post
- [Node.js v22 API — globals / `AbortController`, `AbortSignal`](https://nodejs.org/docs/latest-v22.x/api/globals.html) — availability and stability of `AbortSignal.abort/timeout/any`, `throwIfAborted`, `reason`
- [Node.js v22 API — events](https://nodejs.org/docs/latest-v22.x/api/events.html) — `defaultMaxListeners` has no effect on `AbortSignal` instances
- [resilience4j — `CircuitBreakerConfig.java`](https://github.com/resilience4j/resilience4j/blob/master/resilience4j-circuitbreaker/src/main/java/io/github/resilience4j/circuitbreaker/CircuitBreakerConfig.java) — all `DEFAULT_*` constants; `SlidingWindowType`; `automaticTransitionFromOpenToHalfOpen = false`
- [Polly v8 — `RetryStrategyOptions.TResult.cs`](https://github.com/App-vNext/Polly/blob/main/src/Polly.Core/Retry/RetryStrategyOptions.TResult.cs) — `MaxRetryAttempts` = retries *in addition to* the original call
- [Polly v8 — `CircuitBreakerStrategyOptions.TResult.cs`](https://github.com/App-vNext/Polly/blob/main/src/Polly.Core/CircuitBreaker/CircuitBreakerStrategyOptions.TResult.cs) — `FailureRatio 0.1`, `MinimumThroughput 100` (floor 2), `SamplingDuration 30s`, `BreakDuration 5s`
- [Polly v8 — pipelines documentation](https://github.com/App-vNext/Polly/blob/main/docs/pipelines/index.md) — outer/inner composition semantics and the three timeout/retry arrangements
- [`dotnet/extensions` — `HttpStandardResilienceOptions.cs`](https://github.com/dotnet/extensions/blob/main/src/Libraries/Microsoft.Extensions.Http.Resilience/Resilience/HttpStandardResilienceOptions.cs) — verbatim pipeline order `Bulkhead -> Total Request Timeout -> Retry -> Circuit Breaker -> Attempt Timeout`; 10 s attempt timeout
- [cockatiel](https://github.com/connor4312/cockatiel) — `maxAttempts` as total attempts; `ConsecutiveBreaker`/`SamplingBreaker`; `BrokenCircuitError`; `TimeoutStrategy.Aggressive`/`Cooperative`
- [opossum](https://github.com/nodeshift/opossum) — `errorThresholdPercentage 50`, `volumeThreshold`, 10 s / 10-bucket rolling window, single-trial half-open
- [Cloudflare sliding-window-counter approximation](https://github.com/alisaifee/limits/discussions/245) — `weighted = previous × overlap + current`; ~0.003 % error over 400 M requests
- [AWS Prescriptive Guidance — circuit breaker pattern](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/circuit-breaker.html) and Michael Nygard, *Release It!* — the CLOSED/OPEN/HALF_OPEN formulation
