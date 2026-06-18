#Requires -Version 5.1
param()

$ErrorActionPreference = 'Stop'

$installerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$packageRoot = Split-Path -Parent $installerDir
Set-Location -Path $packageRoot

function Normalize-Arch([string]$Value) {
    switch ($Value.ToLowerInvariant()) {
        'x86_64'  { return 'amd64' }
        'amd64'   { return 'amd64' }
        'arm64'   { return 'arm64' }
        'aarch64' { return 'arm64' }
        default { throw "Unsupported CPU architecture: $Value" }
    }
}

function Read-PayloadMetadata([string]$Path) {
    $metadata = @{}
    if (-not (Test-Path $Path)) { return $metadata }
    foreach ($line in [System.IO.File]::ReadLines((Get-Item $Path).FullName)) {
        if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) {
            continue
        }
        $idx = $line.IndexOf('=')
        $metadata[$line.Substring(0, $idx)] = $line.Substring($idx + 1)
    }
    return $metadata
}

$arch = if ($env:BDAG_INSTALL_ARCH) { $env:BDAG_INSTALL_ARCH } else {
    switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()) {
        'X64'   { 'amd64' }
        'Arm64' { 'arm64' }
        default { throw "Unsupported CPU architecture: $([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture)" }
    }
}
$arch = Normalize-Arch $arch
$payloadMetadata = Read-PayloadMetadata (Join-Path $packageRoot 'release-payload.env')
$payloadArch = $payloadMetadata['BDAG_RELEASE_PAYLOAD_ARCH']
if (-not $payloadArch) {
    switch ($payloadMetadata['BDAG_RELEASE_PAYLOAD_TARGET']) {
        'linux-amd64' { $payloadArch = 'amd64' }
        'linux-arm64' { $payloadArch = 'arm64' }
    }
}
if (-not $payloadArch) {
    $payloadArch = $arch
}
$payloadArch = Normalize-Arch $payloadArch
$dockerPlatform = "linux/$payloadArch"
if ($payloadMetadata['DOCKER_PLATFORM'] -and $payloadMetadata['DOCKER_PLATFORM'] -ne $dockerPlatform) {
    throw "release-payload.env has inconsistent DOCKER_PLATFORM=$($payloadMetadata['DOCKER_PLATFORM']); expected $dockerPlatform."
}
$installMode = $env:BDAG_INSTALL_MODE
$deployKind = $env:BDAG_DEPLOY_KIND
$chainMode = $env:BDAG_CHAIN_MODE
$nodeOnlyInstall = $false
$nodeArchival = '0'
$snapshotBaseUrl = if ($env:BDAG_SNAPSHOT_BASE_URL) { $env:BDAG_SNAPSHOT_BASE_URL } else { 'https://bdagstack.bdagdev.xyz' }
$snapshotUrl = $env:BDAG_SNAPSHOT_URL
$snapshotMinBytes = if ($env:BDAG_SNAPSHOT_MIN_BYTES) { [int64]$env:BDAG_SNAPSHOT_MIN_BYTES } else { [int64]1048576 }
$requireSnapshot = $env:BDAG_REQUIRE_SNAPSHOT -eq '1'
$requestedSnapshotDownloader = if ($env:BDAG_SNAPSHOT_DOWNLOADER) { $env:BDAG_SNAPSHOT_DOWNLOADER.ToLowerInvariant() } else { 'auto' }
$aria2Connections = if ($env:BDAG_ARIA2_CONNECTIONS) { [int]$env:BDAG_ARIA2_CONNECTIONS } else { 8 }
$installAria2 = $env:BDAG_INSTALL_ARIA2 -ne '0'
$installMinFreeBytes = if ($env:BDAG_INSTALL_MIN_FREE_BYTES) { [int64]$env:BDAG_INSTALL_MIN_FREE_BYTES } else { [int64]10737418240 }
$installCheckPorts = if ($env:BDAG_INSTALL_CHECK_PORTS) { $env:BDAG_INSTALL_CHECK_PORTS -split '[, ]+' } else { @('3334', '8080', '9280', '18545', '18546', '38131') }
$strictPreflight = $env:BDAG_INSTALL_STRICT_PREFLIGHT -eq '1'
$strictPorts = $env:BDAG_INSTALL_STRICT_PORTS -eq '1'
$cleanOrphanContainers = $env:BDAG_CLEAN_ORPHAN_CONTAINERS -eq '1'

