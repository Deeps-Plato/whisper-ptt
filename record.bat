@echo off
del "%TEMP%\whisper_ptt.wav" 2>nul
ffmpeg.exe -f dshow -i audio="Microphone (Yeti Classic)" -ar 16000 -ac 1 -y "%TEMP%\whisper_ptt.wav"
