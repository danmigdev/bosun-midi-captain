import { mount } from "svelte";
import SetupWizard from "./components/SetupWizard.svelte";
mount(SetupWizard, { target: document.getElementById("app")!, props: { standalone: true } });
