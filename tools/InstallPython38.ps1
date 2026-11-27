$ErrorActionPreference = 'Stop'
$target = Join-Path $env:LOCALAPPDATA 'KKAuto\Python38'
$python = Join-Path $target 'python.exe'
$installer = Join-Path $env:TEMP 'python-3.8.10-amd64.exe'
$url = 'https://www.python.org/ftp/python/3.8.10/python-3.8.10-amd64.exe'

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $client = New-Object System.Net.WebClient
    $client.DownloadFile($url, $installer)
    $signature = Get-AuthenticodeSignature -FilePath $installer
    if ($signature.Status -ne 'Valid' -or
        $null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        throw 'The downloaded Python installer signature could not be verified.'
    }
    $arguments = '/quiet InstallAllUsers=0 Include_launcher=0 Include_pip=1 Include_test=0 TargetDir="' + $target + '"'
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw ('Python installer failed with exit code ' + $process.ExitCode)
    }
    if (-not (Test-Path $python)) {
        throw ('Python was not found at ' + $python)
    }
    Write-Host ('Python installed at ' + $python)
    exit 0
} catch {
    Write-Host ('Automatic Python installation failed: ' + $_.Exception.Message)
    exit 1
}
