@echo off
echo ============================================
echo  PPPokerHA -- Push and Deploy
echo ============================================
echo.
cd /d "%~dp0"

echo [0/4] Removing stale git locks...
if exist ".git\index.lock"  del /f /q ".git\index.lock"
if exist ".git\HEAD.lock"   del /f /q ".git\HEAD.lock"
if exist ".git\MERGE_HEAD"  del /f /q ".git\MERGE_HEAD"
echo.

echo [1/4] Pushing feat/tournament-details to origin...
git push origin feat/tournament-details
if %ERRORLEVEL% neq 0 (echo ERROR: push branch failed & pause & exit /b 1)
echo.

echo [2/4] Creating a clean temp branch from origin/main...
git fetch origin main
git checkout -B _deploy origin/main
if %ERRORLEVEL% neq 0 (echo ERROR: could not create temp branch & pause & exit /b 1)
echo.

echo [3/4] Merging feat/tournament-details into temp branch...
git merge feat/tournament-details --no-edit
if %ERRORLEVEL% neq 0 (echo ERROR: merge failed & git checkout feat/tournament-details & git branch -D _deploy & pause & exit /b 1)
echo.

echo [4/4] Pushing temp branch as main (triggers Railway deploy)...
git push origin _deploy:main
if %ERRORLEVEL% neq 0 (echo ERROR: push main failed & git checkout feat/tournament-details & git branch -D _deploy & pause & exit /b 1)
echo.

echo Returning to feature branch and cleaning up...
git checkout feat/tournament-details
git branch -D _deploy
echo.

echo ============================================
echo  Done! Railway will deploy in ~1-2 minutes.
echo  Check: https://pppokerha.up.railway.app
echo ============================================
pause
