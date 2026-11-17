@echo off
copy /y "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\rc.exe" "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207\bin\HostX64\x64\"
if errorlevel 1 exit /b 1
copy /y "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\rcdll.dll" "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207\bin\HostX64\x64\"
exit /b %errorlevel%
