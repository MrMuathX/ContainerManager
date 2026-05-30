#Requires -Version 5.1
# .SYNOPSIS
# Deploy ContainerManager to a Linux server over SSH.
#
# .DESCRIPTION
# Builds a tarball from tracked files (working-tree by default), uploads with scp, extracts
# on the remote host under RemotePath, optionally bootstraps .env from .env.example, then
# runs docker compose build and up on the server.
#
param(
    [string] $RemoteHost = "server",
    [string] $RemotePath = "~/containermanager",
    [int] $HealthPort = 8081,
    [int] $HealthCheckAttempts = 120,
    [int] $HealthCheckIntervalSec = 2,
    [switch] $SkipHealthCheck,
    [switch] $SkipEnvBootstrap,
    [switch] $RequireCleanWorktree,
    [switch] $NoDockerCache,
    [switch] $BundleWorkingTree,
    [switch] $WhatIf
)

$ErrorActionPreference = "Stop"
$Script:SshArgs = @(
    "-o", "BatchMode=yes",
    "-o", "PreferredAuthentications=publickey",
    "-o", "PasswordAuthentication=no"
)

function Invoke-Ssh {
    param(
        [Parameter(Mandatory)][string] $SshTarget,
        [Parameter(Mandatory)][string] $RemoteCommand
    )
    & ssh @Script:SshArgs $SshTarget $RemoteCommand
}

function Invoke-Scp {
    param(
        [Parameter(Mandatory)][string] $SourcePath,
        [Parameter(Mandatory)][string] $TargetSpec
    )
    & scp @Script:SshArgs $SourcePath $TargetSpec
}

function ConvertTo-UnixLf {
    param([string] $Text)
    $cr = [char]13
    $lf = [char]10
    return (($Text -replace ($cr + $lf), $lf) -replace $cr, $lf)
}

function ConvertTo-BashSingleQuoted {
    param([string] $Value)
    return "'" + ($Value -replace "'", "'\''") + "'"
}

function Invoke-RemoteBashStdin {
    # PowerShell 5.x can inject CRLF into piped stdin for native exe; base64 avoids that.
    param([string] $SshTarget, [string] $Script, [string[]] $BashArgs)
    $lf = ConvertTo-UnixLf $Script
    $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($lf))
    $argList = ($BashArgs | ForEach-Object { ConvertTo-BashSingleQuoted $_ }) -join ' '
    $remote = 'printf ''%s'' ' + $b64 + ' | base64 -d | bash -s -- ' + $argList
    Invoke-Ssh -SshTarget $SshTarget -RemoteCommand $remote
}

function Get-RemoteExpandedPath {
    param([string] $SshTarget, [string] $Path)
    $q = ConvertTo-BashSingleQuoted $Path
    $remoteCmd = 'python3 -c ''import os,sys; print(os.path.expanduser(sys.argv[1]))'' ' + $q
    $out = Invoke-Ssh -SshTarget $SshTarget -RemoteCommand $remoteCmd
    if (-not $out) { throw ('Could not resolve remote path for {0}; ssh returned empty.' -f $Path) }
    return ([string]$out).Trim()
}

