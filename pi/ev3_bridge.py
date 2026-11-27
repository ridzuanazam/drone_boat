"""EV3 wired-USB TCP bridge with differential joystick mixing and failsafe watchdog."""

import math
import os
import re
import select
import socket
import subprocess
import threading
import time

DISCOVERY_REQUEST = b"EV3_BOAT_DISCOVER"
DISCOVERY_REPLY_PREFIX = b"EV3_BOAT_HERE:"
IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def mix_joystick(x, y, max_speed=900, deadzone=0.08):
    """Convert normalized joystick (x=turn, y=forward) to (left, right) motor speeds."""
    x = max(-1.0, min(1.0, float(x)))
    y = max(-1.0, min(1.0, float(y)))
    if math.hypot(x, y) < deadzone:
        return (0, 0)

    raw_left = y + x
    raw_right = y - x
    scale = max(1.0, abs(raw_left), abs(raw_right))
    left = int(round((raw_left / scale) * max_speed))
    right = int(round((raw_right / scale) * max_speed))
    return (left, right)


def _get_local_ipv4s():
    """Return deduplicated local non-loopback IPv4 addresses across all interfaces."""
    ips = []

    # 1. Linux /proc/net/fib_trie (discovers USB gadget / link-local 169.254.x.x interfaces on Pi)
    if os.path.isfile("/proc/net/fib_trie"):
        candidate_ip = None
        try:
            with open("/proc/net/fib_trie", "r") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if line.startswith("|-- "):
                        parts = line.split()
                        token = parts[1] if len(parts) > 1 else ""
                        candidate_ip = token if IPV4_RE.fullmatch(token) else None
                        if (
                            candidate_ip
                            and "/32 host LOCAL" in line
                            and not candidate_ip.startswith("127.")
                            and candidate_ip not in ips
                        ):
                            ips.append(candidate_ip)
                            candidate_ip = None
                    elif line.startswith("+-- "):
                        candidate_ip = None
                    elif "/32 host LOCAL" in line and candidate_ip:
                        if not candidate_ip.startswith("127.") and candidate_ip not in ips:
                            ips.append(candidate_ip)
                        candidate_ip = None
        except OSError:
            pass

    # 2. Hostname resolution fallback (Windows / macOS / Linux)
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in infos:
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass

    return ips


def _get_arp_neighbor_ips():
    """Return candidate neighbor IPv4 addresses (including 169.254.x.x USB EV3 links)."""
    neighbors = []
    if os.path.isfile("/proc/net/arp"):
        try:
            with open("/proc/net/arp", "r") as handle:
                for raw_line in handle.readlines()[1:]:
                    parts = raw_line.split()
                    if len(parts) >= 4 and IPV4_RE.fullmatch(parts[0]):
                        ip = parts[0]
                        if ip not in neighbors and not ip.startswith("127."):
                            neighbors.append(ip)
        except OSError:
            pass
    return neighbors


def _subnet_broadcast(local_ip):
    """Return the subnet broadcast IPv4 address for a local interface address."""
    octets = local_ip.split(".")
    if octets[0] == "169" and octets[1] == "254":
        return "169.254.255.255"
    return "{0}.{1}.{2}.255".format(octets[0], octets[1], octets[2])


def discover_ev3_udp(discovery_port=5556, timeout_s=0.6, target_hosts=None):
    """Broadcast EV3_BOAT_DISCOVER via UDP and return (ip, tcp_port) or None."""
    sockets = []
    try:
        for local_ip in _get_local_ipv4s():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.bind((local_ip, 0))
                sockets.append(sock)
            except OSError:
                sock.close()
                continue
            for bcast in ("255.255.255.255", _subnet_broadcast(local_ip)):
                try:
                    sock.sendto(DISCOVERY_REQUEST, (bcast, discovery_port))
                except OSError:
                    pass

        fallback = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            fallback.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            fallback.bind(("", 0))
            sockets.append(fallback)
            targets = ["255.255.255.255", "169.254.255.255", "10.42.0.255"]
            if target_hosts:
                targets.extend(target_hosts)
            for neighbor_ip in _get_arp_neighbor_ips():
                if neighbor_ip not in targets:
                    targets.append(neighbor_ip)
            for target in targets:
                try:
                    fallback.sendto(DISCOVERY_REQUEST, (target, discovery_port))
                except OSError:
                    pass
        except OSError:
            fallback.close()

        deadline = time.monotonic() + timeout_s
        while sockets:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select(sockets, [], [], remaining)
            if not readable:
                break
            for sock in readable:
                try:
                    data, peer = sock.recvfrom(128)
                except OSError:
                    continue
                if data.startswith(DISCOVERY_REPLY_PREFIX):
                    port_bytes = data[len(DISCOVERY_REPLY_PREFIX):].strip()
                    try:
                        reply_port = int(port_bytes)
                        if not (1 <= reply_port <= 65535):
                            reply_port = 5555
                    except ValueError:
                        reply_port = 5555
                    return (peer[0], reply_port)
        return None
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass


