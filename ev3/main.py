#!/usr/bin/env pybricks-micropython
"""EV3 motor TCP server for the Drone Boat (Port A=Right, Port B=Left, silent)."""

try:
    import uos as os
except ImportError:
    import os

try:
    import usocket as socket
except ImportError:
    import socket

try:
    import uselect as select
except ImportError:
    import select

from pybricks.hubs import EV3Brick
from pybricks.ev3devices import Motor
from pybricks.parameters import Port, Direction

# --- Configuration ---
HOST = "0.0.0.0"
PORT = 5555
DISCOVERY_PORT = 5556
DISCOVERY_REQUEST = b"EV3_BOAT_DISCOVER"
DISCOVERY_REPLY_PREFIX = "EV3_BOAT_HERE:"
MAX_SPEED = 900            # deg/s cap for EV3 Large Motors
MOTOR_DIRECTION = Direction.CLOCKWISE  # Flip to Direction.COUNTERCLOCKWISE if reversed
HEARTBEAT_TIMEOUT_MS = 2000
ACCEPT_POLL_MS = 500


def clamp_speed(value):
    """Clamp motor speed integer to [-MAX_SPEED, MAX_SPEED]."""
    if value > MAX_SPEED:
        return MAX_SPEED
    if value < -MAX_SPEED:
        return -MAX_SPEED
    return value


def parse_command_line(raw_line):
    """Parse one ASCII command line into (action, left, right) or None."""
    line = raw_line.strip()
    if not line:
        return None
    if line == "S":
        return ("stop", 0, 0)
    if line == "H":
        return ("heartbeat", 0, 0)
    if line == "Q":
        return ("quit", 0, 0)
    if line.startswith("M "):
        parts = line.split()
        if len(parts) != 3:
            return None
        try:
            left = clamp_speed(int(parts[1]))
            right = clamp_speed(int(parts[2]))
        except ValueError:
            return None
        if left == 0 and right == 0:
            return ("stop", 0, 0)
        return ("move", left, right)
    return None


def _is_valid_ipv4(text):
    """Return True if text is a valid dotted-quad IPv4 address."""
    parts = text.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part or not part.isdigit():
            return False
        val = int(part)
        if val < 0 or val > 255:
            return False
    return True


def _ensure_ev3_usb_ipv4():
    """Ensure EV3 usb0 has a link-local IPv4 address (169.254.201.2) when plugged into Pi."""
    ips = get_local_ips()
    if any(ip.startswith("169.254.") for ip in ips):
        return
    for cmd in (
        "ip link set usb0 up 2>/dev/null",
        "ip addr add 169.254.201.2/16 broadcast 169.254.255.255 dev usb0 2>/dev/null",
        "sudo -n ip link set usb0 up 2>/dev/null",
        "sudo -n ip addr add 169.254.201.2/16 broadcast 169.254.255.255 dev usb0 2>/dev/null",
        "echo maker | sudo -S ip link set usb0 up 2>/dev/null",
        "echo maker | sudo -S ip addr add 169.254.201.2/16 broadcast 169.254.255.255 dev usb0 2>/dev/null",
    ):
        try:
            if hasattr(os, "system"):
                os.system(cmd)
        except Exception:
            pass
        ips = get_local_ips()
        if any(ip.startswith("169.254.") for ip in ips):
            return


def get_local_ips(fib_trie_path="/proc/net/fib_trie"):
    """Parse non-loopback local IPv4 addresses from Linux /proc/net/fib_trie."""
    ips = []
    candidate_ip = None
    try:
        with open(fib_trie_path, "r") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if line.startswith("|-- "):
                    parts = line.split()
                    ip_token = parts[1] if len(parts) > 1 else ""
                    candidate_ip = ip_token if _is_valid_ipv4(ip_token) else None
                    if (
                        candidate_ip is not None
                        and "/32 host LOCAL" in line
                        and not candidate_ip.startswith("127.")
                    ):
                        if candidate_ip not in ips:
                            ips.append(candidate_ip)
                        candidate_ip = None
                elif line.startswith("+-- "):
                    candidate_ip = None
                elif "/32 host LOCAL" in line and candidate_ip is not None:
                    if not candidate_ip.startswith("127.") and candidate_ip not in ips:
                        ips.append(candidate_ip)
                    candidate_ip = None
    except OSError:
        return []
    return ips


