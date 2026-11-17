BOQ -> RFQ  -  Setup (one time, about 15 minutes)
=================================================

1) Unzip this folder somewhere permanent, e.g. Documents\BOQ2RFQ
   (not inside the zip, and not in Program Files).

2) Install Ollama (the AI engine):
      https://ollama.com/download  ->  Download for Windows  ->  run the installer.

3) Download the AI model (about 10 GB). Open "Command Prompt" and type:
      ollama pull gemma4:e4b
   Wait until it says "success". Close the window.


Using the app
-------------
- Double-click BOQ2RFQ.exe.
  The first time, Windows may say "Windows protected your PC":
  click "More info" -> "Run anyway".
- A black window opens and your browser opens the app automatically.
  Keep the black window open while you work; close it to stop the app.
- The black window shows "AI model: ... (ready)" when the AI is working.
  If it says Ollama is not running, start "Ollama" from the Start menu and
  reopen BOQ2RFQ.exe. Without the AI, the app still works, but less accurately.
- All outputs (work packages, RFQs) are saved in the "workspace" folder
  next to BOQ2RFQ.exe.

Requirements: Windows 10/11 (64-bit), 16 GB RAM recommended for the AI model.
Microsoft Excel is optional (used for exact-look sheet snapshots).
