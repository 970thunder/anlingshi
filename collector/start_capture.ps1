param(
  [string]$TargetHosts = "",
  [int]$Port = 8080,
  [string]$PostUrl = "http://127.0.0.1:8000/api/v1/results",
  [string]$WriteToken = "change-me-local"
)

$env:TARGET_HOSTS = $TargetHosts
$env:POST_URL = $PostUrl
$env:WRITE_TOKEN = $WriteToken
$env:RAW_FLOW_LOG = "data/flows.jsonl"
New-Item -ItemType Directory -Force data | Out-Null
Write-Host "抓包代理: $($env:COMPUTERNAME):$Port"
Write-Host "目标域名:  $(if ($TargetHosts) { $TargetHosts } else { '未限制，首次勘探会记录所有域名' })"
Write-Host "操作手机/开发者工具完成一局后，另开终端运行: python collector/inspect_flows.py"
mitmdump --listen-host 0.0.0.0 --listen-port $Port -s collector/mitm_addon.py
