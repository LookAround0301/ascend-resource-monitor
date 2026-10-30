param([switch]$SelfTest)
. (Join-Path $PSScriptRoot 'common.ps1')
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$form=New-Object Windows.Forms.Form
$form.Text='昇腾资源监控 · 部署向导'
$form.Size=New-Object Drawing.Size(1040,790)
$form.MinimumSize=New-Object Drawing.Size(1040,790)
$form.StartPosition='CenterScreen'
$form.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$tabs=New-Object Windows.Forms.TabControl
$tabs.Dock='Fill'; $form.Controls.Add($tabs)
$serverPage=New-Object Windows.Forms.TabPage; $serverPage.Text='1. 服务器'
$optionsPage=New-Object Windows.Forms.TabPage; $optionsPage.Text='2. 存储与访问'
$deployPage=New-Object Windows.Forms.TabPage; $deployPage.Text='3. 检查与部署'
$tabs.TabPages.AddRange(@($serverPage,$optionsPage,$deployPage))
function Label($Parent,[string]$Text,[int]$X,[int]$Y,[int]$Width=850) {
    $v=New-Object Windows.Forms.Label; $v.Text=$Text; $v.Location=New-Object Drawing.Point($X,$Y); $v.Size=New-Object Drawing.Size($Width,50); $Parent.Controls.Add($v); return $v
}
function Field($Parent,[string]$Name,[int]$Y,[string]$Default) {
    $null=Label $Parent $Name 24 $Y 220
    $v=New-Object Windows.Forms.TextBox; $v.Location=New-Object Drawing.Point(260,$Y); $v.Size=New-Object Drawing.Size(650,28); $v.Text=$Default; $Parent.Controls.Add($v); return $v
}
function Button($Parent,[string]$Text,[int]$X,[int]$Y,[int]$W=170) {
    $v=New-Object Windows.Forms.Button; $v.Text=$Text; $v.Location=New-Object Drawing.Point($X,$Y); $v.Size=New-Object Drawing.Size($W,38); $Parent.Controls.Add($v); return $v
}
$null=Label $serverPage '填写 Linux 昇腾服务器。监控 IP 是中心节点能够访问的内网地址；SSH 地址可以是业务 IP。' 24 24
$grid=New-Object Windows.Forms.DataGridView
$grid.Location=New-Object Drawing.Point(24,85); $grid.Size=New-Object Drawing.Size(960,300)
$grid.AllowUserToAddRows=$true; $grid.AllowUserToDeleteRows=$true; $grid.AutoSizeColumnsMode='Fill'; $grid.RowHeadersWidth=28
foreach ($col in @(@('name','节点名称'),@('host','SSH 地址'),@('ssh_port','SSH 端口'),@('ssh_user','SSH 用户'),@('monitor_ip','监控 IPv4'))) {
    $null=$grid.Columns.Add($col[0],$col[1])
}
$mode=New-Object Windows.Forms.DataGridViewComboBoxColumn
$mode.Name='node_exporter'; $mode.HeaderText='系统采集器'; $null=$mode.Items.AddRange(@('auto','reuse','install')); $null=$grid.Columns.Add($mode)
$grid.Columns['ssh_port'].FillWeight=60; $grid.Columns['ssh_user'].FillWeight=65
$grid.Add_DefaultValuesNeeded({param($sender,$eventArgs) $eventArgs.Row.Cells['ssh_port'].Value='22'; $eventArgs.Row.Cells['ssh_user'].Value='root'; $eventArgs.Row.Cells['node_exporter'].Value='auto'})
$serverPage.Controls.Add($grid)
$null=Label $serverPage '系统采集器：auto 自动复用已有 node_exporter，缺失时安装；reuse 只复用；install 单独安装。' 24 400
$null=Label $serverPage '中心节点（运行 Grafana 与 Prometheus）' 24 462 400
$central=New-Object Windows.Forms.ComboBox; $central.DropDownStyle='DropDownList'; $central.Location=New-Object Drawing.Point(430,460); $central.Size=New-Object Drawing.Size(400,28); $serverPage.Controls.Add($central)
$key=Field $serverPage 'SSH 私钥（可选）' 520 ''
$key.Width=550
$chooseKey=Button $serverPage '浏览' 830 515 80
$chooseKey.Add_Click({$d=New-Object Windows.Forms.OpenFileDialog; $d.Title='选择当前 PC 的 SSH 私钥（不会复制进安装包）'; if ($d.ShowDialog() -eq 'OK') {$key.Text=$d.FileName}; $d.Dispose()})
$null=Label $serverPage '留空时使用本机 SSH 默认密钥；需要密码时在运行终端输入。无需在向导里保存密码。' 24 576
$next=Button $serverPage '下一步' 814 636
$next.Add_Click({$tabs.SelectedIndex=1})