Write-Host "=== BlockDAG Pool Stack Installer (windows/$arch) ===" -ForegroundColor Cyan
Write-Host ""

if ($payloadMetadata['BDAG_RELEASE_PAYLOAD_TARGET']) {
    Write-Host "Runtime payload: $($payloadMetadata['BDAG_RELEASE_PAYLOAD_TARGET']) ($dockerPlatform)"
    Write-Host ""
}

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required. $Hint"
    }
}

function Warn-OrFailPreflight([string]$Message) {
    if ($strictPreflight) {
        throw $Message
    }
    Write-Host "Warning: $Message" -ForegroundColor Yellow
}

function Test-PortListening([string]$Port) {
    try {
        return [bool](Get-NetTCPConnection -LocalPort ([int]$Port) -State Listen -ErrorAction Stop | Select-Object -First 1)
    } catch {
        return $false
    }
}

function Invoke-ReleasePreflight {
    Write-Host "=== Release preflight ===" -ForegroundColor Cyan

    if ($arch -notin @('amd64', 'arm64')) {
        Warn-OrFailPreflight "unsupported CPU architecture '$arch'."
    }

    $drive = Get-PSDrive -Name (Get-Location).Drive.Name
    if ($drive.Free -lt $installMinFreeBytes) {
        Warn-OrFailPreflight "free disk $($drive.Free) bytes is below BDAG_INSTALL_MIN_FREE_BYTES=$installMinFreeBytes."
    }

    $busyPorts = @()
    foreach ($port in $installCheckPorts) {
        if ($port -and (Test-PortListening $port)) {
            $busyPorts += $port
        }
    }
    if ($busyPorts.Count -gt 0) {
        if ($strictPorts) {
            throw "host ports already listening: $($busyPorts -join ', ')"
        }
        Write-Host "Warning: host ports already listening: $($busyPorts -join ', '). Existing stack services may be using them." -ForegroundColor Yellow
    }

    $timeService = Get-Service W32Time -ErrorAction SilentlyContinue
    if (-not $timeService -or $timeService.Status -ne 'Running') {
        Warn-OrFailPreflight "Windows Time service is not running."
    }

    if (Get-Command jq -ErrorAction SilentlyContinue) {
        Write-Host "jq found; release scripts do not require it for installer JSON parsing."
    } else {
        Write-Host "jq not found; continuing because installer parsing avoids a jq dependency."
    }

    try {
        Invoke-WebRequest -Uri $snapshotUrl -Method Head -UseBasicParsing -TimeoutSec 10 | Out-Null
    } catch {
        Warn-OrFailPreflight "could not reach snapshot seed URL $snapshotUrl; the installer will fall back to genesis/P2P sync."
    }
    Write-Host ""
}

function Set-EnvValue([string]$Path, [string]$Key, [string]$Value) {
    $text = [System.IO.File]::ReadAllText((Get-Item $Path).FullName)
    $escaped = [regex]::Escape($Key)
    $line = "$Key=$Value"
    if ($text -match "(?m)^$escaped=") {
        $text = [regex]::Replace($text, "(?m)^$escaped=.*", { param($match) $line })
    } else {
        $text = $text.TrimEnd() + "`n$line`n"
    }
    $text = $text -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText((Join-Path (Get-Location) $Path), $text, [System.Text.Encoding]::UTF8)
}

function Get-EnvFileValue([string]$Path, [string]$Key) {
    if (-not (Test-Path $Path)) { return '' }
    $escaped = [regex]::Escape($Key)
    $line = Get-Content $Path | Where-Object { $_ -match "^$escaped=" } | Select-Object -Last 1
    if (-not $line) { return '' }
    $value = $line.Substring($Key.Length + 1).Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
}

