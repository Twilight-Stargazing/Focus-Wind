#ifndef AppVersion
  #error Hay chay build.bat de bien dich (so phien ban lay tu version.py)
#endif

[Setup]
AppName=Focus Wind
AppVersion={#AppVersion}
AppPublisher=Focus Wind
DefaultDirName={autopf}\Focus Wind
DefaultGroupName=Focus Wind
OutputDir=dist
OutputBaseFilename=FocusWind-Setup
Compression=lzma2/ultra64
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\FocusWind.exe
AppId={{9f96e3f6-eea7-48b2-9576-1065fa9e0544}}
CloseApplications=yes

[Languages]
Name: "default"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Tạo biểu tượng ngoài Desktop"; Flags: unchecked
Name: "startupicon"; Description: "Khởi động cùng Windows"; Flags: checkedonce

[Files]
Source: "dist\FocusWind.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Focus Wind"; Filename: "{app}\FocusWind.exe"
Name: "{autodesktop}\Focus Wind"; Filename: "{app}\FocusWind.exe"; Tasks: desktopicon
Name: "{userstartup}\Focus Wind"; Filename: "{app}\FocusWind.exe"; Tasks: startupicon

[Run]
Filename: "{app}\FocusWind.exe"; Description: "Mở Focus Wind"; Flags: nowait postinstall