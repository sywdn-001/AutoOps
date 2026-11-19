$ErrorActionPreference = 'Stop'

$port        = if ($env:BASTION_PORT)          { $env:BASTION_PORT }          else { '5000' }
$gatewayPort = if ($env:BASTION_GATEWAY_PORT)  { $env:BASTION_GATEWAY_PORT }  else { '2222' }
$instanceDir = if ($env:BASTION_INSTANCE_DIR)  { $env:BASTION_INSTANCE_DIR }  else { 'C:\data\instance' }
$noGateway   = $env:BASTION_CONTAINER_NO_GATEWAY -eq '1'

New-Item -ItemType Directory -Force -Path $instanceDir | Out-Null

Write-Host '------------------------------------------------------------'
Write-Host '  AutoOps Bastion (Windows container)'
Write-Host "  web UI / API : http://0.0.0.0:$port"
Write-Host "  health check : http://127.0.0.1:$port/api/health"
if ($noGateway) {
    Write-Host '  SSH gateway  : disabled (BASTION_CONTAINER_NO_GATEWAY=1)'
} else {
    Write-Host "  SSH gateway  : 0.0.0.0:$gatewayPort"
}
Write-Host "  data dir     : $instanceDir"
Write-Host "  python       : $((python --version) 2>&1)"
Write-Host '------------------------------------------------------------'

$arguments = @('run.py', '--host', '0.0.0.0', '--port', $port)

if ($noGateway) {
    $arguments += '--no-gateway'
} else {
    $arguments += @('--gateway-host', '0.0.0.0', '--gateway-port', $gatewayPort)
}

if ($env:BASTION_LOG_LEVEL) {
    $arguments += @('--log-level', $env:BASTION_LOG_LEVEL)
}

Set-Location C:\app\bastion-backend
python @arguments
exit $LASTEXITCODE
