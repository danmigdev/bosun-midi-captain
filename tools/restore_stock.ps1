<#
.SYNOPSIS
  Reinstall the original PaintAudio/PySwitch MIDI Captain firmware from a backup
  ZIP, leaving the pedal exactly as it was before bosun was ever installed.

.DESCRIPTION
  A bosun install flashes CircuitPython 9.x, while the stock backup is a
  CircuitPython 7.3.3 filesystem whose lib/*.mpy will not import on CP9. So a
  faithful "undo" is not a file copy - it is a full re-flash:

    1. Enter the RP2040 ROM bootloader (RPI-RP2).
    2. (default) Erase flash with flash_nuke.uf2  -> no bosun residue, fresh FAT.
    3. Flash the exact CircuitPython the backup expects (read from boot_out.txt).
    4. Copy the backup tree onto the fresh CIRCUITPY drive.

  The CircuitPython version and board id are read FROM the backup's boot_out.txt,
  so this script restores any compatible backup, not just one hard-coded build.

  UF2 images are downloaded once and cached under
  %LOCALAPPDATA%\bosun-restore (reused offline afterwards).

.PARAMETER BackupZip
  Path to the stock backup ZIP. Default: the 2026-06-17 desktop snapshot.

.PARAMETER Port
  COM port of the pedal, used to drop into the bootloader over the REPL.
  If omitted, the pedal is auto-detected by its USB VID (no prompt); only an
  ambiguous setup falls back to asking. The final reboot is also automatic, so
  a normal run needs no port argument and no physical power-cycle.

.PARAMETER ManualBootloader
  Do not touch any serial port. Wait for you to put the pedal into RPI-RP2
  mode yourself (double-tap RESET / hold BOOTSEL while plugging in).

.PARAMETER SkipNuke
  Skip the flash_nuke erase step. Faster, but bosun files that are not part of
  the backup could linger. Leave nuke on for a true factory-clean restore.

.PARAMETER AssumeYes
  Do not prompt for confirmation. Use only in unattended runs.

.EXAMPLE
  pwsh -File tools\restore_stock.ps1

.EXAMPLE
  pwsh -File tools\restore_stock.ps1 -Port COM4 -AssumeYes

.EXAMPLE
  pwsh -File tools\restore_stock.ps1 -ManualBootloader
#>

[CmdletBinding()]
param(
    [string] $BackupZip = "C:\Users\danmigdev\Desktop\midi_captain_backup_20260617.zip",
    [string] $Port,
    [switch] $ManualBootloader,
    [switch] $SkipNuke,
    [switch] $AssumeYes
)

$ErrorActionPreference = "Stop"
$NUKE_URL = "https://datasheets.raspberrypi.com/soft/flash_nuke.uf2"
$CACHE    = Join-Path $env:LOCALAPPDATA "bosun-restore"

function Info($m)  { Write-Host "[info] $m" }
function Ok($m)    { Write-Host "[ok  ] $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Step($m)  { Write-Host "`n=== $m ===" -ForegroundColor Cyan }

# ---------------------------------------------------------------- drive helpers

# Drive letter (e.g. "E:") of the first volume with the given label, or $null.
function Get-DriveByLabel([string]$Label) {
    $v = Get-Volume -ErrorAction SilentlyContinue |
         Where-Object { $_.FileSystemLabel -eq $Label -and $_.DriveLetter } |
         Select-Object -First 1
    if ($v) { return "$($v.DriveLetter):" }
    return $null
}

# Block until a labelled volume appears (or disappears), returns the drive or $null.
function Wait-Drive([string]$Label, [int]$TimeoutSec = 40, [switch]$Gone) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $d = Get-DriveByLabel $Label
        if ($Gone) { if (-not $d) { return $null } }
        else       { if ($d)      { return $d } }
        Start-Sleep -Milliseconds 600
    }
    return (Get-DriveByLabel $Label)
}

# Copy a UF2 to a bootloader drive. The board reboots mid-copy, so a vanished
# destination or a write error at the tail is expected and treated as success.
function Send-Uf2([string]$Uf2, [string]$Drive) {
    $dst = Join-Path $Drive (Split-Path $Uf2 -Leaf)
    try {
        Copy-Item -LiteralPath $Uf2 -Destination $dst -Force -ErrorAction Stop
    } catch {
        # Expected: the RP2040 resets as soon as the full image lands.
        Info "copy returned '$($_.Exception.Message.Trim())' (board likely reset - normal)"
    }
}

# ---------------------------------------------------------------- downloads

function Get-Cached([string]$Url, [string]$FileName) {
    New-Item -ItemType Directory -Force -Path $CACHE | Out-Null
    $out = Join-Path $CACHE $FileName
    if (Test-Path $out) { Ok "cached $FileName"; return $out }
    Info "downloading $FileName"
    Invoke-WebRequest -Uri $Url -OutFile $out -UseBasicParsing
    Ok "downloaded $FileName"
    return $out
}

# ---------------------------------------------------------------- bootloader entry

# Drive the CircuitPython REPL with a block of commands. Broadcasts to every
# open-able port (the console runs the REPL; the data CDC just ignores it) and
# asserts DTR/RTS - this mirrors the editor's serial::reboot_to_bootloader,
# which is the proven recipe. $Body is the python the REPL should run.
function Send-ReplCommands([string]$Body, [string]$What) {
    $names = [System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object
    if ($Port) { $names = @($Port) }   # honor explicit -Port if given
    if (-not $names) { Warn "no serial ports to send $What to"; return $false }
    $sent = $false
    foreach ($name in $names) {
        $sp = $null
        try {
            $sp = New-Object System.IO.Ports.SerialPort($name, 115200)
            $sp.WriteTimeout = 800
            $sp.DtrEnable = $true
            $sp.RtsEnable = $true
            $sp.Open()
            $sp.Write([byte[]](0x03), 0, 1)        # single Ctrl-C: break into REPL
            Start-Sleep -Milliseconds 300
            $sp.Write($Body)                        # \r\n ... \r\n terminated
            Start-Sleep -Milliseconds 250
            Info "sent $What to $name"
            $sent = $true
        } catch {
            Info "skip $name ($($_.Exception.Message.Trim()))"
        } finally {
            try { if ($sp) { $sp.Close() } } catch {}
        }
    }
    return $sent
}

# CRITICAL: RP2040/CircuitPython 9 needs RunMode.UF2 (RunMode.BOOTLOADER does NOT
# enter the ROM bootloader there). getattr() keeps the CP7 fallback working too.
$REPL_TO_BOOTLOADER = "`r`nimport microcontroller`r`nmicrocontroller.on_next_reset(getattr(microcontroller.RunMode,'UF2',microcontroller.RunMode.BOOTLOADER))`r`nmicrocontroller.reset()`r`n"
$REPL_HARD_RESET    = "`r`nimport microcontroller`r`nmicrocontroller.reset()`r`n"

function Enter-Bootloader-ViaRepl() { return (Send-ReplCommands $REPL_TO_BOOTLOADER "bootloader request") }

# Find the pedal's CDC port by USB VID, with no user input.
# CircuitPython (bosun console AND a freshly-flashed stock unit) enumerates as
# Adafruit VID 239A; the raw Pico bootloader/UART is RaspberryPi VID 2E8A.
# When several CDC interfaces match, prefer interface 0 (MI_00 = the REPL console).
function Find-PedalPort() {
    $cand = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '\(COM\d+\)' -and
                           ($_.PNPDeviceID -match 'VID_239A' -or $_.PNPDeviceID -match 'VID_2E8A') }
    if (-not $cand) { return $null }
    $ranked = $cand | Sort-Object `
        @{ Expression = { if ($_.PNPDeviceID -match 'VID_239A') { 0 } else { 1 } } }, `
        @{ Expression = { if ($_.PNPDeviceID -match 'MI_00') { 0 } else { 1 } } }, `
        @{ Expression = { [int]([regex]::Match($_.Name, 'COM(\d+)').Groups[1].Value) } }
    $best = $ranked | Select-Object -First 1
    if ($best.Name -match '\((COM\d+)\)') { return $Matches[1] }
    return $null
}

# Hard-reset a freshly-flashed CircuitPython unit over the REPL so the new boot.py
# runs (which hides the drive) and the stock firmware boots - no physical replug.
function Reset-Pedal-ViaRepl() { return (Send-ReplCommands $REPL_HARD_RESET "hard-reset") }

# ================================================================ main

Step "Stock firmware restore"

if (-not (Test-Path -LiteralPath $BackupZip)) { throw "Backup ZIP not found: $BackupZip" }
Info "backup: $BackupZip"

# 1. Extract the backup and learn what to restore TO.
$work = Join-Path $env:TEMP ("bosun-restore-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
    Info "extracting backup..."
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($BackupZip, $work)

    $bootOut = Join-Path $work "boot_out.txt"
    $cpVer = "7.3.3"; $board = "raspberry_pi_pico"
    if (Test-Path $bootOut) {
        $txt = Get-Content $bootOut -Raw
        if ($txt -match "CircuitPython\s+(\d+\.\d+\.\d+)") { $cpVer = $Matches[1] }
        if ($txt -match "Board ID:\s*([\w_]+)")           { $board = $Matches[1] }
        Ok "backup targets CircuitPython $cpVer on $board"
    } else {
        Warn "no boot_out.txt in backup; assuming CircuitPython $cpVer / $board"
    }

    # 2. Fetch the UF2 images the backup needs.
    Step "Preparing firmware images"
    $cpUrl = "https://downloads.circuitpython.org/bin/$board/en_US/adafruit-circuitpython-$board-en_US-$cpVer.uf2"
    $cpUf2 = Get-Cached $cpUrl "circuitpython-$board-$cpVer.uf2"
    $nukeUf2 = $null
    if (-not $SkipNuke) { $nukeUf2 = Get-Cached $NUKE_URL "flash_nuke.uf2" }

    # Count what we will copy back (excluding host junk).
    $excludeDirs  = @(".fseventsd", "__pycache__")
    $excludeFiles = @("boot_out.txt", ".metadata_never_index", ".Trashes", ".DS_Store", "Thumbs.db")
    $payload = Get-ChildItem -LiteralPath $work -Recurse -File |
               Where-Object {
                   $rel = $_.FullName.Substring($work.Length).TrimStart('\')
                   $top = ($rel -split '\\')[0]
                   ($_.Name -notin $excludeFiles) -and
                   (-not ($rel -split '\\' | Where-Object { $_ -in $excludeDirs }))
               }
    Info "$($payload.Count) files to restore after the flash"

    # 3. Confirm (destructive).
    Step "Plan"
    Write-Host "  - Pedal will be ERASED" $(if ($SkipNuke) { "(nuke skipped)" } else { "(flash_nuke)" })
    Write-Host "  - CircuitPython $cpVer ($board) will be flashed"
    Write-Host "  - $($payload.Count) backup files will be copied onto CIRCUITPY"
    if (-not $AssumeYes) {
        $ans = Read-Host "`nProceed? This wipes the pedal. [y/N]"
        if ($ans -notmatch '^[yY]') { Warn "aborted by user"; return }
    }

    # 4. Get into the bootloader.
    Step "Entering RP2040 bootloader"
    $rp = Get-DriveByLabel "RPI-RP2"
    if ($rp) {
        Ok "RPI-RP2 already present at $rp"
    } else {
        if (-not $ManualBootloader) {
            $auto = Find-PedalPort
            if ($auto) { Ok "pedal detected on $auto (USB VID match)" }
            try { [void](Enter-Bootloader-ViaRepl) }
            catch { Warn "REPL bootloader entry failed ($($_.Exception.Message)); falling back to manual" }
        }
        Info "waiting for RPI-RP2 (double-tap RESET / hold BOOTSEL if nothing happens)..."
        $rp = Wait-Drive "RPI-RP2" -TimeoutSec 60
        if (-not $rp) { throw "RPI-RP2 bootloader drive never appeared. Re-run with -ManualBootloader and put the pedal in bootloader mode by hand." }
        Ok "RPI-RP2 at $rp"
    }

    # 5. Erase, then flash CircuitPython.
    if ($nukeUf2) {
        Step "Erasing flash (flash_nuke)"
        Send-Uf2 $nukeUf2 $rp
        Info "waiting for the board to re-enter the bootloader..."
        Wait-Drive "RPI-RP2" -TimeoutSec 20 -Gone | Out-Null
        $rp = Wait-Drive "RPI-RP2" -TimeoutSec 60
        if (-not $rp) { throw "RPI-RP2 did not reappear after flash_nuke." }
        Ok "flash erased; RPI-RP2 at $rp"
    }

    Step "Flashing CircuitPython $cpVer"
    Send-Uf2 $cpUf2 $rp
    Info "waiting for CIRCUITPY..."
    Wait-Drive "RPI-RP2" -TimeoutSec 20 -Gone | Out-Null
    $cp = Wait-Drive "CIRCUITPY" -TimeoutSec 60
    if (-not $cp) { throw "CIRCUITPY drive never appeared after flashing CircuitPython." }
    Ok "CircuitPython mounted at $cp"
    Start-Sleep -Seconds 2   # let the FS settle

    # 6. Restore files (robocopy: robust, retries, excludes host junk).
    Step "Restoring backup files to $cp"
    # robocopy /XD matches directory names anywhere in the tree, so plain names suffice.
    $rcArgs = @($work, "$cp\", "/E", "/R:2", "/W:2", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
    foreach ($d in $excludeDirs)  { $rcArgs += @("/XD", $d) }
    foreach ($f in $excludeFiles) { $rcArgs += @("/XF", $f) }
    & robocopy @rcArgs | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE" }
    Ok "files copied (robocopy code $LASTEXITCODE)"

    # 7. Flush so writes commit before we reset.
    $cp = Wait-Drive "CIRCUITPY" -TimeoutSec 5
    if ($cp) {
        try { Write-VolumeCache -DriveLetter $cp.TrimEnd(':') -ErrorAction SilentlyContinue } catch {}
    }

    # 8. Auto-reboot into the restored firmware (no physical power-cycle needed).
    # The freshly-copied code.py triggers a CP auto-reload, so the REPL is busy
    # for a moment; let it settle before the Ctrl-C, and retry a few times.
    Step "Booting stock firmware"
    $bootDone = $false
    Info "letting the restored firmware settle..."
    Start-Sleep -Seconds 4
    for ($try = 1; $try -le 3 -and -not $bootDone; $try++) {
        try { [void](Reset-Pedal-ViaRepl) } catch { Warn "auto-reset failed ($($_.Exception.Message))" }
        # Stock boot.py hides the drive on this hard reset: CIRCUITPY vanishing
        # is the success signal that the stock firmware took over.
        if (Wait-Drive "CIRCUITPY" -TimeoutSec 12 -Gone) {
            Ok "stock firmware booted; CIRCUITPY hidden like a factory unit"
            $bootDone = $true
        } elseif ($try -lt 3) {
            Info "drive still mounted; retrying reset ($try/3)..."
            Start-Sleep -Seconds 2
        }
    }

    Step "Done"
    Ok "Stock CircuitPython $cpVer firmware restored."
    if (-not $bootDone) {
        Warn "Auto-reboot did not take. Power-cycle the pedal (unplug/replug) once to finish."
    }
    exit 0   # don't leak robocopy's non-zero success code as our exit status
}
finally {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $work
}
