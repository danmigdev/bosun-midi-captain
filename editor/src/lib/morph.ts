import type { Binding } from "./protocol";

export const morphPercent = (value: number): number => Math.round(value * 100 / 127);
export const morphValue = (percent: number): number => Math.round(Math.max(0, Math.min(100, percent)) * 127 / 100);

export function morphButtonBinding(sw: string, channel = 1): Binding {
  return {
    switch: sw, mode: "momentary", label: "MORPH",
    led: { on: "#ef767a", off: "#5799e5" },
    actions: {
      press: { messages: [{ type: "kemper_morph_trigger", channel, state: "on" }] },
      release: { messages: [{ type: "kemper_morph_trigger", channel, state: "off" }] },
    },
  };
}
