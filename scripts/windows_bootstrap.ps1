$ErrorActionPreference = 'Stop'

$Repo = 'https://github.com/pmaass424/ima-search-bot.git'
$Base = 'C:\ima-research-bot'
$Inbox = 'C:\ima-research-inbox'

Write-Host '== IMA Research Bot Windows bootstrap =='
Write-Host "Repo: $Repo"
Write-Host "Install dir: $Base"
Write-Host "Inbox: $Inbox"

function HasCommand($Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user;C:\ProgramData\chocolatey\bin;C:\Program Files\Git\cmd"
}

function Ensure-Chocolatey {
    if (HasCommand choco) {
        return
    }

    Write-Host 'Installing Chocolatey...'
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
    Refresh-Path
}

function Ensure-Git {
    if (HasCommand git) {
        return
    }

    if (HasCommand winget) {
        Write-Host 'Installing Git via winget...'
        winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
    } else {
        Ensure-Chocolatey
        Write-Host 'Installing Git via Chocolatey...'
        choco install git -y --no-progress
    }
    Refresh-Path
}

function Ensure-Python {
    if (HasCommand python) {
        return
    }

    if (HasCommand py) {
        return
    }

    if (HasCommand winget) {
        Write-Host 'Installing Python via winget...'
        winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
    } else {
        Ensure-Chocolatey
        Write-Host 'Installing Python via Chocolatey...'
        choco install python -y --no-progress
    }
    Refresh-Path
}

function Python-Cmd {
    if (HasCommand py) {
        return 'py -3'
    }
    return 'python'
}

New-Item -ItemType Directory -Force -Path $Inbox | Out-Null

Ensure-Git
Ensure-Python

if (Test-Path $Base) {
    Write-Host 'Updating existing repo...'
    Set-Location $Base
    git pull --ff-only
} else {
    Write-Host 'Cloning repo...'
    git clone $Repo $Base
    Set-Location $Base
}

$python = Python-Cmd
Write-Host "Using Python command: $python"

if (-not (Test-Path '.venv')) {
    Invoke-Expression "$python -m venv .venv"
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -e .
.\.venv\Scripts\python.exe -m playwright install chromium

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
}

$envPath = Join-Path $Base '.env'
$envText = Get-Content $envPath -Raw -Encoding UTF8
$envText = $envText -replace 'WATCH_DIR=.*', 'WATCH_DIR=C:\ima-research-inbox'
$envText = $envText -replace 'IMA_HUMAN_DOWNLOAD_DIR=.*', 'IMA_HUMAN_DOWNLOAD_DIR=C:\ima-research-inbox'
$envText = $envText -replace 'IMA_HUMAN_PROFILE_DIR=.*', 'IMA_HUMAN_PROFILE_DIR=C:\ima-browser-profile'
$envText = $envText -replace 'IMA_HUMAN_HEADLESS=.*', 'IMA_HUMAN_HEADLESS=0'
$envText = $envText -replace 'SEND_TEXT_UPDATES=.*', 'SEND_TEXT_UPDATES=1'
$envText = $envText -replace 'DIGEST_MODE=.*', 'DIGEST_MODE=1'
Set-Content -Path $envPath -Value $envText -Encoding UTF8

Write-Host ''
Write-Host 'Bootstrap done.'
Write-Host "Project: $Base"
Write-Host "Inbox: $Inbox"
Write-Host 'Next required manual config in .env: OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.'
Write-Host 'Next IMA step: install ima.copilot Windows app, log in once, then test CDP/Playwright control.'
