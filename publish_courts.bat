@echo off
rem Runs the court finder and pushes the page to GitHub Pages.
rem Double-click to publish now; Task Scheduler runs this file on a schedule.
cd /d "%~dp0"
py court_finder.py --publish
