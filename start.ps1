[CmdletBinding()]
param(
    [string]$EnvFile = "deploy/.env",
    [string]$Workspace = "",
    [switch]$NoChat,
    [switch]$NoBuild
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not installed. Install and start Docker Desktop first."
}
docker info --format '{{.ServerVersion}}' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker is not running or cannot be accessed." }
docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker Compose v2.20+ is required." }

if (-not [IO.Path]::IsPathRooted($EnvFile)) { $EnvFile = Join-Path $projectRoot $EnvFile }
$EnvFile = [IO.Path]::GetFullPath($EnvFile)
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    if ($EnvFile -eq [IO.Path]::GetFullPath((Join-Path $projectRoot "deploy/.env"))) {
        Copy-Item -LiteralPath (Join-Path $projectRoot "deploy/.env.example") -Destination $EnvFile
        Write-Host "Created $EnvFile. Set your API key and workspace, then run this script again."
        exit 1
    }
    throw "Configuration file does not exist: $EnvFile"
}

$oldEnvFile = $env:LOOPSMITH_ENV_FILE
$oldWorkspace = $env:LOOPSMITH_WORKSPACE
try {
    $env:LOOPSMITH_ENV_FILE = $EnvFile.Replace('\', '/')
    if ($Workspace) {
        if (-not [IO.Path]::IsPathRooted($Workspace)) { $Workspace = Join-Path $projectRoot $Workspace }
        $Workspace = (Resolve-Path -LiteralPath $Workspace).Path
        if (-not (Test-Path -LiteralPath $Workspace -PathType Container)) { throw "Workspace must be a directory." }
        $env:LOOPSMITH_WORKSPACE = $Workspace.Replace('\', '/')
    }
    $composeArgs = @("compose", "--project-directory", $projectRoot, "--env-file", $EnvFile, "-f", (Join-Path $projectRoot "compose.yaml"))
    & docker @composeArgs config --quiet
    if ($LASTEXITCODE -ne 0) { throw "Invalid deployment configuration." }
    $buildOption = if ($NoBuild) { "--no-build" } else { "--build" }
    & docker @composeArgs up --detach $buildOption --wait --wait-timeout 120 agent
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Startup failed. Inspect Core logs with: docker compose --env-file `"$EnvFile`" logs --tail 60 agent"
        throw "Core did not become healthy. Check credentials, paths, proxy and file permissions."
    }
    Write-Host "Core is ready. Session data is stored in the agent-data volume."
    if (-not $NoChat) {
        & docker @composeArgs exec agent loopsmith-tui
        if ($LASTEXITCODE -ne 0) { throw "Chat exited with an error. The Core container is still running." }
    }
} finally {
    $env:LOOPSMITH_ENV_FILE = $oldEnvFile
    $env:LOOPSMITH_WORKSPACE = $oldWorkspace
}
