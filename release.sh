#!/usr/bin/env bash
# release.sh — build le .app, le .dmg, push le tag git et crée la release GitHub.
# Usage : ./release.sh
# Requiert : pyinstaller, hdiutil (macOS), git, gh CLI authentifié.

set -euo pipefail

cd "$(dirname "$0")"

# Lit la VERSION dans app.py (source unique de vérité)
VERSION=$(python3 -c 'import re; print(re.search(r"^VERSION\s*=\s*\"([^\"]+)\"", open("app.py").read(), re.M).group(1))')
TAG="v${VERSION}"
DMG_NAME="OBSMonitor-${VERSION}.dmg"

echo "→ Release ${TAG}"

# 1. Repo doit etre clean
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "✗ Modifs non commitees. Commit avant release." >&2
  exit 1
fi

# 2. Tag deja present ?
if git rev-parse "${TAG}" >/dev/null 2>&1; then
  echo "✗ Tag ${TAG} existe deja. Bump VERSION dans app.py." >&2
  exit 1
fi

# 3. Build .app
echo "→ pyinstaller…"
rm -rf build dist
pyinstaller --noconfirm OBSMonitor.spec

if [[ ! -d "dist/OBSMonitor.app" ]]; then
  echo "✗ dist/OBSMonitor.app introuvable." >&2
  exit 1
fi

# 4. Crée le DMG (drag-and-drop vers /Applications)
echo "→ Création DMG…"
STAGE=$(mktemp -d)
cp -R dist/OBSMonitor.app "${STAGE}/"
ln -s /Applications "${STAGE}/Applications"
rm -f "${DMG_NAME}"
hdiutil create -volname "OBS Monitor" -srcfolder "${STAGE}" \
  -ov -format UDZO "${DMG_NAME}"
rm -rf "${STAGE}"

# 5. Tag + push
git tag "${TAG}"
git push origin main
git push origin "${TAG}"

# 6. Release GitHub
echo "→ gh release create…"
LAST_TAG=$(git describe --tags --abbrev=0 "${TAG}^" 2>/dev/null || echo "")
if [[ -n "${LAST_TAG}" ]]; then
  NOTES=$(git log --pretty=format:'- %s' "${LAST_TAG}..${TAG}")
else
  NOTES=$(git log --pretty=format:'- %s' "${TAG}")
fi

gh release create "${TAG}" "${DMG_NAME}" \
  --title "OBS Monitor ${TAG}" \
  --notes "${NOTES}"

echo "✓ Release ${TAG} publiée. L'app installée va se mettre à jour automatiquement."
