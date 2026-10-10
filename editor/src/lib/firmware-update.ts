// Version comparison behind the firmware update offers (see unified-update.ts).

/** Semver-ish compare. Strips any pre-release suffix (`-native`, `-rc.1`,
 * …) so a firmware build like `0.6.5-native` is treated as `0.6.5`.
 * Positive when `a > b`, negative when `a < b`, zero on equal. */
export function compareVersions(a: string, b: string): number {
  const sanitize = (v: string) => v.split("-")[0];
  const pa = sanitize(a).split(".").map(n => parseInt(n, 10) || 0);
  const pb = sanitize(b).split(".").map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const da = pa[i] || 0, db = pb[i] || 0;
    if (da !== db) return da - db;
  }
  return 0;
}
