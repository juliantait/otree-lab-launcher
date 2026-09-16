@echo off

REM FYI: REM means that row is commented out

REM === Database Setup (only needed if you want to set up your own DB; this requires the postgres password to be entered into the terminal) ===
REM psql -U postgres
REM CREATE DATABASE experimentname;
REM CREATE USER otree WITH PASSWORD 'changeme';
REM GRANT ALL PRIVILEGES ON DATABASE experimentname TO otree;

REM === Database Management (at your lab, you can use a shared DB without issue) ===
set DB_NAME=otree
set DB_USER=otree
set DB_PASSWORD=changeme
set DB_HOST=localhost
set DB_PORT=5432
set DATABASE_URL=postgres://%DB_USER%:%DB_PASSWORD%@%DB_HOST%:%DB_PORT%/%DB_NAME%


REM === OTree variables ===
set OTREE_ADMIN_USERNAME=admin
set OTREE_ADMIN_PASSWORD=changeme
set OTREE_PRODUCTION=1 
set OTREE_AUTH_LEVEL=STUDY

REM === Starting the OTree project up on the server ===

REM === !!! ADOPT PATH TO YOUR OTREE PROJECT FOLDER!!! ===
cd "C:\Users\YourName\Desktop\my great experiment"
echo Type 'y' to reset the database (advised)
otree resetdb

REM Start the server in a new terminal
start "oTree Server" cmd /k otree prodserver

REM === Open oTree Monitoring Page === 

REM === !!! SELECT LAB !!! ===
REM SMALL LAB: YOUR_LAB_SERVER_IP
REM LARGE LAB: YOUR_LAB_SERVER_IP

REM Wait for prodserver to boot up & open oTree monitoring page
timeout /t 5 >nul
start http://YOUR_LAB_SERVER_IP:8000/rooms

echo Your postgres server is up and running :)
echo This terminal will close now
timeout /t 10 >nul