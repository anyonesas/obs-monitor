#!/usr/bin/env bash
# release.sh — build le .app, le .dmg, et publie la release GitHub.
# Tolerant : marche meme si le dossier n'est pas un git checkout (le tag
# est cree directement sur main par gh release).
# Usage : ./release.sh
# Requiert : pyinstaller, hdiutil (macOS), gh CLI authentifie.

set -euo pipefail
cd "$(dirname "$0")"

REPO="anyonesas/obs-monitor"

# Lit la VERSION dans app.py (source unique de verite)
VERSION=$(python3 -c 'import re; print(re.search(r"^VERSION\s*=\s*\"([^\"]+)\"", open("app.py").read(), re.M).group(1))')
TAG="v${VERSION}"
DMG_NAME="OBSMonitor-${VERSION}.dmg"

echo "→ Release ${TAG}"

# Refuse si la release existe deja sur GitHub
if gh release view "${TAG}" --repo "${REPO}" >/dev/null 2>&1; then
  echo "✗ Release ${TAG} existe deja sur GitHub. Bump VERSION dans app.py." >&2
  exit 1
fi

# 1. Build .app via PyInstaller
echo "→ pyinstaller…"
rm -rf build dist
pyinstaller --noconfirm OBSMonitor.spec

if [[ ! -d "dist/OBSMonitor.app" ]]; then
  echo "✗ dist/OBSMonitor.app introuvable." >&2
  exit 1
fi

# 2. Cree le DMG (drag-and-drop vers /Applications)
echo "→ Creation DMG ${DMG_NAME}…"
STAGE=$(mktemp -d)
cp -R dist/OBSMonitor.app "${STAGE}/"
ln -s /Applications "${STAGE}/Applications"
rm -f "${DMG_NAME}"
hdiutil create -volname "OBS Monitor" -srcfolder "${STAGE}" \
  -ov -format UDZO "${DMG_NAME}"
rm -rf "${STAGE}"

# 3. Cree la release GitHub (tag sur main automatiquement)
echo "→ gh release create…"
gh release create "${TAG}" "${DMG_NAME}" \
  --repo "${REPO}" \
  --target main \
  --title "OBS Monitor ${TAG}" \
  --notes "Build automatique v${VERSION}"

echo ""
echo "✓ Release ${TAG} publiee."
echo "  Les Macs installes vont detecter la mise a jour dans les 30 min,"
echo "  ou immediatement via le menu > Verifier mise a jour."