def make_multi_poller(sockets):
    """Return wait_ready(timeout_ms) returning ready sockets via poll() or select()."""
    if hasattr(select, "poll"):
        poller = select.poll()
        fd_map = {}
        for sock in sockets:
            poller.register(sock, select.POLLIN)
            if hasattr(sock, "fileno"):
                try:
                    fd_map[sock.fileno()] = sock
                except Exception:
                    pass

        def wait_ready_poll(timeout_ms):
            events = poller.poll(timeout_ms)
            ready = []
            for obj, _event in events:
                if isinstance(obj, int):
                    mapped = fd_map.get(obj)
                    if mapped is not None:
                        ready.append(mapped)
                else:
                    ready.append(obj)
            return ready

        return wait_ready_poll

    def wait_ready_select(timeout_ms):
        readable, _, _ = select.select(sockets, [], [], timeout_ms / 1000.0)
        return readable

    return wait_ready_select


def make_poller(sock):
    """Return a callable wait_readable(timeout_ms) for a single socket."""
    wait_multi = make_multi_poller([sock])

    def wait_readable(timeout_ms):
        return bool(wait_multi(timeout_ms))

    return wait_readable


class MotorController:
    """Hot-plugging wrapper for EV3 Large Motors (Port A=Right, Port B=Left)."""

    def __init__(self, direction=MOTOR_DIRECTION):
        self.direction = direction
        self.right_motor = None
        self.left_motor = None
        self.refresh()

    def refresh(self):
        """Try to initialize any missing motor ports without crashing."""
        if self.right_motor is None:
            try:
                self.right_motor = Motor(Port.A, positive_direction=self.direction)
            except Exception:
                self.right_motor = None
        if self.left_motor is None:
            try:
                self.left_motor = Motor(Port.B, positive_direction=self.direction)
            except Exception:
                self.left_motor = None
        return self.missing_ports()

    def missing_ports(self):
        missing = []
        if self.right_motor is None:
            missing.append("A")
        if self.left_motor is None:
            missing.append("B")
        return missing

    def stop(self):
        """Brake both drive motors immediately."""
        for attr in ("left_motor", "right_motor"):
            motor = getattr(self, attr)
            if motor is not None:
                try:
                    motor.brake()
                except Exception:
                    setattr(self, attr, None)

    def apply_speeds(self, left, right):
        """Set left (Port B) and right (Port A) motor speeds, hot-reconnecting if needed."""
        if left == 0 and right == 0:
            self.stop()
            return
        if self.left_motor is None or self.right_motor is None:
            self.refresh()
        if self.left_motor is not None:
            try:
                self.left_motor.run(left)
            except Exception:
                self.left_motor = None
        if self.right_motor is not None:
            try:
                self.right_motor.run(right)
            except Exception:
                self.right_motor = None


def stop_motors(left_motor, right_motor=None):
    """Brake both drive motors immediately (supports MotorController or raw pair)."""
    if isinstance(left_motor, MotorController):
        left_motor.stop()
        return
    if left_motor is not None:
        left_motor.brake()
    if right_motor is not None:
        right_motor.brake()


def apply_motor_speeds(left, right, left_motor, right_motor=None):
    """Set left (Port B) and right (Port A) motor speeds, or brake if both 0."""
    if isinstance(left_motor, MotorController):
        left_motor.apply_speeds(left, right)
        return
    if left == 0 and right == 0:
        stop_motors(left_motor, right_motor)
    else:
        if left_motor is not None:
            left_motor.run(left)
        if right_motor is not None:
            right_motor.run(right)


def handle_client(client, left_motor, right_motor, ev3=None):
    """Process line commands from the Pi 5 bridge until disconnect, timeout, or Q."""
    wait_readable = make_poller(client)
    recv_buf = b""
    last_speeds = (0, 0)

    try:
        while True:
            try:
                ready = wait_readable(HEARTBEAT_TIMEOUT_MS)
            except OSError:
                stop_motors(left_motor, right_motor)
                return False

            if not ready:
                print("Client heartbeat timeout; braking motors.")
                stop_motors(left_motor, right_motor)
                return False

            try:
                chunk = client.recv(256)
            except OSError:
                stop_motors(left_motor, right_motor)
                return False

            if not chunk:
                stop_motors(left_motor, right_motor)
                return False

            recv_buf = recv_buf + chunk
            if b"\n" not in recv_buf:
                if len(recv_buf) > 512:
                    recv_buf = b""
                continue

            parts = recv_buf.split(b"\n")
            recv_buf = parts[-1]
            lines = parts[:-1]

            pending_action = None
            for raw_bytes in lines:
                try:
                    text = raw_bytes.decode()
                except Exception:
                    continue
                parsed = parse_command_line(text)
                if parsed is None:
                    continue
                if parsed[0] == "quit":
                    stop_motors(left_motor, right_motor)
                    return True
                if parsed[0] in ("move", "stop"):
                    pending_action = parsed

            if pending_action is not None:
                action, left, right = pending_action
                if action == "stop":
                    stop_motors(left_motor, right_motor)
                    last_speeds = (0, 0)
                elif (left, right) != last_speeds:
                    apply_motor_speeds(left, right, left_motor, right_motor)
                    last_speeds = (left, right)
    finally:
        stop_motors(left_motor, right_motor)
        if ev3 is not None:
            ev3.screen.print("Stopped")
        try:
            client.close()
        except OSError:
            pass


