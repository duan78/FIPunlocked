$ErrorActionPreference = 'Stop'
$cecilDir = 'C:\Users\Arnaud\.zcode\workspace\1793817436471-42fb2902-0f03-4b00-8231-e3c9bbe85b78\cecil\cecil_pkg\lib\net40'
Add-Type -Path (Join-Path $cecilDir 'Mono.Cecil.dll')

$target = 'C:\Program Files\Logitech\DirectOutput\DirectOutput.dll'
$module = [Mono.Cecil.ModuleDefinition]::ReadModule($target)

$out = New-Object System.Text.StringBuilder

# 1) toutes les methodes dont le nom ou les operandes mentionnent Server / StartServer / SaiFip
foreach ($t in $module.GetTypes()) {
    foreach ($m in $t.Methods) {
        if (-not $m.HasBody) { continue }
        $bodyTxt = ($m.Body.Instructions | ForEach-Object { [string]$_.Operand }) -join ' '
        if ($m.Name -match 'Server' -or $bodyTxt -match 'StartServer|SendServerMsg|SendServerFile') {
            [void]$out.AppendLine('====== ' + $t.FullName + '::' + $m.Name + ' (' + $m.ReturnType.Name + ') ======')
            foreach ($p in $m.Parameters) {
                [void]$out.AppendLine('    arg ' + $p.Name + ' : ' + $p.ParameterType.FullName)
            }
            foreach ($ins in $m.Body.Instructions) {
                $op = $ins.Operand
                $opStr = ''
                if ($op -ne $null) { $opStr = $op.ToString() }
                [void]$out.AppendLine(('  IL_' + $ins.Offset.ToString('X4') + ': ' + $ins.OpCode.Name.PadRight(12) + ' ' + $opStr))
            }
            [void]$out.AppendLine('')
        }
    }
}
$outFile = 'C:\Users\Arnaud\.zcode\workspace\1793817436471-42fb2902-0f03-4b00-8231-e3c9bbe85b78\cecil\do_server_il.txt'
$out.ToString() | Set-Content $outFile
Write-Output ("IL dumpé: {0} ({1} octets)" -f $outFile, (Get-Item $outFile).Length)