$id=Field $optionsPage '部署名称（英文短名）' 24 'ascend-monitor'
$title=Field $optionsPage '面板标题' 80 '昇腾资源实时监控'
$executor=Field $optionsPage '执行者姓名或工号' 136 $env:USERNAME
$storage=Field $optionsPage '已挂载的数据目录' 192 '/home'
$localPort=Field $optionsPage '本机访问端口' 248 '13000'
$grafPort=Field $optionsPage '服务器 Grafana 端口' 304 '3000'
$promPort=Field $optionsPage '服务器 Prometheus 端口' 360 '9090'
$offline=Field $optionsPage '离线软件包目录（可选）' 416 ''
$anon=New-Object Windows.Forms.CheckBox; $anon.Text='通过 SSH 隧道访问时允许免登录查看（Viewer，只读）'; $anon.Checked=$true; $anon.Location=New-Object Drawing.Point(24,478); $anon.Size=New-Object Drawing.Size(900,32); $optionsPage.Controls.Add($anon)
$null=Label $optionsPage '默认保留 7 天 / 5 GB 指标；采集端口为 9100、18082、18083。高级配置可导出 JSON 后修改。' 24 530
$null=Label $optionsPage '数据目录必须位于独立数据卷。软件配置默认为 Linux ARM64；其他架构请提供匹配的 software.json。' 24 584
$next2=Button $optionsPage '下一步' 814 636; $next2.Add_Click({$tabs.SelectedIndex=2})

$null=Label $deployPage '先检查所有主机，再开始部署。检查只上传小型检查脚本；部署会下载经 SHA-256 校验的软件包。' 24 24
$null=Label $deployPage '不会修改业务容器、NPU 驱动、SSH 公钥或防火墙。已有其他监控占用端口或固定配置时会停止并说明冲突。' 24 82
$null=Label $deployPage '部署完成后自动验证指标与面板，并生成 Windows 一键安装 / 打开面板 ZIP。' 24 140
$summary=New-Object Windows.Forms.TextBox; $summary.Multiline=$true; $summary.ReadOnly=$true; $summary.ScrollBars='Vertical'; $summary.Location=New-Object Drawing.Point(24,210); $summary.Size=New-Object Drawing.Size(960,245); $deployPage.Controls.Add($summary)
$export=Button $deployPage '导出配置' 24 480
$check=Button $deployPage '检查环境' 214 480
$deploy=Button $deployPage '开始部署' 404 480
$openFolder=Button $deployPage '打开结果目录' 594 480
$state=Label $deployPage '准备就绪。检查 / 部署会打开终端显示进度，并按需询问 SSH 密码。' 24 548
$null=Label $deployPage '首次连接会记录 SSH 主机密钥；主机密钥变化会拒绝连接。软件包下载失败时可填写离线包目录后重试。' 24 604

