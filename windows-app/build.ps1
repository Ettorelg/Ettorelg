param([string]$Python = 'python', [string]$Compiler = "$env:LOCALAPPDATA\AlphaMenuBuildTools\Inno\ISCC.exe")
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
& $Python -m PyInstaller --noconfirm --windowed --onedir --name AlphaMenu --icon ../static/app-icon-512.png --paths ../tools --hidden-import epson_bridge --hidden-import axon_bridge main.py
if ($LASTEXITCODE) { throw 'Compilazione app fallita' }
& $Python collect_licenses.py
if ($LASTEXITCODE) { throw 'Raccolta licenze fallita' }
& $Python -c "from PIL import Image; Image.open('../static/app-icon-512.png').save('alpha-menu.ico', sizes=[(16,16),(32,32),(48,48),(256,256)])"
Invoke-WebRequest 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile MicrosoftEdgeWebview2Setup.exe
$signature = Get-AuthenticodeSignature MicrosoftEdgeWebview2Setup.exe
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') { throw 'Firma WebView2 non valida' }
& $Compiler installer.iss
if ($LASTEXITCODE) { throw 'Compilazione installer fallita' }
