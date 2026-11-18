#!/usr/bin/env bash
# Install rf-spectrum on a Raspberry Pi 5 (Pi OS Bookworm, 64-bit).
# Usage:  sudo bash deploy/install.sh
#
# Idempotent and non-interactive. Does NOT enable the service at boot:
# pi-profiles starts and stops it.
#
# Options (environment variables):
#   RTLSDR_DRIVER=blog     build the RTL-SDR Blog librtlsdr (supports V3 and V4)  [default]
#   RTLSDR_DRIVER=debian   use Debian's rtl-sdr packages (fine for V3, NOT for V4)
#   RTLSDR_DRIVER=none     skip RTL-SDR driver install
#   RTLSDR_BLOG_REF=v1.3.6 git tag of github.com/rtlsdrblog/rtl-sdr-blog to build
#   INSTALL_HACKRF=1       also install HackRF tools (hackrf_sweep, udev rules)
set -euo pipefail

APP=rf-spectrum
APP_DIR=/opt/$APP
DATA_DIR=/var/lib/$APP
ENV_FILE=/etc/$APP.env
SVC_USER=rf-spectrum
RTLSDR_DRIVER="${RTLSDR_DRIVER:-blog}"
RTLSDR_BLOG_REF="${RTLSDR_BLOG_REF:-v1.3.6}"
INSTALL_HACKRF="${INSTALL_HACKRF:-0}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export DEBIAN_FRONTEND=noninteractive

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash deploy/install.sh" >&2
    exit 1
fi

echo "=== rf-spectrum install (receive-only spectrum awareness) ==="

echo "[1/8] System packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync usbutils \
    git cmake build-essential pkg-config libusb-1.0-0-dev >/dev/null

echo "[2/8] RTL-SDR driver ($RTLSDR_DRIVER)"
case "$RTLSDR_DRIVER" in
    blog)
        MARKER=/usr/local/share/rf-spectrum/rtlsdr-blog.ref
        if [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$RTLSDR_BLOG_REF" ] && [ -x /usr/local/bin/rtl_power ]; then
            echo "  RTL-SDR Blog driver $RTLSDR_BLOG_REF already installed"
        else
            BUILD=$(mktemp -d)
            git clone -q --depth 1 --branch "$RTLSDR_BLOG_REF" https://github.com/rtlsdrblog/rtl-sdr-blog "$BUILD/src"
            cmake -S "$BUILD/src" -B "$BUILD/build" -DCMAKE_INSTALL_PREFIX=/usr/local \
                -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON >/dev/null
            cmake --build "$BUILD/build" -j"$(nproc)" >/dev/null
            cmake --install "$BUILD/build" >/dev/null
            ldconfig
            install -d /usr/local/share/rf-spectrum
            echo "$RTLSDR_BLOG_REF" > "$MARKER"
            rm -rf "$BUILD"
            echo "  built and installed RTL-SDR Blog driver $RTLSDR_BLOG_REF into /usr/local"
        fi
        if dpkg -l 2>/dev/null | grep -qE '^ii +librtlsdr'; then
            echo "  NOTE: Debian librtlsdr is also installed. rf-spectrum's start.sh puts"
            echo "        /usr/local/lib first, so the service uses the Blog build. Other tools"
            echo "        may still pick up the Debian one (fine for a V3, not for a V4)."
        fi
        ;;
    debian)
        apt-get install -y -qq rtl-sdr librtlsdr-dev >/dev/null
        ;;
    none) echo "  skipped" ;;
    *) echo "Unknown RTLSDR_DRIVER=$RTLSDR_DRIVER" >&2; exit 1 ;;
esac

echo "[3/8] udev rules + DVB-T driver blacklist"
cat > /etc/udev/rules.d/20-rtlsdr.rules <<'EOF'
# RTL2832U SDR dongles (RTL-SDR Blog V3/V4 and generic): user access via plugdev
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0660", SYMLINK+="rtl_sdr"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0660", SYMLINK+="rtl_sdr"
EOF
cat > /etc/modprobe.d/blacklist-rtl-sdr.conf <<'EOF'
# The kernel claims RTL2832U dongles as DVB-T TV tuners; stop it so librtlsdr can use them.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832_sdr
blacklist rtl2832
blacklist rtl2830
blacklist r820t
EOF
for mod in rtl2832_sdr dvb_usb_rtl28xxu rtl2832 rtl2830 r820t; do
    modprobe -r "$mod" 2>/dev/null || true
done
udevadm control --reload-rules || true
udevadm trigger --subsystem-match=usb || true

echo "[4/8] HackRF tools (INSTALL_HACKRF=$INSTALL_HACKRF)"
if [ "$INSTALL_HACKRF" = "1" ]; then
    # rf-spectrum only ever runs hackrf_sweep (receive-only). Never use hackrf_transfer -t.
    apt-get install -y -qq hackrf >/dev/null
    echo "  hackrf installed: $(command -v hackrf_sweep || echo 'hackrf_sweep missing?')"
else
    echo "  skipped (set INSTALL_HACKRF=1 for 2.4 / 5.8 GHz presets)"
fi

echo "[5/8] Service user"
getent group plugdev >/dev/null || groupadd --system plugdev
if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi
usermod -a -G plugdev "$SVC_USER"

echo "[6/8] Code -> $APP_DIR"
install -d -m 755 "$APP_DIR"
rsync -a --delete --exclude .git --exclude .venv --exclude data --exclude '__pycache__' \
    --exclude .pytest_cache "$SRC"/ "$APP_DIR"/
chmod +x "$APP_DIR/deploy/start.sh"
install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR"

echo "[7/8] Python venv"
[ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "[8/8] Config, systemd unit, pi-profiles"
if [ ! -f "$ENV_FILE" ]; then
    install -m 600 -o root -g root "$SRC/deploy/$APP.env.example" "$ENV_FILE"
    echo "  created $ENV_FILE from example"
else
    echo "  kept existing $ENV_FILE"
fi
install -m 644 "$SRC/deploy/$APP.service" /etc/systemd/system/$APP.service
systemctl daemon-reload
if [ -d /opt/pi-profiles/profiles.d ]; then
    install -m 644 "$SRC"/profiles.d/*.conf /opt/pi-profiles/profiles.d/
    echo "  installed pi-profiles profile(s)"
fi

echo ""
echo "=== Install complete (service NOT enabled at boot; pi-profiles starts it) ==="
echo "  Web UI:   http://<pi-ip>:8140"
echo "  Start:    sudo systemctl start $APP     (or: sudo pi-profiles rf-spectrum)"
echo "  Logs:     journalctl -u $APP -f"
echo "  Config:   $ENV_FILE"
echo "  Unplug/replug the SDR (or reboot) once so the blacklist and udev rules apply."
