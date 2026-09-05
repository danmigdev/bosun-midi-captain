import { mount } from "svelte";
import "./kiosk.css";
import KioskApp from "./KioskApp.svelte";

mount(KioskApp, { target: document.getElementById("app")! });
