$ErrorActionPreference = 'Stop'
$cecilDir = 'C:\Users\Arnaud\.zcode\workspace\1793817436471-42fb2902-0f03-4b00-8231-e3c9bbe85b78\cecil\cecil_pkg\lib\net40'
Add-Type -Path (Join-Path $cecilDir 'Mono.Cecil.dll')

$target = 'C:\Program Files\SPAD.neXt\SPAD.neXt.Devices.dll'
$module = [Mono.Cecil.ModuleDefinition]::ReadModule($target)

$out = New-Object System.Text.StringBuilder
$interesting = @('SaitekFipDevice','FIP_MSG','FIP_MSG_REQUESTID','FIP_STATUS')

foreach ($t in $module.GetType('SPAD.neXt.Devices.SaitekFipDevice').NestedTypes + @($module.GetType('SPAD.neXt.Devices.SaitekFipDevice')) + @($module.GetType('SPAD.neXt.Devices.FIP_MSG'))) {
    if (-not $t) { continue }
    foreach ($m in $t.Methods) {
        if (-not $m.HasBody) { continue }
        if ($m.Name -match 'Get|Set_') { continue }   # skip trivial accessors
        [void]$out.AppendLine('====== ' + $t.FullName + '::' + $m.Name + ' ======')
        foreach ($ins in $m.Body.Instructions) {
            $op = $ins.Operand
            $opStr = ''
            if ($op -ne $null) { $opStr = $op.ToString() }
            [void]$out.AppendLine(('  IL_' + $ins.Offset.ToString('X4') + ': ' + $ins.OpCode.Name.PadRight(12) + ' ' + $opStr))
        }
        [void]$out.AppendLine('')
    }
}
$outFile = 'C:\Users\Arnaud\.zcode\workspace\1793817436471-42fb2902-0f03-4b00-8231-e3c9bbe85b78\cecil\fip_il.txt'
$out.ToString() | Set-Content $outFile
Write-Output ("IL dumpé: {0} ({1} octets)" -f $outFile, (Get-Item $outFile).Length)
