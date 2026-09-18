[Setup]
AppId={{4B329E79-E28A-41C8-A282-FD6EBF2B0F48}
AppName=Alpha Menu
AppVersion=1.1.7
AppPublisher=Alpha System srl
AppPublisherURL=https://menu.alphasystemsrl.it
DefaultDirName={localappdata}\Programs\Alpha Menu
DefaultGroupName=Alpha Menu
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\static\windows
OutputBaseFilename=AlphaMenu-Setup-1.1.7
SetupIconFile=alpha-menu.ico
UninstallDisplayIcon={app}\AlphaMenu.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"

[Files]
Source: "dist\AlphaMenu\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{autoprograms}\Alpha Menu"; Filename: "{app}\AlphaMenu.exe"; AppUserModelID: "AlphaSystem.AlphaMenu"
Name: "{autodesktop}\Alpha Menu"; Filename: "{app}\AlphaMenu.exe"; AppUserModelID: "AlphaSystem.AlphaMenu"

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Verifica Microsoft WebView2..."; Flags: waituntilterminated
Filename: "{app}\AlphaMenu.exe"; Description: "Apri Alpha Menu"; Flags: nowait postinstall skipifsilent
