#!/bin/bash
# Moki One-Line Installer
# Usage: curl -sSL https://raw.githubusercontent.com/domdewom2/moki/main/install.sh | bash
# Options: --no-analytics  Disable anonymous usage data

set -e

# Parse flags to pass through to setup.sh
SETUP_FLAGS=""
for arg in "$@"; do
  case "$arg" in
    --no-analytics) SETUP_FLAGS="$SETUP_FLAGS --no-analytics" ;;
  esac
done

echo ""
echo "Moki Installer"
echo "=================="
echo ""

# Check if already installed
if [ -d ~/moki ]; then
  echo "Moki is already installed in ~/moki"
  echo "   For updates: cd ~/moki && git pull"
  exit 1
fi

# Install git if needed
if ! command -v git &> /dev/null; then
  echo "Installing git..."
  sudo apt-get update
  sudo apt-get install -y git
fi

# Clone repository
echo "Downloading Moki..."
git clone https://github.com/domdewom2/moki.git ~/moki

# Run setup
echo ""
echo "Running setup..."
cd ~/moki/pi
chmod +x setup.sh
./setup.sh $SETUP_FLAGS
