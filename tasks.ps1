#Requires -Version 5.1
<#
.SYNOPSIS
    Task runner for wascat-backend on Windows.

.DESCRIPTION
    Wraps the handful of commands used daily. The main reason it exists rather
    than a README full of copy-paste is `up`: the Docker daemon is frequently
    not running on a developer laptop, and the error Compose gives for that
    ("failed to connect to the docker API at npipe:...") reads like a broken
    install rather than "start Docker Desktop". This starts it and waits.

.EXAMPLE
    .\tasks.ps1 up
    .\tasks.ps1 migrate
    .\tasks.ps1 seed
    .\tasks.ps1 dev
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'down', 'reset', 'migrate', 'revision', 'seed', 'backfill',
        'dev', 'test', 'check', 'fmt', 'doctor', 'logs', 'psql', 'help')]
    [string]$Task = 'help',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# Native executables (docker, uv) write progress and warnings to stderr.
# Under Windows PowerShell 5.1 with ErrorActionPreference='Stop', each stderr
# line becomes a terminating NativeCommandError, so `docker compose pull`
# aborts the script even when it succeeds. Success is decided by exit code
# instead; see Invoke-Checked.
$ErrorActionPreference = 'Continue'
$PSNativeCommandUseErrorActionPreference = $false
Set-Location $PSScriptRoot

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "    $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "    $message" -ForegroundColor Yellow }

function Test-DockerRunning {
    try {
        docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Start-DockerDesktop {
    if (Test-DockerRunning) { return }

    Write-Warn 'Docker daemon is not responding; starting Docker Desktop.'
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Docker\Docker\Docker Desktop.exe')
    ) | Where-Object { $_ -and (Test-Path $_) }

    if (-not $candidates) {
        throw 'Docker Desktop was not found. Install it, or start the daemon yourself, then re-run.'
    }
    Start-Process -FilePath $candidates[0] | Out-Null

    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        if (Test-DockerRunning) {
            Write-Ok 'Docker daemon is up.'
            return
        }
    }
    throw 'Docker Desktop did not become ready within 180s. Start it manually and re-run.'
}

function Invoke-Checked($label, [scriptblock]$block) {
    $global:LASTEXITCODE = 0
    & $block
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "$label failed with exit code $LASTEXITCODE" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

switch ($Task) {
    'up' {
        Start-DockerDesktop
        Write-Step 'Starting Postgres and MinIO'
        Invoke-Checked 'docker compose up' { docker compose up -d postgres minio createbuckets }
        Write-Ok 'Postgres  localhost:5432   (wascat/wascat)'
        Write-Ok 'MinIO S3  localhost:9000   console localhost:9001 (wascat/wascat-dev-secret)'
    }
    'down' {
        Write-Step 'Stopping containers (volumes preserved)'
        docker compose down
    }
    'reset' {
        Write-Warn 'This destroys the local database AND every uploaded object.'
        $answer = Read-Host 'Type "reset" to confirm'
        if ($answer -ne 'reset') { Write-Host 'Cancelled.'; break }
        docker compose down -v
        Write-Ok 'Volumes removed. Run "up", then "migrate", then "seed".'
    }
    'migrate' {
        Write-Step 'Applying migrations'
        Invoke-Checked 'alembic upgrade' { uv run alembic upgrade head }
    }
    'revision' {
        $message = if ($Rest) { $Rest -join ' ' } else { 'change' }
        Write-Step "Autogenerating revision: $message"
        uv run alembic revision --autogenerate -m $message
    }
    'seed' {
        Write-Step 'Importing the generated catalogue into Postgres and MinIO'
        Invoke-Checked 'seed' { uv run wascat db seed @Rest }
    }
    'backfill' {
        Write-Step 'Backfilling the remaining unsegmented frames'
        Invoke-Checked 'backfill' { uv run wascat ingest backfill @Rest }
    }
    'dev' {
        Write-Step 'Starting the API on http://localhost:8000 (docs at /api/v1/docs)'
        uv run uvicorn wascat.main:app --reload --port 8000 --host 127.0.0.1
    }
    'test' {
        Invoke-Checked 'pytest' { uv run pytest @Rest }
    }
    'check' {
        Write-Step 'ruff check'
        Invoke-Checked 'ruff check' { uv run ruff check . }
        Write-Step 'ruff format --check'
        Invoke-Checked 'ruff format' { uv run ruff format --check . }
        Write-Step 'mypy'
        Invoke-Checked 'mypy' { uv run mypy }
        Write-Step 'import-linter'
        Invoke-Checked 'lint-imports' { uv run lint-imports }
        Write-Ok 'All static checks passed.'
    }
    'fmt' {
        uv run ruff format .
        uv run ruff check --fix .
    }
    'doctor' {
        uv run wascat doctor
    }
    'logs' {
        docker compose logs -f @Rest
    }
    'psql' {
        docker compose exec postgres psql -U wascat -d wascat @Rest
    }
    default {
        Write-Host @'
wascat-backend tasks

  up         Start Postgres + MinIO (starts Docker Desktop if it is stopped)
  down       Stop containers, keep data
  reset      Destroy database and object storage volumes (asks first)

  migrate    alembic upgrade head
  revision   Autogenerate a migration:  .\tasks.ps1 revision add thing
  seed       Import seed/catalog.generated.json into Postgres + MinIO
  backfill   Ingest the remaining unsegmented frames

  dev        Run the API with reload on :8000
  test       pytest (extra args are passed through)
  check      ruff + format + mypy + import-linter
  fmt        Format and autofix
  doctor     Report on Python, Docker, database, storage and migration state
  logs       Follow container logs
  psql       Open a psql shell in the Postgres container
'@
    }
}
