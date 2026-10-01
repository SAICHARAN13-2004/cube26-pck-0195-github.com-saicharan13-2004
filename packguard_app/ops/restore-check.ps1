param(
    [Parameter(Mandatory = $true)]
    [string]$BackupDirectory
)

$ErrorActionPreference = "Stop"

function Get-PostgresConnectionParts([string]$ConnectionString) {
    $uri = [System.Uri]$ConnectionString
    $userinfo = [System.Uri]::UnescapeDataString($uri.UserInfo) -split ":", 2
    if ($userinfo.Count -ne 2 -or -not $uri.AbsolutePath.TrimStart("/")) {
        throw "The restore-test database URL must include username, password, host, and database."
    }
    return @{
        Host = $uri.Host
        Port = if ($uri.IsDefaultPort) { 5432 } else { $uri.Port }
        Username = $userinfo[0]
        Password = $userinfo[1]
        Database = $uri.AbsolutePath.TrimStart("/")
    }
}

$BackupDirectory = (Resolve-Path $BackupDirectory).Path
$databaseBackup = Join-Path $BackupDirectory "packguard.dump"
$objectBackup = Join-Path $BackupDirectory "objects"
if (-not (Test-Path $databaseBackup) -or -not (Test-Path $objectBackup)) {
    throw "The backup directory must contain packguard.dump and objects/."
}
if (-not $env:PACKGUARD_RESTORE_TEST_DATABASE_URL -or -not $env:PACKGUARD_RESTORE_TEST_OBJECT_STORAGE_URL) {
    throw "Set isolated PACKGUARD_RESTORE_TEST_DATABASE_URL and PACKGUARD_RESTORE_TEST_OBJECT_STORAGE_URL targets."
}
$parts = Get-PostgresConnectionParts $env:PACKGUARD_RESTORE_TEST_DATABASE_URL
if ($parts.Database -notmatch "_restore_test$") {
    throw "Refusing restore: the target database name must end in _restore_test."
}
$targetStorage = [System.Uri]$env:PACKGUARD_RESTORE_TEST_OBJECT_STORAGE_URL
if ($targetStorage.Scheme -ne "s3" -or -not $targetStorage.Host -or $env:PACKGUARD_RESTORE_TEST_OBJECT_STORAGE_URL -eq $env:PACKGUARD_OBJECT_STORAGE_URL) {
    throw "Use a dedicated, non-production s3:// restore-test bucket or prefix."
}

$env:PGPASSWORD = $parts.Password
try {
    & pg_restore --exit-on-error --single-transaction --no-owner --host=$parts.Host --port=$parts.Port --username=$parts.Username --dbname=$parts.Database $databaseBackup
    if ($LASTEXITCODE -ne 0) { throw "Database restore failed with exit code $LASTEXITCODE." }

    & psql --host $parts.Host --port $parts.Port --username $parts.Username --dbname $parts.Database --set=ON_ERROR_STOP=1 --tuples-only --command="SELECT COUNT(*) FROM schema_migrations WHERE version >= 3"
    if ($LASTEXITCODE -ne 0) { throw "Restored database migration check failed." }

    $awsArguments = @("s3", "sync", $objectBackup, $env:PACKGUARD_RESTORE_TEST_OBJECT_STORAGE_URL, "--only-show-errors")
    if ($env:PACKGUARD_S3_ENDPOINT_URL) { $awsArguments += @("--endpoint-url", $env:PACKGUARD_S3_ENDPOINT_URL) }
    & aws @awsArguments
    if ($LASTEXITCODE -ne 0) { throw "Evidence object restore failed with exit code $LASTEXITCODE." }

    Write-Output "Restore check completed. Verify record counts and authenticated evidence access in the isolated restore environment before relying on this backup."
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
