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

echo [1/5] Pushing feat/tournament-details to origin...
git push origin feat/tournament-details
if %ERRORLEVEL% neq 0 (echo ERROR: push branch failed & pause & exit /b 1)
echo.

echo [2/5] Creating a clean temp branch from origin/main...
git fetch origin main
git checkout -B _deploy origin/main
if %ERRORLEVEL% neq 0 (echo ERROR: could not create temp branch & pause & exit /b 1)
echo.

echo [3/5] Merging feat/tournament-details into temp branch...
git merge feat/tournament-details --no-edit
if %ERRORLEVEL% neq 0 (echo ERROR: merge failed & git checkout feat/tournament-details & git branch -D _deploy & pause & exit /b 1)
echo.

echo [4/5] Pushing temp branch as main (triggers Railway deploy)...
git push origin _deploy:main
if %ERRORLEVEL% neq 0 (echo ERROR: push main failed & git checkout feat/tournament-details & git branch -D _deploy & pause & exit /b 1)
echo.

echo [5/5] Deploying Firestore rules to Firebase (project pppoker-analyser)...
echo   (still on _deploy, so the rules match what was just pushed to main)
call firebase deploy --only firestore:rules --project pppoker-analyser --non-interactive
if %ERRORLEVEL% neq 0 (echo ERROR: firebase rules deploy failed -- rules NOT updated & git checkout feat/tournament-details & git branch -D _deploy & pause & exit /b 1)
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
