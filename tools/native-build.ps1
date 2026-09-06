param(
    [ValidateSet('host', 'rp2040', 'all')][string]$Platform = 'host',
    [string]$SdkPath,
    [switch]$FetchSdk,
    [string]$Distribution = 'Ubuntu',
    [ValidateRange(786432, 16777216)][uint32]$FlashBytes = 8388608
)
$ErrorActionPreference = 'Stop'
if ($FlashBytes % 4096 -ne 0) { throw 'FlashBytes must be sector aligned (4096 bytes)' }
$repoRoot = Split-Path -Parent $PSScriptRoot
$linuxRepo = & wsl.exe -d $Distribution -- wslpath -a -u $repoRoot
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve repository path in WSL' }
$arguments = @('-d', $Distribution, '--cd', $linuxRepo, '--', 'env')
$arguments += "BOSUN_FLASH_BYTES=$FlashBytes"
if ($SdkPath) {
    $resolvedSdk = (Resolve-Path -LiteralPath $SdkPath).Path
    $linuxSdk = & wsl.exe -d $Distribution -- wslpath -a -u $resolvedSdk
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve SDK path in WSL' }
    $arguments += "PICO_SDK_PATH=$linuxSdk"
}
$arguments += @('bash', './tools/native-build.sh', $Platform)
if ($FetchSdk) { $arguments += '--fetch-sdk' }
& wsl.exe @arguments
exit $LASTEXITCODE