function Resolve-PackagePath([string]$Value) {
    if (-not $Value) { $Value = './data/node' }
    if ([System.IO.Path]::IsPathRooted($Value)) { return $Value }
    $clean = $Value -replace '^[.][\\/]', ''
    return (Join-Path (Get-Location).Path $clean)
}

function Test-ChainMarkers([string]$NetworkDir) {
    return (
        (Test-Path (Join-Path $NetworkDir 'BdagChain')) -or
        (Test-Path (Join-Path $NetworkDir 'bdageth\chaindata')) -or
        (Test-Path (Join-Path $NetworkDir 'chaindata'))
    )
}

function Stage-SnapshotForNodeDatadir {
    if ($snapshotPath -ne './latest.bdsnap' -or -not (Test-Path 'latest.bdsnap')) { return }

    $nodeDir = Resolve-PackagePath (Get-EnvFileValue '.env' 'BDAG_NODE_DATA_DIR')
    $networkDir = Join-Path $nodeDir 'mainnet'
    $target = Join-Path $networkDir 'snapshot.bdsnap'

    if (Test-ChainMarkers $networkDir) {
        Write-Host "Existing chain markers found in $networkDir; preserving node data and skipping snapshot staging."
        return
    }
    if ((Test-Path $target) -and $env:BDAG_REPLACE_STAGED_SNAPSHOT -ne '1') {
        Write-Host "Existing staged node snapshot found: $target"
        return
    }

    New-Item -ItemType Directory -Force -Path $networkDir | Out-Null
    Remove-Item -Path $target -ErrorAction SilentlyContinue
    try {
        New-Item -ItemType HardLink -Path $target -Target (Get-Item 'latest.bdsnap').FullName | Out-Null
        Write-Host "Staged snapshot for node datadir using hard link: $target"
    } catch {
        Copy-Item -Path 'latest.bdsnap' -Destination $target -Force
        Write-Host "Staged snapshot for node datadir: $target"
    }
}

if ($env:BDAG_INSTALL_TEST_WRITE_ENV_ONLY -eq '1') {
    Copy-Item .env.example .env -Force
    Set-EnvValue .env DOCKER_PLATFORM $dockerPlatform
    exit 0
}

function Test-ValidSnapshot([string]$Path) {
    if (-not (Test-Path $Path)) { return $false }
    return ((Get-Item $Path).Length -ge $snapshotMinBytes)
}

function New-PostgresPassword {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes)
}

function Install-Aria2IfPossible {
    if (Get-Command aria2c -ErrorAction SilentlyContinue) { return $true }
    if (-not $installAria2) { return $false }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }

    Write-Host "aria2c is missing. Installing aria2 with winget..." -ForegroundColor Yellow
    & winget install --id aria2.aria2 -e --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Warning: winget failed to install aria2; falling back to the next downloader." -ForegroundColor Yellow
        return $false
    }

    return [bool](Get-Command aria2c -ErrorAction SilentlyContinue)
}

function Resolve-SnapshotDownloader {
    switch ($requestedSnapshotDownloader) {
        'auto' {
            if (Install-Aria2IfPossible) { return 'aria2c' }
            if (Get-Command Start-BitsTransfer -ErrorAction SilentlyContinue) { return 'bits' }
            return 'powershell'
        }
        'aria2c' {
            if (-not (Install-Aria2IfPossible)) {
                throw "aria2c was requested but was not found. Install it with 'winget install aria2.aria2', or set BDAG_SNAPSHOT_DOWNLOADER=powershell."
            }
            return 'aria2c'
        }
        'bits' {
            if (-not (Get-Command Start-BitsTransfer -ErrorAction SilentlyContinue)) {
                throw "BITS was requested but Start-BitsTransfer is not available. Set BDAG_SNAPSHOT_DOWNLOADER=powershell."
            }
            return 'bits'
        }
        'powershell' { return 'powershell' }
        default { throw "Unsupported BDAG_SNAPSHOT_DOWNLOADER '$requestedSnapshotDownloader'. Use auto, aria2c, bits, or powershell." }
    }
}