class EV3Bridge:
    """Thread-safe auto-reconnecting TCP client and watchdog for the EV3 brick."""

    def __init__(
        self,
        host="ev3dev.local",
        port=5555,
        discovery_port=5556,
        max_speed=900,
        command_timeout_s=1.2,
        discovery_hosts=None,
    ):
        self.host = host
        self.port = port
        self.discovery_port = discovery_port
        self.max_speed = max_speed
        self.command_timeout_s = command_timeout_s
        self._discovery_hosts = discovery_hosts

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sock = None
        self._connected = False
        self._active_host = host
        self._active_port = port
        self._target_speeds = (0, 0)
        self._sent_speeds = None
        self._last_input_ts = time.monotonic()
        self._last_hb_ts = time.monotonic()
        self._worker_thread = None

    def start(self):
        """Start the background connection and failsafe watchdog thread."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

    def close(self):
        """Brake motors, close socket, and stop the background thread."""
        self._stop_event.set()
        with self._lock:
            self._target_speeds = (0, 0)
            if self._sock is not None:
                try:
                    self._sock.sendall(b"S\n")
                except OSError:
                    pass
                self._drop_socket_locked()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=2.0)

    def _clamp(self, value):
        return max(-self.max_speed, min(self.max_speed, int(value)))

    def _drop_socket_locked(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._connected = False
        self._sent_speeds = None

    def _send_line_locked(self, line):
        if self._sock is None:
            return False
        try:
            self._sock.sendall(line.encode("ascii"))
            return True
        except OSError:
            self._drop_socket_locked()
            return False

    def _format_speed_line(self, left, right):
        if left == 0 and right == 0:
            return "S\n"
        return "M {0} {1}\n".format(left, right)

    def set_joystick(self, x, y):
        """Set motor speeds from normalized joystick coordinates (-1..1)."""
        left, right = mix_joystick(x, y, max_speed=self.max_speed)
        return self.set_motors(left, right)

    def set_motors(self, left, right):
        """Set explicit (left, right) motor speeds in deg/s."""
        left = self._clamp(left)
        right = self._clamp(right)
        with self._lock:
            self._target_speeds = (left, right)
            self._last_input_ts = time.monotonic()
            if self._sock is not None and (left, right) != self._sent_speeds:
                if self._send_line_locked(self._format_speed_line(left, right)):
                    self._sent_speeds = (left, right)
                    self._last_hb_ts = time.monotonic()
        return (left, right)

    def stop(self):
        """Immediately brake both motors."""
        with self._lock:
            self._target_speeds = (0, 0)
            self._last_input_ts = time.monotonic()
            if self._sock is not None:
                if self._send_line_locked("S\n"):
                    self._sent_speeds = (0, 0)
                    self._last_hb_ts = time.monotonic()

    def heartbeat(self):
        """Refresh the Pi-side watchdog timestamp while driving."""
        with self._lock:
            self._last_input_ts = time.monotonic()

    def get_status(self):
        """Return a JSON-serializable EV3 connection and thrust status dict."""
        with self._lock:
            left, right = self._target_speeds
            return {
                "connected": self._connected,
                "host": self._active_host,
                "port": self._active_port,
                "left_speed": left,
                "right_speed": right,
                "max_speed": self.max_speed,
            }

    def _try_open_tcp(self, host, port):
        try:
            infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except OSError:
            infos = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))]

        for family, socktype, proto, _canon, sockaddr in infos:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(1.0)
                sock.connect(sockaddr)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(0.5)
                return sock
            except OSError:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
        return None

    def _connect_once(self):
        candidate_hosts = [self.host]
        for extra in ("ev3dev.local", "ev3dev"):
            if extra not in candidate_hosts:
                candidate_hosts.append(extra)
        for neighbor_ip in _get_arp_neighbor_ips():
            if neighbor_ip.startswith("169.254.") and neighbor_ip not in candidate_hosts:
                candidate_hosts.append(neighbor_ip)

        sock = None
        used_host, used_port = self.host, self.port
        for candidate in candidate_hosts:
            sock = self._try_open_tcp(candidate, self.port)
            if sock is not None:
                used_host = candidate
                break

        if sock is None:
            found = discover_ev3_udp(
                discovery_port=self.discovery_port,
                timeout_s=0.6,
                target_hosts=self._discovery_hosts,
            )
            if found is not None:
                used_host, used_port = found
                sock = self._try_open_tcp(used_host, used_port)
        if sock is None:
            return False

        with self._lock:
            self._sock = sock
            self._connected = True
            self._active_host = used_host
            self._active_port = used_port
            left, right = self._target_speeds
            if self._send_line_locked(self._format_speed_line(left, right)):
                self._sent_speeds = (left, right)
                self._last_hb_ts = time.monotonic()
        return True

    def _worker(self):
        while not self._stop_event.is_set():
            now = time.monotonic()
            with self._lock:
                sock_missing = self._sock is None

            if sock_missing:
                if not self._connect_once():
                    self._stop_event.wait(1.5)
                continue

            with self._lock:
                if self._sock is not None:
                    if (
                        self._target_speeds != (0, 0)
                        and now - self._last_input_ts > self.command_timeout_s
                    ):
                        self._target_speeds = (0, 0)
                        if self._send_line_locked("S\n"):
                            self._sent_speeds = (0, 0)
                            self._last_hb_ts = now
                    elif now - self._last_hb_ts >= 0.4:
                        if self._send_line_locked("H\n"):
                            self._last_hb_ts = now

            self._stop_event.wait(0.05)
