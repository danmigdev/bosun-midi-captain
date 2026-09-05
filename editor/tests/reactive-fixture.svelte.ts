/** Match the reactive props supplied by App/Settings to their child editors. */
export function reactiveFixture<T>(value: T): T {
  const state = $state(value);
  return state;
}