function Download-Snapshot {
    $tmp = 'latest.bdsnap.part'
    $destination = Join-Path (Get-Location).Path $tmp
    $downloader = Resolve-SnapshotDownloader
    if ($downloader -ne 'aria2c') {
        Remove-Item -Path $tmp -ErrorAction SilentlyContinue
    }

    Write-Host "No local snapshot found. Downloading latest.bdsnap from $snapshotUrl." -ForegroundColor Yellow
    Write-Host "Using $downloader for snapshot download."
    try {
        if ($downloader -eq 'aria2c') {
            $ariaArgs = @(
                '--allow-overwrite=true',
                '--auto-file-renaming=false',
                '--continue=true',
                '--connect-timeout=20',
                '--dir=.',
                '--file-allocation=none',
                "--max-connection-per-server=$aria2Connections",
                '--max-tries=3',
                '--min-split-size=64M',
                "--out=$tmp",
                '--retry-wait=2',
                "--split=$aria2Connections",
                '--timeout=60',
                $snapshotUrl
            )
            & aria2c @ariaArgs
            if ($LASTEXITCODE -ne 0) { throw "aria2c exited with code $LASTEXITCODE" }
        } elseif ($downloader -eq 'bits') {
            Start-BitsTransfer -Source $snapshotUrl -Destination $destination -TransferType Download -ErrorAction Stop
        } else {
            $ProgressPreference = 'Continue'
            Invoke-WebRequest -Uri $snapshotUrl -OutFile $tmp -UseBasicParsing
        }

        if (Test-ValidSnapshot $tmp) {
            Move-Item -Path $tmp -Destination 'latest.bdsnap' -Force
            Write-Host "Snapshot downloaded ($((Get-Item latest.bdsnap).Length) bytes)."
            return $true
        }

        Write-Host "Warning: downloaded snapshot is too small to be valid ($((Get-Item $tmp).Length) bytes)." -ForegroundColor Yellow
    } catch {
        Write-Host "Warning: snapshot download failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }

    if ($downloader -ne 'aria2c') {
        Remove-Item -Path $tmp -ErrorAction SilentlyContinue
    }
    return $false
}

function Continue-WithoutSnapshotOrExit {
    if ($requireSnapshot) {
        throw "Snapshot download/import is required (BDAG_REQUIRE_SNAPSHOT=1), but no valid snapshot is available."
    }

    Write-Host "No snapshot available; continuing with genesis/P2P sync."
}

function Get-ComposeProjectName {
    $json = & docker compose config --format json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) { return $null }
    try {
        return (($json -join "`n") | ConvertFrom-Json).name
    } catch {
        return $null
    }
}

function Plan-OrphanContainerCleanup {
    $project = Get-ComposeProjectName
    if (-not $project) { return }

    $containers = & docker ps -a --filter "label=com.docker.compose.project=$project" --format "{{.Names}}`t{{.Status}}" 2>$null
    if (-not $containers) { return }

    Write-Host ""
    Write-Host "Compose project '$project' has existing containers:" -ForegroundColor Yellow
    $containers | ForEach-Object { Write-Host "  $_" }
    if ($cleanOrphanContainers) {
        Write-Host "BDAG_CLEAN_ORPHAN_CONTAINERS=1; running docker compose down --remove-orphans before start."
        & docker compose down --remove-orphans
    } else {
        Write-Host "Dry-run cleanup only. Set BDAG_CLEAN_ORPHAN_CONTAINERS=1 to remove old/orphan compose containers during install." -ForegroundColor Yellow
    }
}

function Clean-BuildContextMetadata {
    Get-ChildItem -Force -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like '._*' -or $_.Name -eq '.DS_Store' -or $_.Name -eq 'Thumbs.db' -or $_.Name -eq 'desktop.ini' } |
        Remove-Item -Force -ErrorAction SilentlyContinue

    Get-ChildItem -Force -Recurse -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq '__MACOSX' -or $_.Name -eq '$RECYCLE.BIN' -or $_.Name -eq 'System Volume Information' } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

