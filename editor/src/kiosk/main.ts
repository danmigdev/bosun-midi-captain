import { mount } from "svelte";
import "./kiosk.css";
import KioskApp from "./KioskApp.svelte";

// The Pi can hide its pointer without disabling mouse or touch controls.
if (new URLSearchParams(location.search).get("cursor") === "hidden") {
  document.documentElement.dataset.kioskCursor = "hidden";
}

mount(KioskApp, { target: document.getElementById("app")! });
