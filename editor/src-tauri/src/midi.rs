//! USB-MIDI bridge between the Kemper (Player) and the bosun pedal.
//!
//! The Kemper Player is USB-MIDI only and the MIDI Captain pedal is a USB
//! device (not a host), so the two cannot talk directly - the PC must relay
//! MIDI both ways. This is the in-editor equivalent of tools/midi_bridge.py:
//! it forwards every message (SYSEX included - that carries the Kemper
//! bidirectional protocol) between the Player and the pedal, dropping only
//! clock / active-sensing to keep the link quiet.
//!
//! It opens the devices' USB-MIDI interfaces, which are separate from the
//! editor's CDC serial connection, so the bridge and the editor run together.

use std::sync::Mutex;

use midir::{Ignore, MidiInput, MidiInputConnection, MidiOutput};
use serde::Serialize;
use tauri::State;

#[derive(Default)]
pub struct MidiState {
    pub bridge: Mutex<Option<BridgeHandle>>,
}

pub struct BridgeHandle {
    // Holding the two input connections keeps the bridge alive; dropping them
    // (set the Option to None) tears it down. Each input callback owns the
    // matching output connection, so those are released here too.
    _kemper_in: MidiInputConnection<()>,
    _pedal_in: MidiInputConnection<()>,
    kemper_port: String,
    pedal_port: String,
}

#[derive(Serialize)]
pub struct MidiPorts {
    pub inputs: Vec<String>,
    pub outputs: Vec<String>,
}

#[derive(Serialize)]
pub struct BridgeStatus {
    pub active: bool,
    pub kemper_port: Option<String>,
    pub pedal_port: Option<String>,
}

/// First port name that contains any of `needles` (case-insensitive).
fn find_name(names: &[String], needles: &[&str]) -> Option<String> {
    names
        .iter()
        .find(|n| {
            let low = n.to_lowercase();
            needles.iter().any(|s| low.contains(s))
        })
        .cloned()
}

/// Clock (0xF8) and active-sensing (0xFE) flood the link and aren't needed by
/// the Kemper bidirectional protocol - everything else (notably SYSEX) is
/// forwarded verbatim.
fn should_forward(msg: &[u8]) -> bool {
    !matches!(msg.first(), Some(0xF8) | Some(0xFE))
}

#[tauri::command]
pub fn midi_list_ports() -> Result<MidiPorts, String> {
    let mi = MidiInput::new("bosun-scan-in").map_err(|e| e.to_string())?;
    let mo = MidiOutput::new("bosun-scan-out").map_err(|e| e.to_string())?;
    let inputs = mi.ports().iter().filter_map(|p| mi.port_name(p).ok()).collect();
    let outputs = mo.ports().iter().filter_map(|p| mo.port_name(p).ok()).collect();
    Ok(MidiPorts { inputs, outputs })
}

#[tauri::command]
pub fn midi_bridge_status(state: State<MidiState>) -> BridgeStatus {
    match state.bridge.lock() {
        Ok(g) => match g.as_ref() {
            Some(h) => BridgeStatus {
                active: true,
                kemper_port: Some(h.kemper_port.clone()),
                pedal_port: Some(h.pedal_port.clone()),
            },
            None => BridgeStatus { active: false, kemper_port: None, pedal_port: None },
        },
        Err(_) => BridgeStatus { active: false, kemper_port: None, pedal_port: None },
    }
}

#[tauri::command]
pub fn midi_bridge_stop(state: State<MidiState>) {
    if let Ok(mut g) = state.bridge.lock() {
        *g = None; // dropping BridgeHandle closes both input + output connections
    }
}