function Ensure-DockerignorePattern([string]$Pattern) {
    if (-not (Test-Path .dockerignore)) {
        New-Item -ItemType File -Path .dockerignore | Out-Null
    }

    $lines = Get-Content .dockerignore -ErrorAction SilentlyContinue
    if ($lines -notcontains $Pattern) {
        Add-Content -Path .dockerignore -Value $Pattern
    }
}

function Ensure-DockerignoreExcludesSnapshots {
    # Snapshots are mounted at runtime; sending them to Docker build context can
    # exhaust Docker Desktop's Linux VM disk and fail with input/output errors.
    Ensure-DockerignorePattern '*.bdsnap'
    Ensure-DockerignorePattern 'latest.bdsnap.part'
    Ensure-DockerignorePattern 'latest.bdsnap.part.*'
    Ensure-DockerignorePattern '*.aria2'
}

Require-Command docker "Install Docker Desktop, then re-run this installer."
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose v2 is required. Install/update Docker Desktop."
}

if (-not (Test-Path .env.example) -or -not (Test-Path node.conf.example) -or -not (Test-Path docker-compose.yml)) {
    throw "Run this installer from the extracted pool-stack-docker release folder."
}

function Convert-DeployKind([string]$Value) {
    switch ($Value) {
        { $_ -in @('1', 'pool', 'pool-stack') } { return 'pool' }
        { $_ -in @('2', 'node', 'standalone', 'standalone-node') } { return 'node' }
        default { return $null }
    }
}

function Convert-ChainMode([string]$Value) {
    switch ($Value) {
        { $_ -in @('1', 'non-archive', 'nonarchive', 'pruned') } { return 'non-archive' }
        { $_ -in @('2', 'archive', 'full') } { return 'archive' }
        default { return $null }
    }
}

# Legacy combined override pre-seeds both dimensions; explicit
# BDAG_DEPLOY_KIND/BDAG_CHAIN_MODE take precedence.
if ($installMode) {
    switch ($installMode) {
        { $_ -in @('pool', 'pool-stack') } {
            if (-not $deployKind) { $deployKind = 'pool' }
        }
        'archive-node' {
            if (-not $deployKind) { $deployKind = 'node' }
            if (-not $chainMode) { $chainMode = 'archive' }
        }
        { $_ -in @('node', 'non-archive-node') } {
            if (-not $deployKind) { $deployKind = 'node' }
            if (-not $chainMode) { $chainMode = 'non-archive' }
        }
        default { throw "Invalid BDAG_INSTALL_MODE '$installMode'. Use pool, archive-node, or node." }
    }
}

# Step 1/2 - deployment.
if ($deployKind) {
    $deployKind = Convert-DeployKind $deployKind
    if (-not $deployKind) { throw "Invalid deployment '$($env:BDAG_DEPLOY_KIND)'. Use pool or node." }
    Write-Host "Deployment: $deployKind (preselected)"
} else {
    Write-Host "Step 1/2 - Select what to install:"
    Write-Host "  1) Mining pool stack with dashboard (default)"
    Write-Host "  2) Standalone node only"
    while (-not $deployKind) {
        $choice = Read-Host "Choice [1]"
        if (-not $choice) { $choice = '1' }
        $deployKind = Convert-DeployKind $choice
        if (-not $deployKind) { Write-Host "Please enter 1 or 2." -ForegroundColor Yellow }
    }
    Write-Host ""
}

# Step 2/2 - chain data type.
if ($chainMode) {
    $chainMode = Convert-ChainMode $chainMode
    if (-not $chainMode) { throw "Invalid chain mode '$($env:BDAG_CHAIN_MODE)'. Use archive or non-archive." }
    Write-Host "Chain data: $chainMode (preselected)"
    Write-Host ""
} else {
    Write-Host "Step 2/2 - Select chain data type:"
    Write-Host "  1) Non-archive (pruned chain data, default)"
    Write-Host "  2) Archive (keeps full block history, no pruning)"
    while (-not $chainMode) {
        $choice = Read-Host "Choice [1]"
        if (-not $choice) { $choice = '1' }
        $chainMode = Convert-ChainMode $choice
        if (-not $chainMode) { Write-Host "Please enter 1 or 2." -ForegroundColor Yellow }
    }
    Write-Host ""
}

