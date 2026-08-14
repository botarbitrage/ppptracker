@echo off
setlocal
echo ============================================
echo  PPPokerHT -- Push and Deploy
echo ============================================
echo.
cd /d "%~dp0"

rem --- Source branch: first argument, else the currently checked-out branch ---
set "SRC_BRANCH=%~1"
if "%SRC_BRANCH%"=="" for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set "SRC_BRANCH=%%b"
if /I "%SRC_BRANCH%"=="main" (echo ERROR: currently on 'main' -- check out your feature branch first, or pass its name as an argument. & pause & exit /b 1)
if /I "%SRC_BRANCH%"=="HEAD" (echo ERROR: detached HEAD -- check out a branch first. & pause & exit /b 1)
echo Deploying from branch: %SRC_BRANCH%
echo.

echo [0/5] Removing stale git locks...
if exist ".git\index.lock"  del /f /q ".git\index.lock"
if exist ".git\HEAD.lock"   del /f /q ".git\HEAD.lock"
if exist ".git\MERGE_HEAD"  del /f /q ".git\MERGE_HEAD"
echo.

echo [1/5] Pushing %SRC_BRANCH% to origin...
git push origin %SRC_BRANCH%
if %ERRORLEVEL% neq 0 (echo ERROR: push branch failed & pause & exit /b 1)
echo.

echo [2/5] Creating a clean temp branch from origin/main...
git fetch origin main
git checkout -B _deploy origin/main
if %ERRORLEVEL% neq 0 (echo ERROR: could not create temp branch & pause & exit /b 1)
echo.

echo [3/5] Merging %SRC_BRANCH% into temp branch...
git merge %SRC_BRANCH% --no-edit
if %ERRORLEVEL% neq 0 (echo ERROR: merge failed & git checkout %SRC_BRANCH% & git branch -D _deploy & pause & exit /b 1)
echo.

echo [4/5] Pushing temp branch as main (Railway auto-deploys main from GitHub)...
git push origin _deploy:main
if %ERRORLEVEL% neq 0 (echo ERROR: push main failed & git checkout %SRC_BRANCH% & git branch -D _deploy & pause & exit /b 1)
echo.

echo [5/5] Deploying Firestore rules to Firebase (project pppoker-analyser)...
echo   (still on _deploy, so the rules match what was just pushed to main)
call firebase deploy --only firestore:rules --project pppoker-analyser --non-interactive
if %ERRORLEVEL% neq 0 (echo ERROR: firebase rules deploy failed -- rules NOT updated & git checkout %SRC_BRANCH% & git branch -D _deploy & pause & exit /b 1)
echo.

echo Returning to %SRC_BRANCH% and cleaning up...
git checkout %SRC_BRANCH%
git branch -D _deploy
echo.

echo ============================================
echo  Done! Railway auto-deploys main in ~1-2 min.
echo  Check: https://ppptracker.up.railway.app
echo ============================================
endlocal
pause
