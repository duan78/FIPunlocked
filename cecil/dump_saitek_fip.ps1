$ErrorActionPreference = 'Stop'
$cecilDir = 'C:\Users\Arnaud\.zcode\workspace\1793817436471-42fb2902-0f03-4b00-8231-e3c9bbe85b78\cecil\cecil_pkg\lib\net40'
Add-Type -Path (Join-Path $cecilDir 'Mono.Cecil.dll')

$target = 'C:\Program Files\SPAD.neXt\ext\Saitek.FIP.dll'
$module = [Mono.Cecil.ModuleDefinition]::ReadModule($target)

$out = New-Object System.Text.StringBuilder
[void]$out.AppendLine('=== TYPES ===')
foreach ($t in $module.GetTypes()) {
    [void]$out.AppendLine($t.FullName)
}
[void]$out.AppendLine('')
[void]$out.AppendLine('=== CORPS (methodes avec IL, sans accesseurs triviaux) ===')
foreach ($t in $module.GetTypes()) {
    foreach ($m in $t.Methods) {
        if (-not $m.HasBody) { continue }
        if ($m.Name -match '^(get_|set_|op_)') { continue }
        if ($m.IsConstructor) { continue }
        [void]$out.AppendLine('')
        [void]$out.AppendLine('====== ' + $t.FullName + '::' + $m.Name + ' (' + $m.ReturnType.Name + ') ======')
        foreach ($p in $m.Parameters) {
            [void]$out.AppendLine('    arg ' + $p.Name + ' : ' + $p.ParameterType.FullName)
        }
        foreach ($v in $m.Body.Variables) {
            [void]$out.AppendLine('    loc ' + $v.VariableType.Name)
        }
        foreach ($ins in $m.Body.Instructions) {
            $op = $ins.Operand
            $opStr = ''
            if ($op -ne $null) { $opStr = $op.ToString() }
            [void]$out.AppendLine(('  IL_' + $ins.Offset.ToString('X4') + ': ' + $ins.OpCode.Name.PadRight(13) + ' ' + $opStr))
        }
    }
}
$outFile = 'C:\Users\Arnaud\.zcode\workspace\1793817436471-42fb2902-0f03-4b00-8231-e3c9bbe85b78\cecil\saitek_fip_il.txt'
$out.ToString() | Set-Content $outFile
Write-Output ("IL dumpé: {0} ({1} octets)" -f $outFile, (Get-Item $outFile).Length)
