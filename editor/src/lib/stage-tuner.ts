// Kemper reports a 14-bit deviation centred on 8192. It is a relative pitch
// indicator: do not label it in cents without a documented conversion.
export function tunerReading(note: unknown, deviance: unknown) {
  const name = typeof note === "string" && /^[A-G](?:#|b)?$/.test(note) ? note : null;
  const valid = name !== null && typeof deviance === "number"
    && Number.isFinite(deviance) && deviance >= 0 && deviance <= 16383;
  const offset = valid ? (deviance as number) - 8192 : 0;
  const direction = !valid ? "waiting" : Math.abs(offset) <= 200 ? "center"
    : offset < 0 ? "flat" : "sharp";
  return {
    note: name?.[0] ?? "—",
    accidental: name?.slice(1).replace("#", "♯").replace("b", "♭") ?? "",
    valid,
    direction,
    position: valid ? offset / (offset < 0 ? 8192 : 8191) : 0,
    status: direction === "waiting" ? "Play a note" : direction === "center" ? "In tune"
      : direction === "flat" ? "Tune up" : "Tune down",
  };
}
