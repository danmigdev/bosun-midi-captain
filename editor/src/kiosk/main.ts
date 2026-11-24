import { mount } from "svelte";
import "./kiosk.css";
import KioskApp from "./KioskApp.svelte";
import { startHeartbeat } from "./heartbeat";

mount(KioskApp, { target: document.getElementById("app")! });
// After mount: a page whose Stage cannot start must not report itself alive.
startHeartbeat();
