#!/bin/sh
set -e
cd "$(dirname "$0")"
python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' || { echo "sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-venv"; exit 1; }
python3 -m venv --system-site-packages env
env/bin/pip install -q -r requirements.txt
env/bin/python dictate.py download
mkdir -p ~/.local/bin ~/.local/share/applications
printf '#!/bin/sh\nexec "%s/env/bin/python" "%s/dictate.py" "$@"\n' "$PWD" "$PWD" > ~/.local/bin/dictate
chmod +x ~/.local/bin/dictate
sed "s|@EXEC@|$HOME/.local/bin/dictate|" org.michalbenes.Dictate.desktop > ~/.local/share/applications/org.michalbenes.Dictate.desktop
env/bin/python dictate.py shortcut
command -v wl-copy >/dev/null || command -v xclip >/dev/null || echo "install wl-clipboard or xclip for clipboard copy"