$nodeOnlyInstall = $deployKind -eq 'node'
if ($chainMode -eq 'archive') { $nodeArchival = '1' }
# Snapshot host convention: latest.bdsnap is the non-archive (pruned) snapshot,
# latest-archive.bdsnap is the archive (full history) snapshot.
if (-not $snapshotUrl) {
    $snapshotFile = if ($chainMode -eq 'archive') { 'latest-archive.bdsnap' } else { 'latest.bdsnap' }
    $snapshotUrl = "$($snapshotBaseUrl.TrimEnd('/'))/$snapshotFile"
}
Write-Host "Snapshot source: $snapshotUrl"
Write-Host ""

Invoke-ReleasePreflight

$snapshotPath = 'docker/no-snapshot.marker'
if (Test-ValidSnapshot latest.bdsnap) {
    Write-Host "Found snapshot: latest.bdsnap ($((Get-Item latest.bdsnap).Length) bytes)"
    $snapshotHostPath = './latest.bdsnap'
    $snapshotImportEnabled = '1'
} else {
    if (Test-Path latest.bdsnap) {
        Write-Host "Ignoring invalid snapshot file: latest.bdsnap ($((Get-Item latest.bdsnap).Length) bytes)" -ForegroundColor Yellow
        Remove-Item -Path 'latest.bdsnap' -ErrorAction SilentlyContinue
    }

    $snap = Get-ChildItem -File -Filter '*.bdsnap' | Select-Object -First 1
    if ($snap) {
        if (Test-ValidSnapshot $snap.FullName) {
            Write-Host "Found snapshot: $($snap.Name) ($($snap.Length) bytes)"
            Move-Item -Path $snap.FullName -Destination (Join-Path (Get-Location) 'latest.bdsnap') -Force
            $snapshotHostPath = './latest.bdsnap'
            $snapshotImportEnabled = '1'
        } else {
            Write-Host "Ignoring invalid snapshot file: $($snap.Name) ($($snap.Length) bytes)" -ForegroundColor Yellow
            Remove-Item -Path $snap.FullName -ErrorAction SilentlyContinue
        }
    }

    if ($snapshotHostPath -ne './latest.bdsnap') {
        if (Download-Snapshot) {
            $snapshotHostPath = './latest.bdsnap'
            $snapshotImportEnabled = '1'
        } else {
            Remove-Item -Path 'latest.bdsnap' -ErrorAction SilentlyContinue
            Continue-WithoutSnapshotOrExit
        }
    }
}

if ($snapshotHostPath -ne './latest.bdsnap' -and $requireSnapshot) {
    throw "Snapshot download/import is required (BDAG_REQUIRE_SNAPSHOT=1), but no valid snapshot is available."
}

Write-Host ""
Write-Host "=== Configuration ===" -ForegroundColor Cyan
Write-Host ""

