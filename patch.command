#!/bin/bash
cd "$(dirname "$0")"
python3 patch.py
status=$?
echo
if [ "$status" -eq 0 ]; then
  echo "Done. Restart Cursor."
else
  echo "Exit $status. If Cursor.app was blocked, enable App Management for Terminal and run again."
fi
echo
read -r -p "Press Return to close..."
exit "$status"
