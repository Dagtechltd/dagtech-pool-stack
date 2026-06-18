#Requires -Version 5.1
param()

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$platform = [System.Environment]::OSVersion.Platform.ToString()
$isWindows = $platform -like 'Win*'

if (-not $isWindows) {
    $bash = Get-Command bash -ErrorAction SilentlyContinue
    if (-not $bash) {
        throw "This platform needs bash to run install.sh."
    }
    & $bash.Source (Join-Path $scriptDir 'install.sh')
    exit $LASTEXITCODE
}

switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()) {
    'X64'   { $env:BDAG_INSTALL_ARCH = 'amd64' }
    'Arm64' { $env:BDAG_INSTALL_ARCH = 'arm64' }
    default { throw "Unsupported CPU architecture: $([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture)" }
}

$env:BDAG_INSTALL_OS = 'windows'
& (Join-Path $scriptDir 'installers\install-windows.ps1')
exit $LASTEXITCODE
