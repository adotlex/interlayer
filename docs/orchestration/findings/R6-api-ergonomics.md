# R6 — Prior Art & Public API Ergonomics

**Stream:** Wave 1 / R6
**Status:** complete
**Headline:** the call-site type-inference technique is **empirically verified** — `tsc 5.9.3`, 0 errors
across 8 probe files, 11/11 negative cases producing readable errors, and the runtime executed on
Node v22.22.2 with a virtual clock (no wall-clock sleeps).

---

## 1. Prior art

Every row below was read from the actual README/docs, not from memory. Fetch failures are noted.

| Library | Public API shape | Steal this | Avoid this | Source |
|---|---|---|---|---|
| **cockatiel** (JS, zero-dep) | Standalone policy factories composed by `wrap()`: `wrap(retry(handleAll, {maxAttempts:3, backoff:new ExponentialBackoff()}), circuitBreaker(handleAll, {halfOpenAfter:10_000, breaker:new ConsecutiveBreaker(5)}))`, then `.execute(({signal}) => fn(signal))`. Uniform `IPolicy { onSuccess; onFailure; execute<T>(fn, signal?) }`. | (a) **The executed fn receives `{ signal }`** — cancellation is pushed *into* the callee, not bolted on. (b) Policies are values that compose, not config flags. (c) Typed event hooks `onFailure/onBreak/onRetry/onReset/onHalfOpen` returning a disposable. (d) `handleType`/`handleWhen`/`handleResultType` — failure predicates are first-class, so "a 429 body is a failure" is expressible. (e) `breaker.toJSON()` for serverless state. | Composition order is *positional and undocumented in effect* — `wrap(a,b,c)` gives no hint which policy is outermost, and getting it wrong silently changes semantics. Also the merged execution context is a structural union, so `ctx` shape depends on which policies you wrapped. | https://github.com/connor4312/cockatiel |
| **opossum** (Node circuit breaker) | `new CircuitBreaker(asyncFn, opts)`, `.fire(...args)`, `.fallback(fn)`, EventEmitter with `fire/success/failure/timeout/reject/open/close/halfOpen/fallback/semaphoreLocked`. Options: `timeout`, `errorThresholdPercentage` (50), `resetTimeout` (30000), `rollingCountTimeout` (10), `rollingCountBuckets` (10), `capacity`, `coalesce*`. | Rich, *named* lifecycle events; explicit rolling-window stats (`rollingCountTimeout` × `rollingCountBuckets`) rather than a magic number; `.toJSON()` state export. | (a) **Types ship separately** as `@types/opossum` on DefinitelyTyped — the README literally says "Typings are available" via DT. Drift between impl and types is guaranteed. (b) `fallback(fn)` **swallows the error** unless you also subscribe to the `failure` event — the happy path hides the cause. (c) Wraps one function per breaker, so N capabilities × M providers = N·M hand-managed breaker objects. | https://github.com/nodeshift/opossum |
| **p-retry** | `pRetry(input, {retries:10, factor:2, minTimeout:1000, maxTimeout:Infinity, randomize:false, signal, onFailedAttempt, shouldRetry, shouldConsumeRetry})`; `AbortError` to stop retrying. `onFailedAttempt` receives `{error, attemptNumber, retriesLeft, retriesConsumed, retryDelay}`. | (a) `signal` for cancellation. (b) `onFailedAttempt` carries **attempt metadata**, not just the error. (c) `shouldRetry` predicate. (d) `shouldConsumeRetry` — a failure that doesn't burn budget. | **`retries: 10` means 11 total calls.** The classic off-by-one. A field named `retries` and a field named `attempts` differ by one and nothing in the type system tells you which you have. Also `randomize: false` by default, so the out-of-the-box config is thundering-herd-prone. | https://github.com/sindresorhus/p-retry |
| **Polly v8** (.NET) | `new ResiliencePipelineBuilder().AddRetry(new RetryStrategyOptions()).AddTimeout(TimeSpan.FromSeconds(10)).Build()`, then `pipeline.ExecuteAsync(token => …)`. Strategy *options records*: `RetryStrategyOptions{MaxRetryAttempts, Delay, BackoffType, UseJitter, DelayGenerator, ShouldHandle}`, `CircuitBreakerStrategyOptions{FailureRatio, SamplingDuration, MinimumThroughput, BreakDuration, StateProvider}`, plus `HedgingStrategyOptions`. | (a) **One options record per strategy** — every knob is a named, typed field, not a positional argument. (b) `MaxRetryAttempts` + `UseJitter` are named unambiguously. (c) `MinimumThroughput` — a breaker that refuses to trip on a tiny sample. (d) A single pipeline object reused across call sites. | The README **does not state the ordering semantics** of `.AddRetry().AddTimeout()`. Users must infer that the first-added is outermost. This is the #1 source of "my timeout doesn't apply per attempt" confusion. | https://github.com/App-vNext/Polly |
| **resilience4j** (Java) | Named registries: `CircuitBreakerRegistry.of(defaultConfig)` → `registry.circuitBreaker("paymentService")`. Fluent config builders. Composition via `Decorators.ofSupplier(s).withCircuitBreaker(cb).withBulkhead(b).withRetry(r).decorate()`. | (a) **Named instances with shared named configs** — you configure "aggressive" once and attach it to twenty call sites. (b) Registry as the unit of observability. | (a) Same ordering gap as Polly: the README shows `CircuitBreaker → Bulkhead → Retry` in examples but **documents no authoritative order**. (b) Names are strings with no compile-time checking. | https://github.com/resilience4j/resilience4j |
| **Vercel AI SDK** — provider interface | `LanguageModelV3` with `readonly specificationVersion: 'v3'`, `provider: string`, `modelId: string`, `supportedUrls`, `doGenerate(options)`, `doStream(options)`. The `do` prefix exists to "prevent accidental direct usage of the method by the user". | (a) **A `specificationVersion` literal on the provider interface** — providers declare which contract they implement, so a host can reject or adapt mismatched providers at load time. (b) `do`-prefixed internal methods to keep the app-facing verb (`generateText`) distinct from the provider-facing verb. | Versioning by minting `LanguageModelV1…V3` interfaces means every provider re-implements on each bump. | https://github.com/vercel/ai/blob/main/packages/provider/src/language-model/v3/language-model-v3.ts |
| **Vercel AI SDK** — provider registry | `createProviderRegistry({openai, anthropic})` → `registry.languageModel('openai:gpt-5.1')`; `customProvider()` for aliases; `{separator: ' > '}` option. | The idea of one registry object fronting many providers, with per-provider pre-configuration and aliasing (`customProvider`). | **The cautionary tale for this project.** Three separate real complaints: (a) *"If I create a provider registry, I can't extract the actual list of valid models"* — wrapping the API loses type safety entirely (#5745, 2025-04-14). (b) Model ids are `'literal' \| (string & {})`, so *"by not exporting the type, you allow it to be instantiated with any string, giving an error at runtime"* (#6936, 2025-06-30). (c) The `provider:model` composite string breaks when the model id itself contains a colon — the parser splits on the **last** colon and sends the wrong id (#2056). **Lesson: never encode structure in a delimited string.** | https://github.com/vercel/ai/issues/5745 · https://github.com/vercel/ai/issues/6936 · https://github.com/vercel/ai/issues/2056 |
| **unstorage** (unjs) | `createStorage(opts)`; drivers via `defineDriver`; unix-style `mount`/`unmount` to compose several backends under key prefixes; auto JSON (de)serialisation. | (a) `define*` factory naming for authoring an adapter — it exists purely to give the author's object literal a contextual type. (b) **Mounting**: composing several backends under one facade is exactly our routing problem, solved by namespacing rather than by a magic string id. | Values are effectively `StorageValue`; per-key typing is a per-call generic, which is an assertion, not a proof. | https://unstorage.unjs.io · https://github.com/unjs/unstorage |
| **Keyv** | `new Keyv(store, {namespace:'users'})`; adapter contract is "any store that follows the `Map` api"; v6 adapters implement `set/get/delete/clear/has` plus `*Many` variants and declare `capabilities.expires === true`. Generics: `new Keyv<number>()` and `await keyv.get<string>('key')`. `EventEmitter`, emits `'error'`. | (a) **`capabilities.expires === true`** — explicit *capability negotiation* between host and adapter, versioned per adapter. Directly applicable. (b) Namespacing to avoid collisions. | (a) `keyv.get<string>(...)` is an **unchecked assertion** at the call site — the classic anti-pattern we must not repeat. (b) Errors surface only through an `'error'` event; on a Node `EventEmitter` an unhandled `'error'` **throws**, so failing to subscribe converts a recoverable error into a crash. | https://github.com/jaredwray/keyv/blob/main/core/keyv/README.md |
| **Terraform provider registry** | `terraform { required_providers { mycloud = { source = "mycorp/mycloud", version = "~> 1.0" } } }`. Source address is `[<HOSTNAME>/]<NAMESPACE>/<TYPE>`; the map key is a module-local name, "unique per-module". | **Decoupling the local name from the provider identity.** "Users of a provider can choose any local name for it" — which is how two providers of the same type coexist and how you swap the implementation behind a stable name. Our `name` field is the local name; the package that supplies it is the source. | HCL is stringly-typed throughout; version constraints are validated late. | https://developer.hashicorp.com/terraform/language/providers/requirements |
| **LangChain JS** | Standard model interface `invoke` / `stream` / `batch` with "standard parameters" (`model`, `temperature`, `timeout`, `maxRetries`, `apiKey`, `maxTokens`). *(js.langchain.com is blocked by the egress proxy; taken from the repo README + widely-documented interface.)* | A tiny, stable verb set (`invoke`/`stream`/`batch`) rather than a verb per capability. | Standard parameters are **advisory** — they apply only to some integrations, and provider-specific options escape into untyped bags. An option that silently does nothing on provider B is the worst possible failure mode for an interop layer. | https://github.com/langchain-ai/langchainjs |
| **koa** | `(ctx, next)`; `await next()` splits "capture" and "bubble" phases. | The onion model, and the fact that it is only two arguments. | The docs demonstrate that **omitting `await next()` silently halts the chain** — downstream middleware "never execute; they're bypassed entirely" with no error. Silent. | https://github.com/koajs/koa/blob/master/docs/guide.md |
| **hono** | `new Hono<{ Bindings: Bindings; Variables: Variables }>()`; then `c.env.TOKEN // token is string` and `c.set('user', user) // user should be User`. | **This is the closest analogue to our problem and the shape to copy**: a *record type supplied as one generic parameter* makes a stringly-keyed accessor (`c.get('user')`) fully typed, with no declaration merging and no globals. Two apps can have different `Variables` in one process. | Type accumulation happens through method chaining, so breaking the chain across statements silently loses types (see §4). | https://hono.dev/docs/api/hono |
| **express** | `app.use(fn)`, `(req, res, next)`; types via `@types/express`, extension via `declare global { namespace Express { interface Request {…} } }` | — | Global declaration merging: one process, one shape. Two libraries augmenting `Request` conflict, and the augmentation is invisible at the call site. **This is the technique we explicitly reject** (§4). | https://github.com/expressjs/express |

---

## 2. Recommendation — the public API

**One generic parameter carrying a capability record**, exactly as Hono carries `Variables` — but
inferred from a value (`defineContract`) rather than written by hand, and with the set of *actually
implemented* capabilities and *registered provider names* accumulated from the providers array.

Four verbs to learn:

```ts
defineContract({ name: capability<In, Out>() })   // what the app needs
defineProvider(contract, { name, capabilities })  // who can do it
createLayer({ contract, providers, resilience })  // one object, wired
layer.call('name', input)                         // fully typed
```

The design rules that fall out of the prior art:

1. **No delimited composite strings, ever.** `'openai:gpt-4o'` is the AI SDK's #2056 bug in the
   general case. Capability and provider are two arguments/fields, never one string.
2. **Configuration is a value, not a chain.** `createLayer({...})` takes one config object; the
   fluent-builder alternative accumulates types but silently loses them when the chain is broken
   (verified — §4).
3. **The handler receives `{ signal }`** (cockatiel), and the layer never retries a non-idempotent
   capability unless told to.
4. **Every knob is a named field on an options record** (Polly), and every "off" is `false`, never
   `0`/`undefined`.
5. **Composition order is fixed and documented**, because Polly and resilience4j both left it
   ambiguous and it is the single most common source of confusion:

   ```
   overall deadline
     └─ route / fallback across providers          (outermost)
          └─ retry                       (per provider)
               └─ circuit breaker        (so retries fail fast and hand off)
                    └─ rate limit        (open circuit must not burn tokens)
                         └─ concurrency
                              └─ per-attempt timeout
                                   └─ middleware (onion)
                                        └─ provider handler   (innermost)
   ```
   Not configurable in v1. *(Exact algorithmic semantics belong to R4; this is the API-visible
   ordering contract R4 must implement.)*
6. **Errors never lose their cause,** and a fallback never discards the failures it hid
   (opossum's mistake): `AllProvidersFailedError.failures` holds every suppressed error and
   `cause` is set at every level.
7. **Types are generated from source**, shipped in-package. Never a companion `@types/interlayer`.

---

## 3. Rationale (tied to C1–C5)

| Decision | Why | Constraint |
|---|---|---|
| Contract-as-value + one generic param | Two layers with different contracts coexist in one process; no globals, no augmentation ordering | — |
| Zero runtime deps; own listener `Set` instead of Node `EventEmitter` | No dep, and a listener-less `'failure'` cannot crash the process the way an unhandled EventEmitter `'error'` does (Keyv) | **C4** |
| Injected `clock` + `random` in `LayerConfig` | Retry/backoff/breaker tested in virtual time; **verified**: the runtime probe ran a 3-attempt fallback sequence with zero real sleeping | **C3** |
| `AbortSignal.any` / `AbortSignal.timeout` (Node ≥20 builtins) | Cancellation and timeouts with no dependency and no timer leaks | **C4**, **C5** |
| Surface split into disjoint modules (§5.4) | `contract.ts` / `provider.ts` / `layer.ts` / `resilience/*.ts` / `errors.ts` / `events.ts` / `testing.ts` are separately ownable | **C1** |
| Public options declared `?: T \| undefined` | Consumers on `exactOptionalPropertyTypes` can forward optionals; **verified** this is a hard error otherwise | — |

---

## 4. Rejected, and why

| Rejected | Why |
|---|---|
| **Declaration merging** (`declare module 'interlayer' { interface Capabilities { chat: … } }`) — the Express `namespace Express` model | One global registry per process. Two contracts cannot coexist; a library that registers capabilities silently changes an application's types; the source of a capability is invisible at the call site; and augmentation is order-dependent across `.d.ts` load. Express's `Request` augmentation is the canonical warning. |
| **Fluent accumulating builder** `createLayer(c).register(a).register(b)` (tRPC/Hono style) | It *works* — verified: the chained form accumulates `'chat' \| 'embed'` correctly. But the footgun is verified too: writing `const b = layerBuilder(c); b.register(a); b.register(x);` discards the return value and the accumulated types **silently vanish** — `b.build().call('chat', …)` becomes a type error with no hint why. tRPC's builder is also the documented source of "crazy" hover types and cryptic errors. A single config object cannot be mis-sequenced. |
| **Stringly-typed composite ids** `layer.call('openai:chat', input)` | Verbatim reproduction of AI SDK #2056 (delimiter collision) and #6936 (`(string & {})` accepts anything, fails at runtime). |
| **Per-call assertion generics** `layer.call<ChatReply>('chat', input)` (Keyv's `get<T>`) | An unchecked assertion dressed as type safety. If it disagrees with the provider, nothing tells you. |
| **A class hierarchy** (`class Layer extends …`, `abstract class Provider`) | Providers become impossible to author as plain objects, hostile to tree-shaking, and force `instanceof` checks across module instances. `defineProvider` returning a plain object is testable with a two-line fake. |
| **Configurable strategy order in v1** | Polly and resilience4j both left order under-documented and it is their most confusing aspect. Fix one order, document it in a diagram, revisit in v2. |
| **Making `layer.call` overloaded on an options flag** to change the return type | Overload-driven return types produce bad errors and bad hovers. Two named methods (`call` / `callWithMeta`) instead. |
| **`layer.using(name)`** as the narrowing verb | `using` is now a JS/TS declaration keyword (`await using x = …`); a method with that name reads like a syntax error. Renamed **`layer.only(...)`**. |
| **Depending on zod/valibot for input validation** | Violates C4. Instead `capability<In, Out>({ parse })` accepts *any* `(input: unknown) => In`, which a zod user satisfies with `schema.parse` and everyone else omits. |

---

## 5. Concrete specifics

### 5.1 The type-inference technique — **VERIFIED**

Verification harness: `tsc 5.9.3`, `strict` + `exactOptionalPropertyTypes` +
`noUncheckedIndexedAccess` + `verbatimModuleSyntax` + `erasableSyntaxOnly`, target `es2023`,
module `nodenext`; runtime on Node **v22.22.2** via `node --experimental-strip-types`.
8 files / ~1015 lines. **Result: 0 type errors; 11/11 negative cases fire; runtime probe executes.**
*(Probe lived in the session scratchpad at `r6-probe/`; it is ephemeral — everything needed is
reproduced below.)*

The whole trick is a **phantom type carrier on the capability descriptor**, plus **argument order**:
`capability` is the *first* parameter, so TS resolves `K` from the string literal before it has to
contextually type `input`.

```ts
declare const phantom: unique symbol;

export interface Capability<In = unknown, Out = unknown> {
  /** Type-only carrier. Never present at runtime. */
  readonly [phantom]?: { in: In; out: Out };
  readonly idempotent?: boolean | undefined;
  readonly description?: string | undefined;
}

export type Contract = Record<string, Capability<any, any>>;

/**
 * F-bounded shape used for CONSTRAINTS. `C extends Record<string, Capability>`
 * REJECTS a contract annotated with an `interface`, because interfaces have no
 * implicit index signature. This form accepts both. (Verified: the Record form
 * errors with "Argument of type 'AiContract' is not assignable to
 * parameter of type 'Contract'"; this form compiles.)
 */
export type ContractShape<C> = { readonly [K in keyof C]: Capability<any, any> };

export type CapabilityName<C extends Contract> = Extract<keyof C, string>;

/** Indexed access into the carrier — no conditional `infer` needed. */
export type InputOf<C extends Contract, K extends keyof C> =
  NonNullable<C[K][typeof phantom]>['in'];
export type OutputOf<C extends Contract, K extends keyof C> =
  NonNullable<C[K][typeof phantom]>['out'];

export function capability<In, Out>(meta: CapabilityMeta<In> = {}): Capability<In, Out> {
  return meta as Capability<In, Out>;   // the only cast in the design
}
export function defineContract<C extends ContractShape<C>>(contract: C): C { return contract; }
```

Handlers are contextually typed by constraining the provider's `capabilities` to
`Partial<Handlers<C>>` while still **inferring** it, so the *implemented keys survive*:

```ts
export type Handler<C extends Contract, K extends keyof C> =
  (input: InputOf<C, K>, ctx: CallContext) => Promise<OutputOf<C, K>>;
export type Handlers<C extends Contract> = { [K in keyof C]: Handler<C, K> };

export function defineProvider<
  C extends ContractShape<C> & Contract,
  const Name extends string,          // `const` keeps 'openai' from widening to string
  H extends Partial<Handlers<C>>,     // constraint gives contextual typing; inference keeps keys
>(contract: C, def: { name: Name; capabilities: H; close?: () => Promise<void> | void }):
  Provider<C, Name, H> { return def; }
```

The layer collects names and implemented capabilities from the providers **tuple**, via
distributive conditionals (a naked type parameter is required — `keyof (A | B)` would give the
*intersection*, which is wrong):

```ts
type ImplementedOf<P> = P extends { capabilities: infer H } ? Extract<keyof H, string> : never;
type NameOf<P>        = P extends { name: infer N extends string } ? N : never;

export function createLayer<
  C extends Contract,
  const P extends readonly Provider<C, string, any>[],
>(config: LayerConfig<C, P>):
  Layer<C, NameOf<P[number]>, Extract<ImplementedOf<P[number]>, CapabilityName<C>>>;

export interface Layer<C extends Contract, Names extends string = never,
                       Impl extends CapabilityName<C> = never> {
  call<K extends Impl>(capability: K, input: InputOf<C, K>,
                       options?: CallOptions<Names>): Promise<OutputOf<C, K>>;
  // …
}
```

**What was verified, case by case**

| # | Case | Result |
|---|---|---|
| P1 | `InputOf/OutputOf` are mutually assignable with the author's own types (bidirectional `Assert<A,B>`) | ✅ |
| P2 | The `infer`-based variant is equivalent to the indexed-access variant | ✅ (both work; indexed access chosen — no conditional, cheaper) |
| P3 | Handler params contextually typed with **zero annotations** in the provider | ✅ |
| P4 | `layer.call('chat', {…})` contextually types the input object literal and returns `ChatReply` | ✅ |
| P5 | Two capabilities on one layer infer two different In/Out pairs | ✅ |
| P6 | `route: ['anthropic','openai']` checked against registered provider names | ✅ |
| P7 | `callWithMeta().provider` is `'openai' \| 'anthropic' \| 'deepgram'`, not `string` | ✅ |
| P8 | `layer.only('anthropic')` narrows `Names` and keeps `call` inference | ✅ |
| P9 | `layer.on('fallback', e => …)` types `e.from` and `e.capability` as literal unions | ✅ |
| P10 | `layer.supports(s)` is a type guard that narrows `string` → implemented union | ✅ |
| P11 | `void`-input, `AsyncIterable`-output (streaming), and spread-extended contracts all infer | ✅ |
| P12 | Provider **factories** (`makeProvider('azure', opts)` with `const N extends string`) keep the literal name | ✅ |
| P13 | Providers hoisted to a `const` array **without** `as const` still yield the full name union | ✅ (the `const` type-param modifier is enough) |
| P14 | `[a, b] satisfies readonly Provider<C, string, any>[]` preserves full precision | ✅ |
| P15 | `createLayer({providers: []})` makes `call` uncallable (`Impl = never`) — fails closed | ✅ |
| N1–N11 | Every negative case below produces a compile error | ✅ 11/11 |

**Actual error text a user sees** (captured by stripping the `@ts-expect-error` suppressions):

```
'chatt' is not a capability
  TS2345: Argument of type '"chatt"' is not assignable to parameter of type '"chat" | "embed"'.

capability in the contract but implemented by no registered provider
  TS2345: Argument of type '"transcribe"' is not assignable to parameter of type '"chat" | "embed"'.

wrong input shape
  TS2353: Object literal may only specify known properties, and 'texts' does not exist in type 'ChatRequest'.

wrong output field
  TS2339: Property 'vectors' does not exist on type 'ChatReply'.

unregistered provider in the route
  TS2322: Type '"anthropic"' is not assignable to type '"openai"'.

typo'd capability in per-capability config
  TS2561: Object literal may only specify known properties, but 'chats' does not exist in
          type 'Partial<Record<"chat" | "embed" | "transcribe", ResiliencePolicy>>'.
          Did you mean to write 'chat'?

unknown event name
  TS2345: Argument of type '"exploded"' is not assignable to parameter of type 'LayerEventName'.
```

That last one is only readable because `LayerEventName` is a **named alias**. Declared inline as
`E extends keyof LayerEvents<C, Names>` the message printed the raw generic — verified before/after.
**Rule: never expose `keyof SomeGeneric<…>` in a public signature; alias it.**

### 5.2 The literal usage example

This is the code a user writes, top to bottom. Modulo the elided provider bodies, this compiles.

```ts
import {
  capability, defineContract, defineProvider, createLayer,
  isInterlayerError, AllProvidersFailedError,
} from 'interlayer';

// 1 ── declare what your application needs, independent of who provides it.
const ai = defineContract({
  chat:       capability<ChatRequest, ChatReply>({ idempotent: false }),
  embed:      capability<EmbedRequest, EmbedReply>({ idempotent: true }),
  transcribe: capability<TranscribeRequest, TranscribeReply>({ idempotent: true }),
});

// 2 ── implement it, once per backend. No parameter annotations needed:
//      `input` and the return type come from the contract.
const openai = defineProvider(ai, {
  name: 'openai',
  capabilities: {
    chat:  async (input, { signal }) => callOpenAiChat(input, signal),
    embed: async (input, { signal }) => callOpenAiEmbed(input, signal),
  },
  close: () => pool.close(),
});

const anthropic = defineProvider(ai, {
  name: 'anthropic',
  capabilities: { chat: async (input, { signal }) => callAnthropic(input, signal) },
});

const deepgram = defineProvider(ai, {
  name: 'deepgram',
  capabilities: { transcribe: async (input, { signal }) => callDeepgram(input, signal) },
});

// 3 ── wire it. Array order is the default fallback order.
const layer = createLayer({
  contract:  ai,
  providers: [openai, anthropic, deepgram],
  resilience: {
    timeoutMs:   10_000,                                   // per attempt
    retry:       { attempts: 3, backoff: 'exponential', baseMs: 200, jitter: 'full' },
    breaker:     { failureThreshold: 0.5, samples: 20, resetAfterMs: 30_000 },
    rateLimit:   { perSecond: 20, burst: 40 },
    concurrency: { max: 8, queue: 64 },
  },
  perCapability: {
    embed:      { timeoutMs: 2_000, retry: { attempts: 5 } },
    transcribe: { timeoutMs: 120_000, retry: false },       // explicit off, never `attempts: 0`
  },
});

// 4 ── call it. `input` is checked against ChatRequest; `reply` is ChatReply.
const reply = await layer.call('chat', {
  messages: [{ role: 'user', content: 'hello' }],
  temperature: 0.2,
});
reply.text;   // string

// 5 ── per-call overrides. `route` is checked against the registered names.
const urgent = await layer.call(
  'chat',
  { messages },
  { route: ['anthropic', 'openai'], timeoutMs: 2_000, signal: req.signal },
);

// 6 ── pin a provider (evals, canaries, reproducing a bug)
const control = await layer.only('anthropic').call('chat', { messages });

// 7 ── when you need to know what actually happened
const { value, provider, attempts, durationMs, errors } =
  await layer.callWithMeta('embed', { texts });
metrics.timing('ai.embed', durationMs, { provider, attempts });

// 8 ── observe. `on` returns its own unsubscribe; there is no `off(fn)` identity trap.
const stop = layer.on('fallback', (e) => {
  log.warn(`${e.capability}: ${e.from} -> ${e.to}`, { reason: e.reason.code });
});
layer.on('breaker', (e) => metrics.gauge('breaker', e.state === 'open' ? 1 : 0, { p: e.provider }));

// 9 ── cross-cutting concerns as onion middleware (koa/hono shape)
layer.use(async (call, next) => {
  const span = tracer.start(`${call.capability}:${call.provider}`);
  try { return await next(); } finally { span.end(); }
});

// 10 ── errors: discriminated by `code`, and nothing is ever swallowed.
try {
  await layer.call('chat', { messages });
} catch (err) {
  if (isInterlayerError(err)) {
    switch (err.code) {
      case 'TIMEOUT':      return degrade();
      case 'CIRCUIT_OPEN': return queueForLater();
      case 'ALL_FAILED': {
        const all = err as AllProvidersFailedError;
        log.error('every provider failed', {
          tried:  all.failures.map((f) => f.provider),
          causes: all.failures.map((f) => f.cause),   // original errors, never lost
        });
        throw err;
      }
    }
  }
  throw err;
}

await layer.close();   // or `await using layer = createLayer(…)` on Node ≥24 (see Risks)
```

Deterministic tests need no fake-timer library:

```ts
import { fakeClock, seededRandom, mockProvider } from 'interlayer/testing';

const clock = fakeClock();
const layer = createLayer({
  contract: ai,
  providers: [mockProvider(ai, 'flaky', { chat: failTimes(2) }), openai],
  resilience: { retry: { attempts: 3, baseMs: 100 } },
  clock,
  random: seededRandom(42),
});

await layer.call('chat', { messages: [] });
assert.deepEqual(clock.slept, [100, 200]);   // virtual time; the suite never sleeps
```

*(Verified equivalent: the runtime probe produced `sleeps(ms): [100]`, `virtual time: 100`,
`attempts: 3`, event order `attempt flaky#1 → retry flaky +100ms → attempt flaky#2 →
fallback flaky→solid → attempt solid#1 → success solid after 3`, and a preserved cause chain
`AllProvidersFailedError/ALL_FAILED → PROVIDER_ERROR → Error: boom 3`.)*

### 5.3 Export names

Package `interlayer`. ESM-first, one main entry plus one testing subpath. No default export —
default exports break `import *` ergonomics and rename badly at call sites.

**Functions (4 — the entire thing you must learn)**

| Name | Argument for it |
|---|---|
| `defineContract` | `define*` is the unjs convention for "give my object literal a contextual type" (`defineDriver`). Not `createContract`: nothing is constructed. |
| `capability` | Reads as a noun at its only use site: `chat: capability<ChatRequest, ChatReply>()`. Not `op`/`method`/`endpoint` (HTTP-flavoured), not `defineCapability` (too long inside a record literal). |
| `defineProvider` | "Provider" is the term Terraform, Vercel AI SDK, and .NET all use for a swappable backend. Not `adapter` (Keyv/unstorage sense: one backend, one shape) and not `driver` (implies a device/protocol). |
| `createLayer` | Matches `createStorage`. Not `createInterlayer` (stutters on import), not `interlayer()` (a function named after the package fights the import name). |

**The `Layer` methods**

| Name | Argument for it |
|---|---|
| `call(cap, input, opts?)` | Shortest accurate verb. `invoke` is LangChain-flavoured and one syllable longer at every call site; `execute` is cockatiel/Polly's *policy* verb and would confuse the two concepts; `run` implies a job. `Layer` is a plain object, so there is no real `Function.prototype.call` shadowing. |
| `callWithMeta(...)` | Explicit about the *only* difference (`{value, provider, attempts, durationMs, errors}`). Beats an options flag that changes the return type via overloads. |
| `only(...names)` | `layer.only('anthropic').call(…)` reads as English. Rejected `using` (now a JS keyword), `with` (reserved-word smell), `pin` (jargon). |
| `supports(name)` | Type guard. Reads as a question; `has` would suggest a collection. |
| `on(event, fn) => Unsubscribe` | koa/hono/DOM-familiar. Returning the unsubscribe closure kills the "you must keep the same function reference for `off`" bug class. |
| `use(middleware)` | koa/hono/express. Do not invent a new word for the onion. |
| `close()` + `[Symbol.asyncDispose]` | `close` is what pools and sockets use. `dispose` alone would be unfamiliar to Node users. |
| `contract`, `providers` | Read-only introspection for tooling and tests. |

**Types**

`Layer`, `LayerConfig`, `Contract`, `ContractShape`, `Capability`, `CapabilityMeta`,
`CapabilityName`, `Provider`, `ProviderName`, `Handler`, `Handlers`, `CallContext`, `CallOptions`,
`CallResult`, `InputOf`, `OutputOf`, `Middleware`, `LayerEvents`, `LayerEventName`, `Unsubscribe`,
`Clock`, `ResiliencePolicy`, `RetryPolicy`, `BreakerPolicy`, `RateLimitPolicy`, `ConcurrencyPolicy`.

**Errors** — `code` is the discriminant; the classes exist for `instanceof` but most users only need
`isInterlayerError`.

`InterlayerError` (base: `code`, `capability`, `provider`, `retryable`, `cause`) · `ProviderError` ·
`TimeoutError` · `CircuitOpenError` (`retryAfterMs`) · `RateLimitedError` (`retryAfterMs`) ·
`CancelledError` · `NoProviderError` · `AllProvidersFailedError` (`failures: readonly
InterlayerError[]`) · `ConfigError` · `ErrorCode` · `isInterlayerError`.

`ErrorCode = 'TIMEOUT' | 'CIRCUIT_OPEN' | 'RATE_LIMITED' | 'CANCELLED' | 'PROVIDER_ERROR' |
'NO_PROVIDER' | 'ALL_FAILED' | 'CONFIG'`.

**`interlayer/testing`** — `fakeClock()`, `seededRandom(seed)`, `mockProvider(contract, name, handlers)`,
`recordEvents(layer)`.

Naming rules applied throughout: **`Ms` suffix on every duration** (`timeoutMs`, `baseMs`,
`resetAfterMs`, `retryAfterMs`) so no unit is ever ambiguous; **`attempts` means total attempts
including the first** (p-retry's `retries` off-by-one is the thing being avoided, and the different
word signals the different meaning); **`false` disables**, never `0`.

### 5.4 File ownership (for C1)

`src/contract.ts` · `src/provider.ts` · `src/layer.ts` · `src/errors.ts` · `src/events.ts` ·
`src/middleware.ts` · `src/resilience/{retry,timeout,breaker,ratelimit,concurrency}.ts` ·
`src/clock.ts` · `src/testing.ts` · `src/index.ts` (barrel). `contract.ts` + `errors.ts` are
dependencies of everything and should land first or be authored by one owner.

---

## 6. DX pitfalls to avoid

Each is traced to a library that made the mistake.

1. **Delimited composite identifiers.** `'openai:gpt-4o'` breaks the moment the id contains the
   delimiter — AI SDK #2056 splits on the *last* colon and silently sends the wrong model. Never
   parse structure out of a string. Two arguments.
2. **`(string & {})` escape hatches in public types.** AI SDK #6936: *"by not exporting the type,
   you allow it to be instantiated with any string, giving an error at runtime."* If we ever need an
   unchecked name, it goes behind an explicit `layer.callUnsafe`, not into the main union.
3. **Type-safety that evaporates when wrapped.** AI SDK #5745 — a registry you cannot extract types
   *from* is a registry you cannot build helpers *on*. Every union in our surface must be reachable:
   export `CapabilityName<C>`, `ProviderName<L>`, `InputOf`, `OutputOf` so users can write
   `function wrap<K extends CapabilityName<typeof ai>>(…)`.
4. **Assertion generics dressed as inference.** `keyv.get<string>('key')` proves nothing. Our
   generic is *inferred from an argument*, never supplied by the caller at the call site.
5. **Shipping types separately.** opossum → `@types/opossum` on DefinitelyTyped. Types are generated
   from our own source and versioned with the package.
6. **`retries` vs `attempts`.** p-retry's `retries: 10` performs 11 calls. Use `attempts` (total),
   document it in the JSDoc of the field itself, and assert it in a test.
7. **No jitter by default.** p-retry defaults `randomize: false`. Default to full jitter; a
   synchronised retry storm is worse than a slightly slower recovery.
8. **Fallbacks that swallow the cause.** opossum's `.fallback(fn)` hides the error unless you also
   subscribe to `failure`. Our `AllProvidersFailedError.failures` retains every suppressed error and
   `CallResult.errors` exposes them even on success.
9. **EventEmitter as the only error channel.** Keyv emits `'error'`; an unhandled `'error'` on a Node
   `EventEmitter` **throws**. Use a plain `Set<listener>` (also: zero deps), never let a
   listener-less event escalate, and never make an event the only way to learn a call failed.
10. **Undocumented composition order.** Polly's README shows `.AddRetry().AddTimeout()` and never
    says which wraps which; resilience4j's examples contradict each other. Publish the diagram in
    §2.5 in the README and pin it with a test.
11. **Options that silently do nothing.** LangChain's "standard parameters" apply only to some
    integrations. Ours: excess-property checking catches typos in object literals (verified —
    including *"Did you mean to write 'chat'?"*), and because a config passed as a variable escapes
    that check, `createLayer` also validates keys at runtime and throws `ConfigError`. A provider
    that ignores a capability's `idempotent` flag is not possible — the layer owns retry, not the
    provider.
12. **Silently forgetting `await next()`.** Koa's docs show the chain just stopping. Our middleware
    runner throws `ConfigError` if `next()` is called twice, and `CallResult` records whether the
    handler ran, so a middleware that never calls `next()` is visible rather than mysterious.
13. **`exactOptionalPropertyTypes` friction.** Verified: with `?: AbortSignal`, forwarding
    `AbortSignal | undefined` is `TS2379`. Every optional field in a *public input* type is declared
    `?: T | undefined`.
14. **Interfaces rejected by `Record<string, …>` constraints.** Verified: `const ai: AiContract`
    (an `interface`) fails `C extends Contract` with *"Argument of type 'AiContract' is not
    assignable to parameter of type 'Contract'"*, because interfaces have no implicit index
    signature. Fixed by the F-bounded `C extends ContractShape<C>`.
15. **Unreadable errors from inline generic key lookups.** Verified: `keyof LayerEvents<C, Names>`
    printed raw in the error; the named alias `LayerEventName` printed cleanly. Alias every public
    union.
16. **Type accumulation you can break by reformatting.** The chained-builder footgun (§4) — verified
    to silently lose all registered types when the return value is dropped.
17. **Hidden global state.** No module-level registry, no singleton default layer. Two layers, two
    contracts, one process, no interference.

---

## 7. README skeleton

```
# interlayer
> One typed interface. Many interchangeable providers. Retries, timeouts,
> circuit breaking and fallback included. Zero dependencies. Node 22+.

[badges: npm · CI · zero deps · types included]

## Why
  Three sentences + the before/after diff: N ad-hoc SDK wrappers with
  copy-pasted retry loops  ->  one contract, N providers, one call site.

## Install
  npm i interlayer            # no transitive dependencies

## 60-second example                      <- THE HERO. §5.2 steps 1-4 only,
  Declare a contract · implement two          ~30 lines, ends on the payoff
  providers · createLayer · call it.          comment `reply.text // string`
  Screenshot/code-fence of the editor showing `layer.call('chat', ⌄)`
  completing capability names, and the red squiggle on a wrong input field.

## Core concepts
  Contract · Capability · Provider · Layer · Route
  (one short paragraph each; one diagram: app -> layer -> [p1, p2, p3])

## Guide
  Defining a contract
  Writing a provider            (+ partial providers: implement a subset)
  Registering & fallback order
  Calling: `call` vs `callWithMeta`
  Cancellation & deadlines      (AbortSignal all the way down)
  Resilience
      the ordering diagram (§2.5) FIRST, then each policy
      retry · timeout · circuit breaker · rate limit · concurrency
      per-capability overrides · turning things off with `false`
  Routing & pinning a provider (`only`)
  Middleware
  Events & observability
  Errors                        (table: code -> class -> retryable -> meaning)
  Testing                       (fakeClock, seededRandom, mockProvider)

## Type inference
  How `call` knows the types. Reachable helper types
  (`InputOf`, `OutputOf`, `CapabilityName`, `ProviderName`) and how to write
  your own generic wrappers over a layer.  <- the thing AI SDK #5745 lacked

## Recipes
  Failover between two vendors · canary a new provider · shadow traffic ·
  per-tenant rate limits · streaming capabilities · migrating off a vendor

## API reference
  (generated; grouped exactly as §5.3)

## Design notes
  Why one config object instead of a fluent builder
  Why no delimited "provider:capability" ids
  The fixed strategy order, and why it is not configurable
  Comparison table: cockatiel · opossum · p-retry · AI SDK registry

## Compatibility · Versioning · Contributing · License
```

Hero-example rule: it must fit on one screen, contain **no** resilience configuration (that is the
*second* example), and end on a line where the reader can see a type they did not write.

---

## 8. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **Widening destroys precision.** Verified: annotating `const ps: Provider<typeof ai>[]` collapses `Names` to `string` *and* over-reports `Impl` as every contract key — the type says a capability exists that no provider implements, and it fails at runtime with `NO_PROVIDER`. Fails **open**, which is the wrong direction. | High | Document `satisfies` (verified to preserve precision) instead of annotation; make `createLayer` validate at construction and throw `ConfigError` listing contract capabilities with no provider; ship a lint-rule note. Consider a `strict: true` config flag that makes the unimplemented-capability check fatal. |
| R2 | Same failure via `const h: Partial<Handlers<C>> = {…}` authored outside `defineProvider`. Verified: `Impl` becomes all keys. | Medium | Docs: always pass the handlers literal *inline* to `defineProvider`. Same runtime check as R1 covers it. |
| R3 | Error messages that mention the contract print the whole structural type (`Handler<{ chat: Capability<…>; embed: Capability<…>; … }, "chat">`). Grows with contract size. | Medium | Keep contracts small / split by domain. Do not add further type parameters to `Handler`. Revisit if TS gains better aliasing in errors. |
| R4 | Type-instantiation depth / compile-time cost on large contracts (30+ capabilities × 10 providers). Not measured. | Medium | **Wave 2 must add a `tsc --extendedDiagnostics` budget check** on a synthetic large-contract fixture. Current design is shallow (no recursive conditionals; one distributive conditional over the provider tuple), so it should scale, but this is unmeasured. |
| R5 | `await using layer = createLayer(…)` **does not parse on Node 22** — verified `SyntaxError`. `Symbol.asyncDispose` itself does exist (verified). | Low | Implement `[Symbol.asyncDispose]` (verified working when invoked directly) but document `await layer.close()` as the Node 22 form. Do not put `await using` in the README hero. |
| R6 | The `Middleware` signature is currently untyped in `input`/return (`unknown`) because a middleware spans all capabilities. | Medium | Acceptable for v1 — middleware is cross-cutting by nature. If per-capability typing is wanted, add `layer.useFor('chat', mw)` later; do not retrofit generics onto `use`. |
| R7 | `capability<In, Out>()` has **no runtime validation** by default, so a provider returning the wrong shape is only caught by types. | Medium | The optional `parse` hook (zero-dep, zod-compatible) covers inputs. Outputs are the provider author's responsibility; note it explicitly in the provider-authoring guide. |
| R8 | Fixed strategy order will not suit everyone (e.g. someone wants the breaker outside retry). | Low | Documented as a v1 constraint with the rationale; `only()` + a hand-rolled outer loop is the escape hatch. |
| R9 | The single cast `meta as Capability<In, Out>` in `capability()` is load-bearing — the phantom property never exists at runtime. | Low | Isolated to one line; add a test asserting `Object.keys(capability())` contains no symbol keys, so nobody later "fixes" it by materialising the carrier. |
| R10 | Conflicts with sibling streams: R3 owns registry/adapter internals and R4 owns resilience semantics. This document fixes the *names and order* those internals must expose. | Medium | Orchestrator should reconcile §2.5 (ordering) and §5.3 (policy field names) against R3/R4 before Wave 2 starts. Names in §5.3 are the API contract; R4 owns what happens behind them. |
