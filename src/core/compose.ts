import { ConfigError } from './errors.ts';
import type { Composed, Middleware, Next, Terminal } from './types.ts';

/**
 * Compose middleware into a single callable, koa-style.
 *
 * Differences from `koa-compose`, all deliberate:
 *  1. `next()` RESOLVES TO THE DOWNSTREAM VALUE (koa returns void and mutates
 *     `ctx.body`). This keeps the result in the type system.
 *  2. `next()` is RE-ENTRANT: a middleware may call it several times in
 *     sequence, each call re-running a fresh downstream chain. That is what
 *     makes `retry` expressible as ordinary middleware. Concurrent (unawaited)
 *     re-entry is still rejected — it catches koa's classic "missing await".
 *  3. `next(ctx')` may REPLACE the context for everything downstream, which is
 *     how `timeout` narrows the AbortSignal without mutating shared state.
 */
export function compose<Ctx, R>(middleware: readonly Middleware<Ctx, R>[]): Composed<Ctx, R> {
  for (let i = 0; i < middleware.length; i++) {
    if (typeof middleware[i] !== 'function') {
      throw new ConfigError(`Middleware[${i}] is not a function`, [
        { path: ['middleware', i], message: 'expected a function', code: 'invalid_type' },
      ]);
    }
  }
  const stack = middleware.slice();

  return function run(ctx: Ctx, terminal: Terminal<Ctx, R>): Promise<R> {
    function dispatch(i: number, current: Ctx): Promise<R> {
      const fn = stack[i];
      if (fn === undefined) {
        try {
          return Promise.resolve(terminal(current));
        } catch (err) {
          return Promise.reject(err);
        }
      }
      let pending = false;
      const next: Next<Ctx, R> = (replacement?: Ctx) => {
        if (pending) {
          return Promise.reject(
            new ConfigError(
              `Middleware[${i}] called next() again before the previous call settled. ` +
                'Await the previous next() (retry loops must be sequential).',
              [],
            ),
          );
        }
        pending = true;
        return dispatch(i + 1, replacement ?? current).then(
          (v) => {
            pending = false;
            return v;
          },
          (e: unknown) => {
            pending = false;
            throw e;
          },
        );
      };
      try {
        return Promise.resolve(fn(current, next));
      } catch (err) {
        return Promise.reject(err);
      }
    }

    return dispatch(0, ctx);
  };
}

/** `compose` for the common case where the terminal is already bound. */
export function pipeline<Ctx, R>(
  middleware: readonly Middleware<Ctx, R>[],
  terminal: Terminal<Ctx, R>,
): (ctx: Ctx) => Promise<R> {
  const run = compose(middleware);
  return (ctx: Ctx) => run(ctx, terminal);
}
