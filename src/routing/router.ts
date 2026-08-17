/**
 * The router — U6, `src/routing/`.
 *
 * The router is the TERMINAL of the call stack and the driver of the attempt
 * stack (R3 §2.1):
 *
 *   call stack  (compose<CallContext>)   runs once per `layer.call()`
 *     └── TERMINAL: router.route(ctx, candidates, attempt)
 *          └── for each candidate, in selector order:
 *               attempt stack (compose<AttemptContext>)  runs once per provider
 *                 └── TERMINAL: the provider handler
 *
 * It operates on the `Policy` INTERFACE only. It does not know that retry,
 * circuit breaking, rate limiting or timeouts exist — it receives `Policy`
 * objects, orders them with `policyStack()` and composes them. That is exactly
 * what lets U2–U6 be built in parallel; concrete wiring happens post-wave in
 * `src/layer.ts`.
 *
 * Event ownership is deliberately narrow. The router emits `provider:selected`
 * and `fallback:advance` (nothing else can), and counts `stats.attempts` at the
 * terminal because the terminal IS the physical-call boundary — it sits inside
 * every attempt policy, including a re-entrant retry. It does NOT emit
 * `attempt:*` (retry owns `willRetry`) or `call:*` (the facade owns those).
 */

import { compose } from '../core/compose.ts';
import { NoProviderError } from '../core/errors.ts';
import { type AttemptPolicy, disposePolicies, policyStack } from '../core/policy.ts';
import type {
  AttemptContext,
  CallContext,
  Composed,
  ProviderRecord,
  Router,
  RouterOptions,
  Selector,
} from '../core/types.ts';
import { type FallbackOptions, runFallbackChain } from './fallback.ts';
import { byHints, chainSelectors, defaultSelector } from './selectors.ts';

/**
 * The provider call, given the attempt context the policies actually produced.
 *
 * `Router.route` (core) hands the callback only the provider, which is enough
 * for a caller that closes over its own context but NOT enough for a policy
 * that narrowed the context on the way down — an attempt timeout replaces
 * `ctx.signal` via `next(ctx')`, and the handler must see the replacement.
 * {@link PolicyRouter.routeWith} is the form that passes it through.
 */
export type AttemptRunner = (provider: ProviderRecord, ctx: AttemptContext) => Promise<unknown>;

export interface CreateRouterOptions extends RouterOptions {
  /**
   * Attempt-scope policies, in any order — `policyStack()` sorts them into the
   * canonical nesting (retry -> rate limit -> breaker -> attempt timeout ->
   * handler). Call-scope policies in this list are ignored: they belong to the
   * call stack, outside the router.
   */
  readonly policies?: readonly AttemptPolicy[] | undefined;
}

export interface PolicyRouter extends Router {
  /** `route`, with the policy-narrowed `AttemptContext` handed to the terminal. */
  routeWith(
    ctx: CallContext,
    candidates: readonly ProviderRecord[],
    attempt: AttemptRunner,
  ): Promise<unknown>;
  /** The attempt stack this router composes, as supplied. */
  readonly policies: readonly AttemptPolicy[];
  /** The effective selector, hint handling included. */
  readonly selector: Selector;
  /** Disposes every policy that declares a teardown. Never rejects. */
  dispose(): Promise<void>;
}

/**
 * Builds the terminal of the call stack.
 *
 * ```ts
 * const router = createRouter({ policies: [retry(), breaker()], selector: weighted() });
 * const value = await router.routeWith(ctx, registry.candidatesFor('chat'), (p, actx) =>
 *   p.capabilities.get('chat')?.(actx.input, actx) ?? Promise.reject(new Error('unsupported')),
 * );
 * ```
 */
export function createRouter(options: CreateRouterOptions = {}): PolicyRouter {
  const policies = options.policies ?? [];
  // Hints first, so `CallOptions.tags` / `.providers` are honoured whichever
  // selector is installed; applying them twice is a no-op.
  const selector: Selector = chainSelectors(byHints(), options.selector ?? defaultSelector);
  const runAttemptStack: Composed<AttemptContext, unknown> = compose<AttemptContext, unknown>(
    policyStack(policies, 'attempt'),
  );
  const fallbackOptions: FallbackOptions = {
    maxProviders: options.maxProviders,
    shouldFallback: options.shouldFallback,
  };

  function routeWith(
    ctx: CallContext,
    candidates: readonly ProviderRecord[],
    attempt: AttemptRunner,
  ): Promise<unknown> {
    if (candidates.length === 0) {
      return Promise.reject(
        new NoProviderError(`No provider available for "${ctx.capability}"`, {
          callId: ctx.callId,
          capability: ctx.capability,
        }),
      );
    }

    const selected = selector(candidates, ctx);
    if (selected.length === 0) {
      return Promise.reject(
        new NoProviderError(
          `No provider matched the routing hints for "${ctx.capability}" ` +
            `(${String(candidates.length)} candidate(s) before filtering)`,
          { callId: ctx.callId, capability: ctx.capability },
        ),
      );
    }

    return runFallbackChain(
      ctx,
      selected,
      (provider: ProviderRecord): Promise<unknown> => {
        // A fresh attempt context per candidate: `attempt` is 1-based PER
        // PROVIDER, and retry bumps it downstream via `next(ctx')`.
        const attemptCtx: AttemptContext = { ...ctx, provider, attempt: 1 };
        return runAttemptStack(attemptCtx, (final: AttemptContext): Promise<unknown> => {
          final.stats.attempts++;
          return attempt(final.provider, final);
        });
      },
      fallbackOptions,
    );
  }

  return {
    policies,
    selector,
    routeWith,
    route(
      ctx: CallContext,
      candidates: readonly ProviderRecord[],
      attempt: (provider: ProviderRecord) => Promise<unknown>,
    ): Promise<unknown> {
      return routeWith(
        ctx,
        candidates,
        (provider: ProviderRecord): Promise<unknown> => attempt(provider),
      );
    },
    dispose(): Promise<void> {
      return disposePolicies(policies);
    },
  };
}