function Read-PlainPassword([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Read-WithDefault([string]$Prompt, [string]$DefaultValue) {
    $value = Read-Host "$Prompt [$DefaultValue]"
    if ($value) { return $value }
    return $DefaultValue
}

function Get-DefaultCidr([string]$IpAddress) {
    if ($IpAddress -match '^([0-9]+)\.([0-9]+)\.([0-9]+)\.[0-9]+$') {
        return "$($Matches[1]).$($Matches[2]).$($Matches[3]).0/24"
    }
    return '192.168.1.0/24'
}

function Test-DefaultDockerBridgeAddress([string]$Value) {
    return $Value -match '^172\.(1[6-9]|2[0-9]|3[0-1])\.'
}

function Assert-PoolLanConfig {
    $poolHost = Get-EnvFileValue '.env' 'BDAG_POOL_HOST'
    $poolUrl = Get-EnvFileValue '.env' 'BDAG_POOL_URL'
    $scanTarget = Get-EnvFileValue '.env' 'BDAG_MINER_SCAN_TARGET'
    $asicCidrs = Get-EnvFileValue '.env' 'BDAG_ASIC_LAN_CIDRS'
    $allowBridge = Get-EnvFileValue '.env' 'BDAG_ALLOW_DOCKER_BRIDGE_ASIC_IPS'
    $poolUrlHost = ($poolUrl -replace '^[^:]+://', '') -replace ':.*$', ''
    if (-not $poolHost -or -not $poolUrl -or -not $scanTarget -or -not $asicCidrs) {
        throw "Pool LAN configuration is incomplete. Set BDAG_POOL_HOST, BDAG_POOL_URL, BDAG_MINER_SCAN_TARGET, and BDAG_ASIC_LAN_CIDRS."
    }
    if ($allowBridge -notin @('1', 'true', 'True')) {
        if ((Test-DefaultDockerBridgeAddress $poolHost) -or (Test-DefaultDockerBridgeAddress $poolUrlHost)) {
            throw "Refusing Docker bridge pool endpoint '$poolUrl'. Use the host-facing ASIC LAN IP, not a 172.16.0.0/12 container address."
        }
        if ($scanTarget -match '(^|[, ])172\.(1[6-9]|2[0-9]|3[0-1])\.' -or $asicCidrs -match '(^|[, ])172\.(1[6-9]|2[0-9]|3[0-1])\.') {
            throw "Refusing Docker bridge ASIC scan scope '$asicCidrs'. Set BDAG_ASIC_LAN_CIDRS to the physical ASIC LAN."
        }
    }
}

if ($env:POSTGRES_PASSWORD) {
    $pgPassword = $env:POSTGRES_PASSWORD
    Write-Host "Using POSTGRES_PASSWORD from environment."
} else {
    $pgPassword = New-PostgresPassword
    Write-Host "Generated Postgres password."
}

Copy-Item .env.example .env -Force
Set-EnvValue .env POSTGRES_PASSWORD $pgPassword
Set-EnvValue .env DOCKER_PLATFORM $dockerPlatform
Set-EnvValue .env SNAPSHOT_PATH $snapshotPath
Set-EnvValue .env BDAG_SNAPSHOT_URL $snapshotUrl
Set-EnvValue .env BDAG_NODE_ARCHIVAL $nodeArchival

$miningAddr = ''
if ($nodeOnlyInstall) {
    Write-Host "Node-only install: skipping pool, dashboard, and ASIC configuration."
} else {
    $miningAddr = Read-Host "Mining/earnings wallet address (0x...)"
    $poolPrivateKey = Read-PlainPassword "Pool operator private key (optional, hidden; press Enter to skip)"

    $poolLanIpDefault = if ($env:BDAG_POOL_HOST) { $env:BDAG_POOL_HOST } else { '192.168.1.10' }
    $poolLanIp = Read-WithDefault "Pool LAN IP miners should connect to" $poolLanIpDefault
    $minerScanTargetDefault = if ($env:BDAG_MINER_SCAN_TARGET) { $env:BDAG_MINER_SCAN_TARGET } elseif ($env:BDAG_ASIC_LAN_CIDRS) { $env:BDAG_ASIC_LAN_CIDRS } else { Get-DefaultCidr $poolLanIp }
    $minerScanTarget = Read-WithDefault "LAN scan range for ASIC discovery" $minerScanTargetDefault
    Set-EnvValue .env MINING_POOL_ADDRESS $miningAddr
    Set-EnvValue .env BDAG_POOL_HOST $poolLanIp
    Set-EnvValue .env BDAG_POOL_URL "stratum+tcp://${poolLanIp}:3334"
    Set-EnvValue .env BDAG_MINER_SCAN_TARGET $minerScanTarget
    Set-EnvValue .env BDAG_ASIC_LAN_CIDRS $minerScanTarget
    Assert-PoolLanConfig
    if ($poolPrivateKey) {
        Set-EnvValue .env POOL_PRIVATE_KEY $poolPrivateKey
    }
}

Copy-Item node.conf.example node.conf -Force
$nodeText = [System.IO.File]::ReadAllText((Get-Item node.conf).FullName)
if (-not $nodeOnlyInstall) {
    if ($nodeText -match '(?m)^miningaddr=') {
        $nodeText = [regex]::Replace($nodeText, '(?m)^miningaddr=.*', "miningaddr=$miningAddr")
    } else {
        $nodeText = $nodeText.TrimEnd() + "`nminingaddr=$miningAddr`n"
    }
}

Write-Host ""
Write-Host "Detecting external IP address..."
try {
    $externalIp = (Invoke-WebRequest -Uri 'https://api.ipify.org' -UseBasicParsing -TimeoutSec 5).Content.Trim()
} catch {
    try {
        $externalIp = (Invoke-WebRequest -Uri 'https://ifconfig.me' -UseBasicParsing -TimeoutSec 5).Content.Trim()
    } catch {
        $externalIp = ''
    }
}

if ($externalIp) {
    Write-Host "  Detected: $externalIp"
    if ($nodeText -match '(?m)^# externalip=') {
        $nodeText = [regex]::Replace($nodeText, '(?m)^# externalip=.*', "externalip=$externalIp")
    } elseif ($nodeText -match '(?m)^externalip=') {
        $nodeText = [regex]::Replace($nodeText, '(?m)^externalip=.*', "externalip=$externalIp")
    } else {
        $nodeText = $nodeText.TrimEnd() + "`nexternalip=$externalIp`n"
    }
} else {
    Write-Host "  Warning: could not detect external IP. Node will operate outbound-only." -ForegroundColor Yellow
}

$nodeText = $nodeText -replace "`r`n", "`n"
[System.IO.File]::WriteAllText((Join-Path (Get-Location) 'node.conf'), $nodeText, [System.Text.Encoding]::UTF8)

if (-not $nodeOnlyInstall) {
    New-Item -ItemType Directory -Force -Path 'collector\logs' | Out-Null
}
Clean-BuildContextMetadata
Stage-SnapshotForNodeDatadir
Plan-OrphanContainerCleanup
$env:DOCKER_DEFAULT_PLATFORM = $dockerPlatform

Write-Host ""
Write-Host "=== Building Docker images ($dockerPlatform) ===" -ForegroundColor Cyan
if ($nodeOnlyInstall) {
    & docker compose build node
} else {
    & docker compose build
}
if ($LASTEXITCODE -ne 0) { throw "docker compose build failed." }

Write-Host ""
if ($nodeOnlyInstall) {
    Write-Host "=== Starting node ===" -ForegroundColor Cyan
    & docker compose up -d --no-build --pull never node
} else {
    Write-Host "=== Starting services ===" -ForegroundColor Cyan
    & docker compose up -d --no-build --pull never
}
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed." }

Write-Host ""
Write-Host "=================================================" -ForegroundColor Green
if ($nodeOnlyInstall) {
    $nodeKind = if ($nodeArchival -eq '1') { 'archive' } else { 'non-archive' }
    Write-Host "  BlockDAG $nodeKind node is running." -ForegroundColor Green
    Write-Host "=================================================" -ForegroundColor Green
    Write-Host "  P2P:        port 8150"
    Write-Host "  Chain RPC:  http://localhost:38131"
    Write-Host "  EVM RPC:    http://localhost:18545"
    Write-Host ""
    Write-Host "  View logs:  docker compose logs -f node"
} else {
    Write-Host "  BlockDAG Pool Stack is running." -ForegroundColor Green
    Write-Host "=================================================" -ForegroundColor Green
    Write-Host "  Dashboard:  http://localhost:8080"
    Write-Host "  Collector:  http://localhost:9280"
    Write-Host "  Stratum:    stratum+tcp://localhost:3334"
    Write-Host "  EVM RPC:    http://localhost:18545"
    Write-Host ""
    Write-Host "  View logs:  docker compose logs -f"
}
Write-Host "  Stop:       docker compose down"
Write-Host "=================================================" -ForegroundColor Green

Start-Process powershell -WorkingDirectory $packageRoot
