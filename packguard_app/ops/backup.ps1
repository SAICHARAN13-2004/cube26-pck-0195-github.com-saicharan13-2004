param(
    [string]$BackupRoot = (Join-Path $PSScriptRoot "..\backups")
)

$ErrorActionPreference = "Stop"

function Get-PostgresConnectionParts([string]$ConnectionString) {
    $uri = [System.Uri]$ConnectionString
    $userinfo = [System.Uri]::UnescapeDataString($uri.UserInfo) -split ":", 2
    if ($userinfo.Count -ne 2 -or -not $uri.AbsolutePath.TrimStart("/")) {
        throw "PACKGUARD_DATABASE_URL must include username, password, host, and database."
    }
    return @{
        Host = $uri.Host
        Port = if ($uri.IsDefaultPort) { 5432 } else { $uri.Port }
        Username = $userinfo[0]
        Password = $userinfo[1]
        Database = $uri.AbsolutePath.TrimStart("/")
    }
}

if (-not $env:PACKGUARD_DATABASE_URL -or -not $env:PACKGUARD_OBJECT_STORAGE_URL) {
    throw "Set PACKGUARD_DATABASE_URL and PACKGUARD_OBJECT_STORAGE_URL before backing up."
}
$storageUri = [System.Uri]$env:PACKGUARD_OBJECT_STORAGE_URL
if ($storageUri.Scheme -ne "s3" -or -not $storageUri.Host) {
    throw "PACKGUARD_OBJECT_STORAGE_URL must be s3://bucket/optional-prefix."
}

$timestamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
$backupDirectory = Join-Path $BackupRoot $timestamp
$objectBackup = Join-Path $backupDirectory "objects"
New-Item -ItemType Directory -Path $objectBackup -Force | Out-Null
$databaseBackup = Join-Path $backupDirectory "packguard.dump"
$parts = Get-PostgresConnectionParts $env:PACKGUARD_DATABASE_URL
$env:PGPASSWORD = $parts.Password
try {
    & pg_dump --format=custom --no-owner --host $parts.Host --port $parts.Port --username $parts.Username --dbname $parts.Database --file $databaseBackup
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE." }

    $awsArguments = @("s3", "sync", $env:PACKGUARD_OBJECT_STORAGE_URL, $objectBackup, "--only-show-errors")
    if ($env:PACKGUARD_S3_ENDPOINT_URL) { $awsArguments += @("--endpoint-url", $env:PACKGUARD_S3_ENDPOINT_URL) }
    & aws @awsArguments
    if ($LASTEXITCODE -ne 0) { throw "Evidence object backup failed with exit code $LASTEXITCODE." }

    & pg_restore --list $databaseBackup | Out-File -FilePath (Join-Path $backupDirectory "archive-check.txt") -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL backup archive verification failed." }
    Write-Output "Verified database and evidence backup: $backupDirectory"
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
