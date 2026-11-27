# Drone Boat — Raspberry Pi 5 + AI Camera + EV3 + iPhone 14

> **Full Documentation:** See [DOCUMENTATION.md](./DOCUMENTATION.md) for architecture diagrams, protocol & failsafe details, API reference, configuration variables, systemd auto-start, and troubleshooting.

Wireless FPV boat controller hosted on a **Raspberry Pi 5** with a **Raspberry Pi AI Camera (IMX500)** and a **LEGO EV3 Brick** connected onboard via USB. Open the web cockpit in **iPhone 14 Safari** (touch joystick + photo/video/AI buttons) or a **laptop browser** (WASD keyboard controls).

## Architecture
- **iPhone 14 (Safari)** / **Laptop Browser** <-- *Wi-Fi* --> **Raspberry Pi 5 (`pi/app.py`)**
- **Raspberry Pi 5** <-- *CSI Ribbon* --> **Raspberry Pi AI Camera (IMX500)**
- **Raspberry Pi 5** <-- *Short USB Cable* --> **LEGO EV3 Brick (`ev3/main.py`)**
  - **Port A**: Right Large Motor
  - **Port B**: Left Large Motor

---

## 1. Quick Demo on Your Laptop (Simulated Camera & EV3)
You can test the full UI, joystick, keyboard controls, photo/video capture, and gallery right away on Windows before moving files to the Pi 5 and EV3:

```powershell
cd C:\dev\ws2\drone_boat
$env:MOCK_MODE = "1"
C:\dev\ws2\.venv\Scripts\python.exe pi\app.py
```
Then open `http://localhost:5000` in your browser (or `http://<your-laptop-ip>:5000` from your iPhone 14 on the same Wi-Fi).

---

## 2. EV3 Setup (`ev3/main.py`)
1. Copy `ev3/main.py` to your EV3 brick (`/home/robot/drone_boat/main.py`) or open `ev3/main.py` in VS Code with the EV3 extension (`Download and Run`).
2. Plug the EV3 into any **USB-A port on the Raspberry Pi 5** using the EV3 mini-USB cable.
3. On ev3dev, enable **USB Ethernet / Tethering** (Brickman → **Wireless and Networks → All Network Connections → Wired** or **Tethering**) if needed, then start `ev3/main.py`.
   - The EV3 beeps once (no speech) and shows its IP and `Port: 5555`.
   - If your boat moves backward when pushing forward on the joystick, change `MOTOR_DIRECTION = Direction.COUNTERCLOCKWISE` in `ev3/main.py`.

---

## 3. Raspberry Pi 5 Setup (`pi/`)
1. Connect the **Raspberry Pi AI Camera** to a CSI camera port on the Pi 5.
2. Install Raspberry Pi OS packages (no `pip install` needed):
   ```bash
   sudo apt update
   sudo apt install -y python3-picamera2 python3-opencv imx500-all ffmpeg
   sudo reboot
   ```
3. Copy the `pi/` folder to the Raspberry Pi 5 (for example `/home/pi/drone_boat/pi`).
4. Start the server:
   ```bash
   cd ~/drone_boat/pi
   python3 app.py
   ```
   *(If `ev3dev.local` does not resolve automatically, the Pi auto-discovers the EV3 over UDP, or you can specify `EV3_HOST=<ev3-ip> python3 app.py`.)*

---

## 4. Field Wi-Fi Hotspot (Using iPhone 14 Outdoors Without a Router)
On the Raspberry Pi 5 (NetworkManager), create a local Wi-Fi hotspot named `DroneBoat`:
```bash
sudo nmcli device wifi hotspot ifname wlan0 ssid DroneBoat password "droneboat"
```
1. On your **iPhone 14**, join Wi-Fi **`DroneBoat`**.
2. Open **Safari** and go to **`http://10.42.0.1:5000`**.
3. Tap **Share → Add to Home Screen** in Safari for a full-screen cockpit app!

---

## 5. Controls
| Action | iPhone 14 Touch | Keyboard |
| :--- | :--- | :--- |
| Drive & Steer | Drag bottom-left joystick (release stops) | Hold `W` / `A` / `S` / `D` |
| Emergency Stop | Release joystick or tap **STOP** | `Space` |
| Take Photo | White Shutter button | `P` |
| Record / Stop Video | Red REC button | `V` |
| Toggle AI Detection | `AI` pill (top-right) | `I` |
| View / Save Media | `Gallery` pill (top-right) | `G` |

---

## 6. Boat Hardware Safety Notes
- **Separate power supplies:** Power the **Raspberry Pi 5** from a 5V/5A USB-C power bank (or 5V BEC) and the **EV3** from its own battery pack inside a **waterproof container**.
- **Failsafe:** If the iPhone loses Wi-Fi, the Pi stops the motors after `1.2 s`; if the Pi stops responding, the EV3 brakes both motors after `2.0 s`.
