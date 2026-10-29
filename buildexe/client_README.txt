TikTok Guard {VERSION}
=====================

Paste the link of any TikTok post. TikTok Guard reads its comments, shows you the negative ones and the questions, writes a
reply with the AI (or you write it yourself), sends it from the account you choose and shows you a screenshot as proof.
Nothing runs in the background: no monitoring, no notifications. It works only while its window is open, and only when you ask.


WHAT YOU NEED
-------------
* Windows 10 or 11 (64-bit) and an internet connection.
* Microsoft Edge, Google Chrome or Brave (Edge is already part of Windows). TikTok Guard uses it, in a window of its own;
  your normal browsing is never touched.
* An OpenRouter key for the AI (free to create at https://openrouter.ai/keys).
* No administrator password is needed.


INSTALL (one minute)
--------------------
1. Double-click   TikTok-Guard-Setup-{VERSION}.exe
2. The first time, Windows may say "Windows protected your PC" (the program is new and not signed with a paid certificate).
   Click "More info", then "Run anyway". This happens once.
3. Click Next, choose whether you want a shortcut on the desktop, click Install, then Finish.
   TikTok Guard opens by itself. Next time, use the "TikTok Guard" icon on the desktop or in the Start menu.


FIRST STEPS (about five minutes)
--------------------------------
1. Accounts -> "Add account". A browser window opens on TikTok: log in the way you always do. TikTok Guard notices the login,
   saves the account and closes that window. Add every account you want to reply with.
   TikTok Guard does not ask for or store your password - the browser window keeps the login, as it always does.
   (Google Chrome may show a grey bar "You are using an unsupported command-line flag" in that window. It is harmless: ignore it or close it.)
2. Controls -> "OpenRouter API key": paste your key and press Save.
3. AI & Brand -> write what the AI should know about your company (what you sell, shipping, returns, opening hours ...).
   Without it the AI will not invent facts: it answers that you will confirm.
4. Home -> paste a post link -> "Analyze". Open a comment, press Reply, choose the account, Send.

If you close the window while a reply is being sent, the program waits until that reply has gone out and then closes by itself.


UPDATE AND REMOVE
-----------------
* Update: run the newer Setup. It replaces the program and keeps all your data. (Close TikTok Guard first - Setup tells you if it is open.)
* Remove: Windows Settings -> Apps -> TikTok Guard -> Uninstall. It asks whether to delete your data too; "No" keeps it.


WHERE YOUR DATA IS
------------------
Everything you create (accounts, settings, replies, screenshots, logs) is kept in:

    %LOCALAPPDATA%\TikTok Guard          (paste this into the Windows Explorer address bar)

A copy of the database is made once a day (the last 7 are kept) in the folder  data\backups  inside it.


IF SOMETHING DOES NOT WORK
--------------------------
Start menu -> TikTok Guard -> "TikTok Guard Diagnostics". A window checks the program, the browser, the internet and your setup
in about half a minute, and saves the result as a file ("last-report.txt", its location is shown in the window). Send us that file.
The report contains no passwords and no AI key.

Common causes:
* The antivirus program removed or blocked a file: add the TikTok Guard program folder to its exceptions and install again.
* "The program could not start": install or update Microsoft Edge / Google Chrome and try again.
* An account shows "Needs login": open Accounts and press "Log in again" for it.
* TikTok sometimes limits how often a profile can be opened. If a read or a reply fails with such a message, wait ten minutes
  or choose another account - nothing is lost.


WITHOUT THE INSTALLER (portable)
--------------------------------
The folder "portable" has TikTok-Guard-{VERSION}-portable.zip. Unpack it completely (right-click -> Extract All), keep the folder
"TikTok Guard" together (the .exe does not work without the "_internal" folder next to it) and double-click "TikTok Guard.exe".
It uses the same data folder as the installed version.


Your use of TikTok through this program remains subject to TikTok's own terms. Replies you send are posted from your own accounts.
