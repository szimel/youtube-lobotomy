<#
    Starts the Productivity Feed on http://127.0.0.1:5000

    Checks the things that otherwise fail confusingly later -- the virtualenv,
    the dependencies, the Jev key and the YouTube cookies -- then waits for the
    server to answer before opening a browser at it.

    It never prints the contents of .env or cookies.txt; it only reports whether
    they are present and usable.
#>

[CmdletBinding()]
param(
    [int]$Port = 5000,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Fail($message, $fix) {
    Write-Host ""
    Write-Host "  $message" -ForegroundColor Red
    if ($fix) { Write-Host "  $fix" -ForegroundColor Yellow }
    Write-Host ""
    exit 1
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Fail "No virtualenv found at .venv" "Create one with: py -m venv .venv"
}

& $python -c "import flask, dotenv, yt_dlp" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "The virtualenv is missing dependencies." ".venv\Scripts\python.exe -m pip install -r requirements.txt"
}

if (-not (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    Fail "No .env file, so there is no Jev API key." "Copy-Item .env.example .env, then put your key in it."
}

# Presence only: the value is never read out or printed.
& $python -c "import os,sys; from dotenv import load_dotenv; load_dotenv(); sys.exit(0 if os.environ.get('JEV_API_KEY','').strip() else 1)"
if ($LASTEXITCODE -ne 0) {
    Fail "JEV_API_KEY is not set in .env" "Add a line reading JEV_API_KEY=your-key (see .env.example)."
}

if (-not (Test-Path (Join-Path $PSScriptRoot "data\cookies.txt"))) {
    Write-Host "  No data\cookies.txt: the home feed and watch history will not work." -ForegroundColor Yellow
    Write-Host "  See 'Keeping YouTube signed in' in README.md." -ForegroundColor Yellow
    Write-Host ""
}

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Fail "Something is already listening on port $Port." "Close it, or run: .\run.ps1 -Port 5055"
}

$url = "http://127.0.0.1:$Port/"
Write-Host ""
Write-Host "  Productivity Feed" -ForegroundColor Green
Write-Host "  $url"
Write-Host "  Ctrl+C to stop."
Write-Host ""

$server = Start-Process -FilePath $python `
    -ArgumentList @("-m", "flask", "--app", "app", "run", "--host", "127.0.0.1", "--port", "$Port") `
    -NoNewWindow -PassThru

try {
    $ready = $false
    foreach ($attempt in 1..40) {
        Start-Sleep -Milliseconds 250
        if ($server.HasExited) { break }
        try {
            Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch {
            # Not up yet.
        }
    }

    if (-not $ready) {
        Fail "The server did not start." "Run it directly to see the error: .venv\Scripts\python.exe -m flask --app app run"
    }

    if (-not $NoBrowser) {
        Start-Process $url | Out-Null
    }

    Wait-Process -Id $server.Id
} finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
}
