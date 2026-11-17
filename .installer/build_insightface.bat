@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64
if errorlevel 1 exit /b 1
set "PATH=C:\Users\Nikita\Desktop\door\.installer\msvc-shim;C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64;%PATH%"
where link.exe
where rc.exe
.venv\Scripts\python.exe -m pip install --no-build-isolation --constraint requirements.txt insightface==0.7.3
exit /b %errorlevel%
