// Tiny pub/sub state store. Immutable replace; subscribers get the whole state.
// One central store keeps panel modules independent — they subscribe to slices
// in their own code rather than relying on a framework.

export function createStore(initial) {
  let state = initial;
  const subs = new Set();
  return {
    get() { return state; },
    set(patch) {
      state = { ...state, ...patch };
      for (const fn of subs) fn(state);
    },
    update(fn) {
      state = fn(state);
      for (const cb of subs) cb(state);
    },
    subscribe(fn) {
      subs.add(fn);
      fn(state);
      return () => subs.delete(fn);
    },
  };
}