/// Open the relay. `kemper` / `pedal` are optional substring hints to
/// disambiguate when several MIDI devices are present; without them we
/// auto-detect by the usual device names.
#[tauri::command]
pub fn midi_bridge_start(
    state: State<MidiState>,
    kemper: Option<String>,
    pedal: Option<String>,
) -> Result<BridgeStatus, String> {
    // Tear down any existing bridge first so a restart can't leak connections.
    {
        let mut g = state.bridge.lock().map_err(|_| "lock poisoned")?;
        *g = None;
    }

    // Resolve port names. Inputs and outputs are enumerated separately because
    // their names can differ; we match each side by the same needle(s).
    let kemper_needles: Vec<&str> = match kemper.as_deref() {
        Some(s) => vec![s],
        None => vec!["profiler", "kemper"],
    };
    let pedal_needles: Vec<&str> = match pedal.as_deref() {
        Some(s) => vec![s],
        None => vec!["circuitpython", "bosun", "captain"],
    };

    let scan_in = MidiInput::new("bosun-resolve-in").map_err(|e| e.to_string())?;
    let scan_out = MidiOutput::new("bosun-resolve-out").map_err(|e| e.to_string())?;
    let in_names: Vec<String> =
        scan_in.ports().iter().filter_map(|p| scan_in.port_name(p).ok()).collect();
    let out_names: Vec<String> =
        scan_out.ports().iter().filter_map(|p| scan_out.port_name(p).ok()).collect();

    let kemper_in_name = find_name(&in_names, &kemper_needles)
        .ok_or("Kemper MIDI input not found - is the Kemper plugged in over USB?")?;
    let kemper_out_name = find_name(&out_names, &kemper_needles)
        .ok_or("Kemper MIDI output not found - is the Kemper plugged in over USB?")?;
    let pedal_in_name = find_name(&in_names, &pedal_needles)
        .ok_or("Pedal MIDI input not found - is the pedal plugged in over USB?")?;
    let pedal_out_name = find_name(&out_names, &pedal_needles)
        .ok_or("Pedal MIDI output not found - is the pedal plugged in over USB?")?;

    // Open the two outputs (written from the opposite side's input callback).
    let pedal_out = MidiOutput::new("bosun-bridge-pedal-out").map_err(|e| e.to_string())?;
    let pedal_out_port = pedal_out
        .ports()
        .into_iter()
        .find(|p| pedal_out.port_name(p).map(|n| n == pedal_out_name).unwrap_or(false))
        .ok_or("pedal output port disappeared")?;
    let mut pedal_out_conn =
        pedal_out.connect(&pedal_out_port, "bosun-bridge").map_err(|e| e.to_string())?;

    let kemper_out = MidiOutput::new("bosun-bridge-kemper-out").map_err(|e| e.to_string())?;
    let kemper_out_port = kemper_out
        .ports()
        .into_iter()
        .find(|p| kemper_out.port_name(p).map(|n| n == kemper_out_name).unwrap_or(false))
        .ok_or("Kemper output port disappeared")?;
    let mut kemper_out_conn =
        kemper_out.connect(&kemper_out_port, "bosun-bridge").map_err(|e| e.to_string())?;

    // Kemper in -> pedal out. ignore(None) is required so SYSEX is delivered to
    // the callback (midir suppresses it by default); we drop clock/AS ourselves.
    let mut kin = MidiInput::new("bosun-bridge-kemper-in").map_err(|e| e.to_string())?;
    kin.ignore(Ignore::None);
    let kin_port = kin
        .ports()
        .into_iter()
        .find(|p| kin.port_name(p).map(|n| n == kemper_in_name).unwrap_or(false))
        .ok_or("Kemper input port disappeared")?;
    let kemper_in_conn = kin
        .connect(
            &kin_port,
            "bosun-bridge",
            move |_ts, msg, _| {
                if should_forward(msg) {
                    let _ = pedal_out_conn.send(msg);
                }
            },
            (),
        )
        .map_err(|e| e.to_string())?;

    // Pedal in -> Kemper out.
    let mut pin = MidiInput::new("bosun-bridge-pedal-in").map_err(|e| e.to_string())?;
    pin.ignore(Ignore::None);
    let pin_port = pin
        .ports()
        .into_iter()
        .find(|p| pin.port_name(p).map(|n| n == pedal_in_name).unwrap_or(false))
        .ok_or("pedal input port disappeared")?;
    let pedal_in_conn = pin
        .connect(
            &pin_port,
            "bosun-bridge",
            move |_ts, msg, _| {
                if should_forward(msg) {
                    let _ = kemper_out_conn.send(msg);
                }
            },
            (),
        )
        .map_err(|e| e.to_string())?;

    let mut g = state.bridge.lock().map_err(|_| "lock poisoned")?;
    *g = Some(BridgeHandle {
        _kemper_in: kemper_in_conn,
        _pedal_in: pedal_in_conn,
        kemper_port: kemper_in_name.clone(),
        pedal_port: pedal_in_name.clone(),
    });

    Ok(BridgeStatus {
        active: true,
        kemper_port: Some(kemper_in_name),
        pedal_port: Some(pedal_in_name),
    })
}
