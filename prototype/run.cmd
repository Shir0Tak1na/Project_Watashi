@echo off
rem Project Watashi launcher (Windows).
rem
rem Why this exists: on many Windows machines `python` on PATH is the Microsoft
rem Store app-execution alias -- a zero-byte reparse point that opens a dialog
rem instead of running Python. Documented commands therefore must not rely on
rem bare `python`. This resolves the project's own interpreter instead.
rem
rem   run.cmd --selftest                run watashi_proto.py --selftest
rem   run.cmd fetch_model --check       run fetch_model.py --check
rem   run.cmd bench_nmt --threads 2,4   run bench_nmt.py --threads 2,4
rem
rem Works from any working directory.

setlocal EnableExtensions
set "HERE=%~dp0"
set "PYCMD="

rem 1. the project venv, which is what the prototype is developed against
if exist "%HERE%..\.venv\Scripts\python.exe" set "PYCMD="%HERE%..\.venv\Scripts\python.exe""
if not defined PYCMD if exist "%HERE%.venv\Scripts\python.exe" set "PYCMD="%HERE%.venv\Scripts\python.exe""

rem 2. the py launcher, which bypasses the Store alias
if not defined PYCMD (
  where py >nul 2>nul
  if not errorlevel 1 set "PYCMD=py -3"
)

if not defined PYCMD (
  echo [run] No Python interpreter found.
  echo [run] Expected a virtual environment at:
  echo [run]   %HERE%..\.venv\Scripts\python.exe
  echo [run] Create it with:
  echo [run]   py -3 -m venv "%HERE%..\.venv"
  echo [run]   "%HERE%..\.venv\Scripts\python.exe" -m pip install -r "%HERE%requirements.txt"
  exit /b 2
)

rem Arguments go through %* so their original quoting survives; _run.py decides
rem which script to execute.
%PYCMD% "%HERE%_run.py" %*
exit /b %ERRORLEVEL%
