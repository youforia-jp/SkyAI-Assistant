@echo off
echo ===================================================
echo [OPERATOR] Booting Skyblock Omni-Operator Engine...
echo ===================================================

:: This runs the server using the module wrapper to bypass PATH errors
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000

:: If the server crashes, this keeps the window open so you can read the error
pause