function Test-SshKeyAuth {
    param([Parameter(Mandatory)][string] $SshTarget)
    & ssh @Script:SshArgs $SshTarget "echo SSH_KEY_OK" | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Invoke-DeployContainerManagerWork {
    param([Parameter(Mandatory)][string] $DeployRoot)

    Push-Location $DeployRoot
    try {
        git rev-parse --is-inside-work-tree | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Not a Git repository. Run this script from the ContainerManager project root."
        }

        $remoteTgz = "/tmp/containermanager-deploy.tgz"
        if (-not (Test-SshKeyAuth -SshTarget $RemoteHost)) {
            throw ("SSH key auth failed for '{0}'. Password prompts are disabled by script. Fix ~/.ssh/config key/user or authorized_keys on server, then retry." -f $RemoteHost)
        }
        $destResolved = Get-RemoteExpandedPath -SshTarget $RemoteHost -Path $RemotePath

        # Default behavior: include tracked working-tree changes unless explicitly disabled.
        $useWorkingTreeBundle = $BundleWorkingTree -or (-not $PSBoundParameters.ContainsKey('BundleWorkingTree'))

        if ($WhatIf) {
            $short = git rev-parse --short HEAD
            Write-Host "WhatIf: Commit: $short"
            Write-Host "WhatIf: Remote path resolves to: $destResolved"
            if ($useWorkingTreeBundle) {
                Write-Host '[WhatIf] tar via git ls-files working-tree snapshot -> temp.tgz -> scp'
            } else {
                Write-Host ('[WhatIf] git archive HEAD -> temp.tgz -> scp {0}:{1}' -f $RemoteHost, $remoteTgz)
            }
            Write-Host "WhatIf: extract into $destResolved, rm $remoteTgz"
            if (-not $SkipEnvBootstrap) {
                Write-Host ('[WhatIf] if missing {0}/.env -> copy .env.example on server' -f $destResolved)
            }
            if ($NoDockerCache) {
                Write-Host '[WhatIf] ssh: docker compose build --no-cache && docker compose up -d'
            } else {
                Write-Host '[WhatIf] ssh: docker compose build && docker compose up -d'
            }
            if (-not $SkipHealthCheck) {
                Write-Host ('[WhatIf] curl http://127.0.0.1:{0}/login on server, retry up to {1} x {2}s' -f $HealthPort, $HealthCheckAttempts, $HealthCheckIntervalSec)
            }
            return
        }

        $gitShort = (git rev-parse --short HEAD).Trim()
        $gitSubj = (git log -1 --pretty=format:%s)
        Write-Host "Deploying $gitShort - $gitSubj" -ForegroundColor Cyan

        $porcelain = git status --porcelain
        if ($RequireCleanWorktree -and $porcelain) {
            throw 'Working tree is not clean. Commit or stash changes, then redeploy, or omit -RequireCleanWorktree.'
        }
        if ($porcelain) {
            if ($useWorkingTreeBundle) {
                Write-Host 'BundleWorkingTree: including current on-disk content for tracked files; untracked files excluded.' -ForegroundColor DarkGray
            } else {
                Write-Warning 'Uncommitted changes exist and git archive includes committed files only. Commit or use -BundleWorkingTree.'
            }
        }

        $stashFile = Join-Path $env:TEMP ('containermanager-deploy-{0}.tgz' -f (Get-Date -Format 'yyyyMMddHHmmss'))
        if ($useWorkingTreeBundle) {
            Write-Host 'Creating tarball from tracked files (current working tree content)...'
            $listFile = Join-Path $env:TEMP ('containermanager-flist-{0}.txt' -f (Get-Date -Format 'yyyyMMddHHmmss'))
            $tracked = @(git ls-files)
            if ($LASTEXITCODE -ne 0) { throw "git ls-files failed." }
            if ($tracked.Count -eq 0) { throw "No tracked files to pack." }

            $existingTracked = @($tracked | Where-Object { Test-Path -LiteralPath $_ })
            if ($existingTracked.Count -eq 0) { throw 'No tracked files exist on disk to pack.' }

            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllLines($listFile, $existingTracked, $utf8NoBom)
            try {
                & tar -czf $stashFile -T $listFile
                if ($LASTEXITCODE -ne 0) { throw 'tar -czf failed. Ensure tar.exe is available and run from repo root.' }
            } finally {
                Remove-Item -Force $listFile -ErrorAction SilentlyContinue
            }
        } else {
            Write-Host ('Creating archive from git HEAD {0}...' -f $gitShort)
            git archive --format=tar.gz -o $stashFile HEAD
            if ($LASTEXITCODE -ne 0) { throw "git archive failed." }
        }

        if (-not (Test-Path $stashFile)) { throw "Archive file was not created." }

        Write-Host ('Uploading to ' + $RemoteHost + ':' + $remoteTgz)
        Invoke-Scp -SourcePath $stashFile -TargetSpec ($RemoteHost + ':' + $remoteTgz)
        if ($LASTEXITCODE -ne 0) { throw "scp failed." }

        $extractScript = @'
set -euo pipefail
REMOTE_PATH="$1"
DEST="$(python3 -c 'import os,sys; print(os.path.expanduser(sys.argv[1]))' "$REMOTE_PATH")"
mkdir -p "$DEST"
tar -xzf /tmp/containermanager-deploy.tgz -C "$DEST"
rm -f /tmp/containermanager-deploy.tgz
echo "$DEST"
'@
        Write-Host ('Extracting on {0} into {1}; resolved path {2} ...' -f $RemoteHost, $RemotePath, $destResolved)
        Invoke-RemoteBashStdin -SshTarget $RemoteHost -Script $extractScript -BashArgs @($RemotePath) | Out-Null
        if ($LASTEXITCODE -ne 0) { throw ('Remote extract failed; ssh exit {0}.' -f $LASTEXITCODE) }

        if (-not $SkipEnvBootstrap) {
            $envCheck = Invoke-Ssh -SshTarget $RemoteHost -RemoteCommand ('test -f {0}/.env && echo yes || echo no' -f $destResolved)
            if (($envCheck | Out-String).Trim() -ne "yes") {
                Write-Host "No .env on server; creating from .env.example ..."
                $bootstrapEnvScript = @'
set -euo pipefail
DEST="$1"
if [ ! -f "$DEST/.env" ] && [ -f "$DEST/.env.example" ]; then
  cp "$DEST/.env.example" "$DEST/.env"
fi
'@
                Invoke-RemoteBashStdin -SshTarget $RemoteHost -Script $bootstrapEnvScript -BashArgs @($destResolved)
                if ($LASTEXITCODE -ne 0) { throw "Remote .env bootstrap failed." }
            }
        }

        if ($NoDockerCache) {
            Write-Host "docker compose build --no-cache && up -d ..."
            $composeCmd = 'cd ' + (ConvertTo-BashSingleQuoted $destResolved) + ' && docker compose build --no-cache && docker compose up -d'
        } else {
            Write-Host "docker compose build && up -d ..."
            $composeCmd = 'cd ' + (ConvertTo-BashSingleQuoted $destResolved) + ' && docker compose build && docker compose up -d'
        }
        Invoke-Ssh -SshTarget $RemoteHost -RemoteCommand $composeCmd
        if ($LASTEXITCODE -ne 0) {
            throw ('docker compose deploy failed; ssh exit {0}. Check remote logs: docker compose logs containermanager' -f $LASTEXITCODE)
        }

        if (-not $SkipHealthCheck) {
            Write-Host ('Health check: GET http://127.0.0.1:{0}/login on {1}, up to {2} tries, {3}s apart ...' -f $HealthPort, $RemoteHost, $HealthCheckAttempts, $HealthCheckIntervalSec)
            $healthScript = @'
set -euo pipefail
PORT="$1"
ATTEMPTS="$2"
INTERVAL="$3"
for ((i=1;i<=ATTEMPTS;i++)); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/login" || true)"
  if [ "$code" = "200" ]; then
    echo "ok"
    exit 0
  fi
  sleep "$INTERVAL"
done
echo "health check failed for /login on port ${PORT}" >&2
exit 1
'@
            Invoke-RemoteBashStdin -SshTarget $RemoteHost -Script $healthScript -BashArgs @("$HealthPort", "$HealthCheckAttempts", "$HealthCheckIntervalSec")
            if ($LASTEXITCODE -ne 0) {
                throw ('Health check failed after retries; ssh exit {0}.' -f $LASTEXITCODE)
            }
            Write-Host "Health OK."
        }

        Remove-Item -Force $stashFile -ErrorAction SilentlyContinue
        Write-Host "Done."
    } finally {
        Pop-Location
    }
}

Invoke-DeployContainerManagerWork -DeployRoot $PSScriptRoot
