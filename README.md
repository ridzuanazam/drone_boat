# Drone Boat — Raspberry Pi 5 + AI Camera + EV3 + iPhone 14

> **Full Technical & Setup Guide:** See [DOCUMENTATION.md](./DOCUMENTATION.md) for full architecture diagrams, step-by-step setup commands, hotspot guide, API reference, and troubleshooting.

Wireless FPV boat controller hosted on a **Raspberry Pi 5** with a **Raspberry Pi AI Camera (IMX500)** and a **LEGO EV3 Brick** connected onboard via USB. Open the web cockpit in **iPhone 14 Safari** (touch joystick + photo/video/AI + bulk Save All) or a **laptop browser** (WASD keyboard controls).

## Hardware Architecture
- **iPhone 14 (Safari)** / **Laptop Browser** `<-- Wi-Fi -->` **Raspberry Pi 5 (`pi/app.py`)**
- **Raspberry Pi 5** `<-- CSI Ribbon Cable -->` **Raspberry Pi AI Camera (IMX500)**
- **Raspberry Pi 5** `<-- Mini-USB Cable -->` **LEGO EV3 Brick (`ev3/main.py`)**
  - **Port A**: Right Large Motor
  - **Port B**: Left Large Motor

---

## Step-by-Step Setup Guide

### Step 1: Upload `ev3/main.py` to the LEGO EV3 Brick
1. Plug your **EV3 Brick** into your laptop using the mini-USB cable and turn the EV3 on.
2. **Option A — Using VS Code (`ev3dev-browser` extension):**
   - Open the **EV3DEV DEVICE BROWSER** panel at the bottom of the Explorer sidebar.
   - Click **"Click here to connect to a device"** and select **`ev3dev`**.
   - Press **`F5`** (or click the **Send workspace to device** icon) to upload `ev3/main.py` to `/home/robot/` on the EV3.
3. **Option B — Using Terminal (`scp`):**
   ```powershell
   ssh robot@ev3dev.local "mkdir -p /home/robot/drone_boat/ev3"
   scp ev3/main.py robot@ev3dev.local:/home/robot/drone_boat/ev3/main.py
   ```
   *(Default EV3 password is `maker`.)*
4. Unplug the EV3 USB cable from your laptop and **plug it into a USB-A port on the Raspberry Pi 5**.
5. Make sure your two Large Motors are plugged into **Port A** (Right) and **Port B** (Left).
6. On the EV3 Brick screen:
   - Go to **File Browser** → open **`drone_boat/ev3/`** (or **`ev3_project/`**) → select **`main.py`** and press the **Center button**.
   - The EV3 will **beep once**, auto-configure its USB IPv4 address (`169.254.201.2`), and display:
     ```text
     Boat EV3 Ready
     169.254.201.2
     Port: 5555
     ```
   - *(Tip: Whenever you update `ev3/main.py` in Git, remember to re-upload `ev3/main.py` to the EV3 Brick via VS Code `F5`, not just `git pull` on the Raspberry Pi.)*

---

### Step 2: Set Up the Raspberry Pi 5 & AI Camera
1. With the Raspberry Pi 5 powered off, connect the **Raspberry Pi AI Camera (IMX500)** ribbon cable to a `CAM/DISP` CSI port on the Pi 5, then power on the Pi 5.
2. Install the required Raspberry Pi OS system packages:
   ```bash
   sudo apt update
   sudo apt install -y python3-picamera2 python3-opencv imx500-all ffmpeg git
   sudo reboot
   ```
3. Verify the AI Camera is detected by the Raspberry Pi 5:
   ```bash
   rpicam-hello --list-cameras
   ```
   *(You should see `imx500` listed. If it says `No cameras available!`, power off the Pi and reseat the CSI ribbon cable.)*
4. Clone this repository onto your Raspberry Pi 5:
   ```bash
   cd ~
   git clone https://github.com/ridzuanazam/drone_boat.git
   ```

---

### Step 3: Turn On the Raspberry Pi 5 Wi-Fi Hotspot (For iPhone Outdoors)
To connect your iPhone 14 directly to the Raspberry Pi 5 without needing an external Wi-Fi router:

```bash
sudo nmcli device wifi hotspot ifname wlan0 ssid DroneBoat password "droneboat"
```
- The Raspberry Pi's IP address on the hotspot is always **`10.42.0.1`**.
- *(Optional)* To make the hotspot start automatically on boot:
  ```bash
  sudo nmcli connection modify Hotspot connection.autoconnect yes
  ```
- To turn off the hotspot later and reconnect to normal Wi-Fi:
  ```bash
  sudo nmcli connection down Hotspot
  sudo nmcli device wifi connect "YOUR_WIFI_NAME" password "YOUR_WIFI_PASSWORD"
  ```

---

### Step 4: Start the Server on the Raspberry Pi 5
Always run `app.py` with the Raspberry Pi's **system Python** (`/usr/bin/python3`, **not** inside a virtualenv, so it has access to system `picamera2` and `imx500`):

```bash
cd ~/drone_boat/pi
pkill -f "python3 app.py" 2>/dev/null || true
/usr/bin/python3 app.py
```
- The terminal will print:
  ```text
  Drone Boat Cockpit listening on http://10.42.0.1:5000
  Connected to EV3 at 169.254.201.2:5555
  ```
- Within 1–2 seconds, the EV3 screen will print **`Pi Connected`**.

---

### Step 5: Connect Your iPhone 14 & Control the Boat
1. On your **iPhone 14**, open **Settings → Wi-Fi** and join **`DroneBoat`** (password: `droneboat`).
   - If iOS asks *"Wi-Fi Network 'DroneBoat' Does Not Appear to Be Connected to the Internet"*, tap **Keep Trying Wi-Fi** (or **Use Without Internet**).
2. Open **Safari** and go to:
   - **`http://10.42.0.1:5000`** *(when connected to the Pi's `DroneBoat` hotspot)*
   - Or **`http://<pi-ip-address>:5000`** *(if both Pi and iPhone are on the same home Wi-Fi)*
3. Check the top-left status pill: it will show a green dot and **`EV3`**.
4. **Drive:** Drag the bottom-left virtual joystick to steer the EV3 motors in real time.
5. **Save All Photos & Videos to iPhone:**
   - Tap **Gallery** (top-right).
   - Tap **`Select All`** (or **`Select`** to pick individual items).
   - Tap **`Save to iPhone`** to open the native iOS Share Sheet and save all selected photos/videos directly to your iPhone **Photos** app at once, or tap **`Download ZIP`** to download them as a single `.zip` archive.

---

## Controls Summary
| Action | iPhone 14 Touch | Laptop Keyboard |
| :--- | :--- | :--- |
| Drive & Steer | Drag bottom-left joystick (release stops) | Hold `W` / `A` / `S` / `D` |
| Emergency Stop | Release joystick or tap **STOP** | `Space` |
| Take Photo | White Shutter button | `P` |
| Record / Stop Video | Red REC button | `V` |
| Toggle AI Detection | `AI` pill (top-right) | `I` |
| Open Gallery / Save All | `Gallery` pill → `Select All` → `Save to iPhone` | `G` |
