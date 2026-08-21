# Known issues

Open items in `interlayer`, recorded deliberately rather than fixed. Each entry
says what is wrong, what it costs today, and what fixing it would take — so the
next person spends their time deciding, not rediscovering.

Everything here was found during the post-Wave-3 repair pass. The eleven defects
that were fixed in that pass are not listed; they live in the code, in the
comment above the thing that was changed.

---

## 1. `satisfies Provider<C>` on a fresh literal widens `id` to `string`

```ts
const openai = { id: 'openai', capabilities: { chat } } satisfies Provider<Ai>;
layer.only('openai');   // no longer checked: `Ids` collapsed to `string`
```

`defineProvider(contract, { … })` infers the literal id and is the supported
form; `satisfies` on an inline object literal widens `id` before `createLayer`
ever sees it, and `ProviderIds<C, P>` then degrades to `string`. `only()` stops
checking ids from that point on, silently.

**Cost.** A typo in `only('opneai')` compiles. Routing then finds no candidate
and the call fails with `NO_PROVIDER` at run time instead of at build time.

**Why not fixed.** The widening happens in the CALLER's file, before any of our
types are applied; there is no declaration we can write that un-widens it. The
realistic mitigations are documentation (`defineProvider` is the supported form)
and a lint rule. Both are out of scope for a repair pass.

---

## 2. `only()` does not narrow its own id type

```ts
layer.only('a').only('b');   // compiles; pins to [] and every call is NO_PROVIDER
```

`only(...ids: readonly Ids[])` returns `Layer<C, Ids, Impl>` — the FULL id union,
not the pinned subset — so a second `only()` accepts an id the first one already
excluded. `intersectPins` is correct at run time (a restriction that can be
undone is not a restriction), so the result is an empty pin set and a guaranteed
`NO_PROVIDER`.

**Cost.** A contradiction that a type could have caught is only found at run
time. Pinned by `contradictoryPinsCompile()` in
`test/types/layer-surface.test.ts`.

**Why not fixed.** The fix is `only<const Sub extends readonly Ids[]>(...ids: Sub):
Layer<C, Sub[number], Impl>`, which changes a published signature and ripples
through `LayerConfig`, `ProviderIds` and every `Layer` annotation in the type
tests. That is an API change with a version bump attached, not a repair.

---

## 3. `maxQueueWaitMs` is not clamped to the remaining call deadline

A caller sitting in the rate limiter's queue can be granted a wait longer than
the call's own remaining budget. The call still fails on time — the total timeout
and the armed deadline both pre-empt the wait — so this is waste, not
incorrectness: a queue slot is held for a call that can no longer use it, and
another caller who could have used the token waits behind it.

**Cost.** Throughput under a saturated bucket with tight deadlines. Never a wrong
answer.

**Fix.** Clamp `maxQueueWaitMs` to `ctx.deadlineAt - now` when a deadline exists,
in `src/resilience/rate-limit/rate-limit.ts`, and re-derive the queue-timing
tests in `test/composition/queue-and-budget.test.ts` around it.

---

## 4. `BULKHEAD_FULL` is reserved and unreachable

`ErrorCode` declares `'BULKHEAD_FULL'` and `errors.ts` exports
`BulkheadFullError`, but no bulkhead unit was built, so nothing throws it.
`test/failure-modes/error-taxonomy.test.ts` records it as `kind: 'reserved'` and
its reachability table would fail to compile if the code were removed from the
union without that entry being removed too.

**Cost.** One dead arm in an exhaustive `switch (err.code)`.

**Decision needed.** Either build the bulkhead policy (a concurrency limiter is
the one resilience primitive the library is missing) or remove the code and the
class in the same commit as the next major. Do not remove one without the other.

---

## 5. `await using` does not parse on the declared Node floor

`package.json` declares `engines.node >= 22.12.0`. V8 on Node 22 rejects the
`await using` DECLARATION outright with a `SyntaxError`; `Symbol.asyncDispose` —
which `Layer` implements and which the suite calls directly — is honoured.
Vitest transpiles the declaration, so `src/layer.test.ts` can contain an
`await using` that a consumer on Node 22 cannot compile.

**Cost.** None today: no README or published doc comment shows the syntax, and
the only occurrences are inside the test suite, which is transpiled. The risk is
purely that someone copies it into consumer-facing prose later.

**Rule.** `await layer.close()` is the documented teardown. Do not put
`await using` in the README hero, in a doc comment on `createLayer`, or in any
example a consumer is meant to paste — until the `engines` floor moves to a
runtime that parses it.

---

## 6. `CallResult` carries both `providerId` and `provider`

Two fields, the same string, on every result:

```ts
const { value, provider, attempts, durationMs, errors } = await layer.callWithMeta(…);
const { value, providerId } = await layer.callWithMeta(…);   // also fine
```

`providerId` matches the settled `id`/`providerId` naming used by the error
context and the whole event map; `provider` exists because R6 §5.2's acceptance
example destructures it, and two independent reviewers said `provider` reads
better at the destructuring site.

**Cost.** A published surface with a redundant name, and a reader who has to
check whether the two can ever differ. (They cannot: `invoke()` assigns both from
the same local.)

**Why not resolved here.** Dropping either one is a breaking change to a public
type with roughly twenty call sites in the suite alone, and the two candidate
names are backed by two different settled documents. It needs an owner's decision
and a major version, not a repair pass. The doc comment on
`CallResult.provider` in `src/core/types.ts` carries the same note.
