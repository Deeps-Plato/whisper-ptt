@echo off
del "%TEMP%\whisper_ptt.wav" 2>nul
"C:\Users\admin\scoop\apps\ffmpeg\current\bin\ffmpeg.exe" -f dshow -i audio="INPUT 1/2 (2- Volt 2)" -ar 16000 -ac 1 -y "%TEMP%\whisper_ptt.wav"
