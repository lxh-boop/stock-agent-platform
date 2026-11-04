# 一键发布：打包已提交的代码并上传到服务器
# 用法：
#   .\deploy\publish.ps1                # 只传代码（日常更新）
#   .\deploy\publish.ps1 -IncludeData   # 连 data/ models/ 一起传（数据有变时）
# 之后去服务器执行：bash ~/stock-agent-platform/deploy/update.sh

param(
    [switch]$IncludeData,
    [string]$Server = "ubuntu@119.29.182.63"
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# archive 只打包"已提交"的内容，有未提交改动时给出提醒
$dirty = git status --porcelain
if ($dirty) {
    Write-Host "⚠ 有未提交的改动，不会包含在本次发布里：" -ForegroundColor Yellow
    $dirty | ForEach-Object { Write-Host "  $_" }
    Write-Host "（要发布它们请先 git add + git commit）" -ForegroundColor Yellow
}

git archive --format=tar.gz -o deploy\migration\code.tar.gz HEAD
$files = @("deploy\migration\code.tar.gz")

if ($IncludeData) {
    tar czf deploy\migration\data_models.tar.gz data models
    $files += "deploy\migration\data_models.tar.gz"
}

scp $files "${Server}:~/migration/"
Write-Host ""
Write-Host "✅ 上传完成（$($files -join ', ')）" -ForegroundColor Green
Write-Host "下一步，SSH 到服务器执行：bash ~/stock-agent-platform/deploy/update.sh"
if ($IncludeData) {
    Write-Host "（本次带了数据包，update.sh 之后手动执行：tar xzf ~/migration/data_models.tar.gz -C ~/stock-agent-platform）"
}