function Get-Configuration {
    $grid.EndEdit() | Out-Null
    $nodes=@()
    foreach ($row in $grid.Rows) {
        if ($row.IsNewRow) { continue }
        $values=@{}; foreach ($name in @('name','host','ssh_user','monitor_ip')) {$values[$name]=[string]$row.Cells[$name].Value}
        $values.ssh_port=[int]$row.Cells['ssh_port'].Value
        $values.node_exporter=[string]$row.Cells['node_exporter'].Value
        if (-not $values.node_exporter) {$values.node_exporter='auto'}
        $values.node_port=9100; $values.npu_port=18082; $values.process_port=18083
        $nodes+= [pscustomobject]$values
    }
    $config=[pscustomobject][ordered]@{id=$id.Text.Trim(); title=$title.Text.Trim(); executor_id=$executor.Text.Trim(); storage_root=$storage.Text.Trim().TrimEnd('/'); central_node=[string]$central.SelectedItem; nodes=$nodes; local_port=[int]$localPort.Text; grafana_port=[int]$grafPort.Text; prometheus_port=[int]$promPort.Text; anonymous_viewer=$anon.Checked; retention_days=7; retention_gb=5; shared_mounts=@('/mnt/share','/mnt/weight'); network_regex='.*'}
    Assert-Deployment $config
    return $config
}
function Refresh-Nodes {
    $selected=[string]$central.SelectedItem; $central.Items.Clear()
    foreach ($row in $grid.Rows) {
        $name=[string]$row.Cells['name'].Value
        if ($name -and -not $row.IsNewRow) {$null=$central.Items.Add($name)}
    }
    if ($selected -in @($central.Items)) {$central.SelectedItem=$selected} elseif ($central.Items.Count -gt 0) {$central.SelectedIndex=0}
}
$grid.Add_CellEndEdit({Refresh-Nodes})
$grid.Add_RowsRemoved({Refresh-Nodes})
$tabs.Add_SelectedIndexChanged({
    Refresh-Nodes
    if ($tabs.SelectedIndex -eq 2) {
        try {$summary.Text=(Get-Configuration | ConvertTo-Json -Depth 10)} catch {$summary.Text=$_.Exception.Message}
    }
})
$export.Add_Click({
    try {
        $config=Get-Configuration; $dialog=New-Object Windows.Forms.SaveFileDialog; $dialog.Filter='JSON (*.json)|*.json'; $dialog.FileName='deployment.json'
        if ($dialog.ShowDialog() -eq 'OK') {Write-JsonFile $config $dialog.FileName}; $dialog.Dispose()
    } catch {[Windows.Forms.MessageBox]::Show($_.Exception.Message,'配置错误') | Out-Null}
})
$script:running=$null
function Start-Work([bool]$CheckOnly) {
    try {
        if ($script:running -and -not $script:running.HasExited) {throw '已有检查或部署正在执行。'}
        $config=Get-Configuration
        $folder=Join-Path $env:LOCALAPPDATA ('AscendMonitor\configs\'+$config.id)
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        $path=Join-Path $folder 'deployment.json'; Write-JsonFile $config $path
        $arguments=@('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+(Join-Path $PSScriptRoot 'run-deployment.ps1')+'"'),'-ConfigPath',('"'+$path+'"'))
        if ($key.Text) {$arguments+=@('-IdentityFile',('"'+$key.Text+'"'))}
        if ($offline.Text) {$arguments+=@('-PackageDirectory',('"'+$offline.Text+'"'))}
        if ($CheckOnly) {$arguments+='-CheckOnly'}
        $script:running=Start-Process powershell.exe -ArgumentList $arguments -PassThru
        $check.Enabled=$false; $deploy.Enabled=$false; $state.Text='执行中：请查看运行终端。完成后结果保存在 LocalAppData\AscendMonitor\runs。'
        $timer.Start()
    } catch {[Windows.Forms.MessageBox]::Show($_.Exception.Message,'无法开始') | Out-Null}
}
$check.Add_Click({Start-Work $true}); $deploy.Add_Click({Start-Work $false})
$openFolder.Add_Click({$path=Join-Path $env:LOCALAPPDATA 'AscendMonitor'; New-Item -ItemType Directory -Force -Path $path | Out-Null; Start-Process explorer.exe $path})
$timer=New-Object Windows.Forms.Timer; $timer.Interval=1500
$timer.Add_Tick({if ($script:running -and $script:running.HasExited) {$timer.Stop(); $check.Enabled=$true; $deploy.Enabled=$true; $state.Text='运行已结束，请查看终端结果与结果目录。'}})
$prefill=Join-Path $PSScriptRoot 'deployment.json'
if (Test-Path -LiteralPath $prefill) {
    try {
        $c=Read-JsonFile $prefill
        foreach ($n in $c.nodes) {$null=$grid.Rows.Add([string]$n.name,[string]$n.host,[string]$n.ssh_port,[string]$n.ssh_user,[string]$n.monitor_ip,[string]$n.node_exporter)}
        $id.Text=$c.id; $title.Text=$c.title; $executor.Text=$c.executor_id; $storage.Text=$c.storage_root
        $localPort.Text=[string]$c.local_port; $grafPort.Text=[string]$c.grafana_port; $promPort.Text=[string]$c.prometheus_port; $anon.Checked=[bool]$c.anonymous_viewer
        Refresh-Nodes; $central.SelectedItem=[string]$c.central_node
    } catch {[Windows.Forms.MessageBox]::Show($_.Exception.Message,'配置加载失败') | Out-Null}
}
if ($SelfTest) {
    $form.CreateControl()
    if ($grid.Columns.Count -ne 6 -or $tabs.TabPages.Count -ne 3) {throw 'Wizard form construction failed.'}
    $form.Dispose(); Write-Output 'Wizard self-test passed'; exit 0
}
$null=$form.ShowDialog()
$timer.Dispose(); $form.Dispose()