def _bind_discovery_socket(host, discovery_port):
    """Best-effort bind of a UDP discovery responder socket, or None on error."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        addr = socket.getaddrinfo(host, discovery_port)[0][-1]
        sock.bind(addr)
        return sock
    except OSError:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        return None


def _handle_discovery_packet(discovery_sock, actual_port):
    """Respond to an EV3_BOAT_DISCOVER UDP probe with the active TCP port."""
    try:
        msg, peer = discovery_sock.recvfrom(64)
    except OSError:
        return
    if msg.strip() == DISCOVERY_REQUEST:
        reply = "{0}{1}".format(DISCOVERY_REPLY_PREFIX, actual_port).encode()
        try:
            discovery_sock.sendto(reply, peer)
        except OSError:
            pass


def run_server(
    host=HOST,
    port=PORT,
    discovery_port=DISCOVERY_PORT,
    on_listen=None,
    on_discovery_port=None,
):
    """Initialize EV3 motors (Port A=Right, Port B=Left) and run silent TCP server."""
    ev3 = EV3Brick()
    _ensure_ev3_usb_ipv4()
    motors = MotorController(direction=MOTOR_DIRECTION)
    missing_ports = motors.missing_ports()
    motors.stop()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    addr = socket.getaddrinfo(host, port)[0][-1]
    server.bind(addr)
    server.listen(1)
    actual_port = port if port else server.getsockname()[1]

    server6 = None
    if hasattr(socket, "AF_INET6"):
        try:
            server6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            server6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                server6.setsockopt(41, 26, 1)  # IPPROTO_IPV6=41, IPV6_V6ONLY=26 on Linux
            except Exception:
                pass
            try:
                addr6 = socket.getaddrinfo("::", actual_port, socket.AF_INET6)[0][-1]
            except Exception:
                addr6 = socket.getaddrinfo("::", actual_port)[0][-1]
            server6.bind(addr6)
            server6.listen(1)
        except Exception as exc:
            print("IPv6 bind note:", exc)
            if server6 is not None:
                try:
                    server6.close()
                except Exception:
                    pass
                server6 = None

    discovery_sock = _bind_discovery_socket(host, discovery_port)
    listen_socks = [server]
    if server6 is not None:
        listen_socks.append(server6)
    if discovery_sock is not None:
        listen_socks.append(discovery_sock)
        if on_discovery_port is not None:
            actual_disc = (
                discovery_port
                if discovery_port
                else discovery_sock.getsockname()[1]
            )
            on_discovery_port(actual_disc)

    wait_ready = make_multi_poller(listen_socks)
    should_exit = False

    local_ips = get_local_ips()
    ev3.speaker.beep()
    ev3.screen.clear()
    ev3.screen.print("Boat EV3 Ready")
    if missing_ports:
        ev3.screen.print("No Motor: " + ",".join(missing_ports))
    for ip_addr in local_ips[:2]:
        ev3.screen.print(ip_addr)
    ev3.screen.print("Port: {0}".format(actual_port))
    if local_ips:
        print("Boat EV3 IPs: {0}".format(", ".join(local_ips)))
    print("Boat EV3 listening on {0}:{1}".format(host, actual_port))

    if on_listen is not None:
        on_listen(actual_port)

    try:
        while not should_exit:
            ready_socks = wait_ready(ACCEPT_POLL_MS)
            if not ready_socks:
                continue
            if discovery_sock is not None and discovery_sock in ready_socks:
                _handle_discovery_packet(discovery_sock, actual_port)
            for srv_sock in (server, server6):
                if srv_sock is not None and srv_sock in ready_socks:
                    client, client_addr = srv_sock.accept()
                    print("Pi connected:", client_addr)
                    motors.refresh()
                    ev3.screen.print("Pi Connected")
                    should_exit = handle_client(client, motors, None, ev3)
                    print("Pi disconnected:", client_addr)
                    if not should_exit:
                        ev3.screen.print("Waiting...")
    except KeyboardInterrupt:
        print("Server interrupted by user.")
    finally:
        motors.stop()
        if discovery_sock is not None:
            try:
                discovery_sock.close()
            except OSError:
                pass
        if server6 is not None:
            try:
                server6.close()
            except OSError:
                pass
        try:
            server.close()
        except OSError:
            pass


if __name__ == "__main__":
    run_server()
