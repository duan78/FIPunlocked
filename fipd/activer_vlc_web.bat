@echo off
rem =====================================================================
rem  activer_vlc_web.bat — active l'interface Web de VLC pour la page
rem  LECTURE du FIP (VLC 3.x n'a pas de SMTC, prevu pour la 4.0).
rem  Ferme VLC proprement (il sauve sa config), patche vlcrc, puis le
rem  relance sur la playlist Freebox. Idempotent, executable au double-clic.
rem =====================================================================
powershell -NoProfile -Command ^
  "$v = Get-Process vlc -ErrorAction SilentlyContinue;" ^
  "if ($v) { $v.CloseMainWindow() | Out-Null;" ^
  "        $v | Wait-Process -Timeout 15 -ErrorAction SilentlyContinue;" ^
  "        if (Get-Process vlc -ErrorAction SilentlyContinue) { Stop-Process -Name vlc -Force } }" ^
  "$f = \"$env:APPDATA\vlc\vlcrc\";" ^
  "$t = [IO.File]::ReadAllText($f);" ^
  "$t = $t.Replace('#extraintf=', 'extraintf=http').Replace('#http-password=', 'http-password=fipd');" ^
  "[IO.File]::WriteAllText($f, $t);" ^
  "Start-Process 'C:\Program Files\VideoLAN\VLC\vlc.exe' -ArgumentList 'http://mafreebox.freebox.fr/freeboxtv/playlist.m3u'"
echo.
echo Interface Web de VLC activee : http://127.0.0.1:8080  (mot de passe: fipd)
echo La page LECTURE du FIP detecte VLC d'ici 1-2 secondes.
timeout /t 4 >nul
