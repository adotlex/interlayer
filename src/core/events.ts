import type { Emitter, EventMap, InterlayerEvents, Unsubscribe } from './types.ts';

/**
 * Zero-dependency typed emitter — R3 §5.4.
 *
 * Two properties chosen deliberately against Node's `EventEmitter`:
 *  - dispatch is SYNCHRONOUS, so event order is deterministic and assertable
 *    against the injected clock;
 *  - a throwing listener CANNOT break the call path — it is routed to
 *    `listener:error`. Node's emitter throws on an unhandled `'error'` event,
 *    which would let an observer crash a request.
 *
 * `on` returns its own unsubscribe closure, so there is no "you must keep the
 * same function reference for `off`" bug class.
 */

type AnyListener = (payload: never) => void;
type AnyWildcard = (event: string, payload: never) => void;

export function createEmitter<E extends EventMap>(): Emitter<E> {
  const listeners = new Map<string, Set<AnyListener>>();
  const wildcards = new Set<AnyWildcard>();

  function add(event: string, fn: AnyListener): Unsubscribe {
    let set = listeners.get(event);
    if (set === undefined) {
      set = new Set();
      listeners.set(event, set);
    }
    const target = set;
    target.add(fn);
    return () => {
      target.delete(fn);
      if (target.size === 0) listeners.delete(event);
    };
  }

  function reportListenerError(event: string, error: unknown): void {
    const set = listeners.get('listener:error');
    if (set === undefined || set.size === 0 || event === 'listener:error') return;
    for (const fn of [...set]) {
      try {
        (fn as (p: unknown) => void)({ event, error, at: 0 });
      } catch {
        /* give up */
      }
    }
  }

  const emitter: Emitter<E> = {
    on(event, fn) {
      return add(event, fn as AnyListener);
    },
    once(event, fn) {
      const wrapped = ((p: never) => {
        off();
        (fn as AnyListener)(p);
      }) as AnyListener;
      const off = add(event, wrapped);
      return off;
    },
    off(event, fn) {
      const set = listeners.get(event);
      if (set === undefined) return;
      set.delete(fn as AnyListener);
      if (set.size === 0) listeners.delete(event);
    },
    onAny(fn) {
      wildcards.add(fn as AnyWildcard);
      return () => {
        wildcards.delete(fn as AnyWildcard);
      };
    },
    emit(event, payload) {
      const set = listeners.get(event);
      if (set !== undefined) {
        // Snapshot: a listener may unsubscribe itself or others during dispatch.
        for (const fn of [...set]) {
          try {
            (fn as (p: unknown) => void)(payload);
          } catch (error) {
            reportListenerError(event, error);
          }
        }
      }
      if (wildcards.size > 0) {
        for (const fn of [...wildcards]) {
          try {
            (fn as (e: string, p: unknown) => void)(event, payload);
          } catch (error) {
            reportListenerError(event, error);
          }
        }
      }
    },
    listenerCount(event) {
      return listeners.get(event)?.size ?? 0;
    },
    removeAllListeners(event) {
      if (event === undefined) {
        listeners.clear();
        wildcards.clear();
      } else {
        listeners.delete(event);
      }
    },
  };

  return emitter;
}

export type InterlayerEmitter = Emitter<InterlayerEvents>;
