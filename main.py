"""Tkinter desktop app for controlling a BenQ projector (e.g. MX528) over RS232.

Run with:  python main.py
"""

from __future__ import annotations

import queue
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from projector_controller import (
    BAUD_RATES,
    DEFAULT_BAUD_RATE,
    POWER_OFF,
    POWER_ON,
    POWER_STATUS,
    SERIAL_FORMAT_DESCRIPTION,
    BusyEvent,
    ConnectionChange,
    ConnectionEvent,
    LogEvent,
    PowerState,
    PowerStatusEvent,
    ProjectorController,
    SerialTransport,
    SimulatedTransport,
    SimulationScenario,
    list_serial_ports,
)

APP_TITLE = "BenQ Projector RS232 Control"
POLL_INTERVAL_MS = 50

COLOR_OK = "#1e8e3e"
COLOR_OFF = "#5f6368"
COLOR_WARN = "#e37400"
COLOR_ERROR = "#d93025"
COLOR_SIM = "#8e24aa"

LOG_COLORS = {
    "TX": "#1a73e8",
    "RX": "#188038",
    "INFO": "#3c4043",
    "WARN": "#b06000",
    "ERROR": "#d93025",
}


class ProjectorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.controller = ProjectorController()
        self.connected = False
        self.busy = False
        self.connected_simulated = False
        self.port_devices: dict[str, str] = {}

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD_RATE))
        self.simulate_var = tk.BooleanVar(value=False)
        self.scenario_var = tk.StringVar(value=SimulationScenario.NORMAL.value)
        self.activity_var = tk.StringVar(value="Idle")

        root.title(APP_TITLE)
        root.minsize(720, 640)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build_styles()
        self._build_ui()

        self.refresh_ports()
        self._set_connection_indicator("Not connected", COLOR_OFF)
        self._set_power_indicator("Unknown", COLOR_OFF, "Connect, then use Check Power Status.")
        self.update_controls()
        self.root.after(POLL_INTERVAL_MS, self.process_events)

    # -- layout -------------------------------------------------------------

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names() and self.root.tk.call("tk", "windowingsystem") == "x11":
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("TkDefaultFont", 15, "bold"))
        style.configure("Hint.TLabel", foreground="#5f6368")
        style.configure("Indicator.TLabel", font=("TkDefaultFont", 11, "bold"))
        style.configure("Power.TButton", padding=(14, 8))
        style.configure("Sim.TLabel", foreground="white", background=COLOR_SIM, padding=6)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(5, weight=1)

        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")

        self.sim_banner = ttk.Label(
            outer,
            text="SIMULATION MODE — no hardware. All replies and power states "
            "shown are simulated, not real projector readings.",
            style="Sim.TLabel",
        )
        self.sim_banner.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.sim_banner.grid_remove()

        self._build_connection_panel(outer).grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._build_status_panel(outer).grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self._build_power_panel(outer).grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self._build_log_panel(outer).grid(row=5, column=0, sticky="nsew", pady=(10, 0))

    def _build_connection_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="Connection", padding=10)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Serial port:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.port_combo = ttk.Combobox(frame, textvariable=self.port_var, width=48)
        self.port_combo.grid(row=0, column=1, sticky="ew")
        self.refresh_button = ttk.Button(frame, text="Refresh Ports", command=self.refresh_ports)
        self.refresh_button.grid(row=0, column=2, padx=(8, 0))

        ttk.Label(frame, text="Baud rate:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        baud_row = ttk.Frame(frame)
        baud_row.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        self.baud_combo = ttk.Combobox(
            baud_row,
            textvariable=self.baud_var,
            values=[str(rate) for rate in BAUD_RATES],
            state="readonly",
            width=10,
        )
        self.baud_combo.pack(side="left")
        ttk.Label(
            baud_row,
            text="Must match the Baud Rate setting in the projector's menu.",
            style="Hint.TLabel",
        ).pack(side="left", padx=(10, 0))

        ttk.Label(
            frame, text=f"Format: {SERIAL_FORMAT_DESCRIPTION}.", style="Hint.TLabel"
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(4, 0))

        sim_row = ttk.Frame(frame)
        sim_row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.simulate_check = ttk.Checkbutton(
            sim_row,
            text="Simulation mode (no hardware)",
            variable=self.simulate_var,
            command=self.update_controls,
        )
        self.simulate_check.pack(side="left")
        ttk.Label(sim_row, text="Scenario:").pack(side="left", padx=(16, 6))
        self.scenario_combo = ttk.Combobox(
            sim_row,
            textvariable=self.scenario_var,
            values=[s.value for s in SimulationScenario],
            state="readonly",
            width=28,
        )
        self.scenario_combo.pack(side="left")

        button_row = ttk.Frame(frame)
        button_row.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.connect_button = ttk.Button(button_row, text="Connect", command=self.on_connect)
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(button_row, text="Disconnect", command=self.on_disconnect)
        self.disconnect_button.pack(side="left", padx=(8, 0))
        return frame

    def _build_status_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="Status", padding=10)
        frame.columnconfigure(2, weight=1)

        ttk.Label(frame, text="Serial connection:").grid(row=0, column=0, sticky="w")
        self.conn_dot = ttk.Label(frame, text="●", style="Indicator.TLabel")
        self.conn_dot.grid(row=0, column=1, padx=(8, 4))
        self.conn_text = ttk.Label(frame, style="Indicator.TLabel")
        self.conn_text.grid(row=0, column=2, sticky="w")

        ttk.Label(frame, text="Projector power:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.power_dot = ttk.Label(frame, text="●", style="Indicator.TLabel")
        self.power_dot.grid(row=1, column=1, padx=(8, 4), pady=(6, 0))
        self.power_text = ttk.Label(frame, style="Indicator.TLabel")
        self.power_text.grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.power_detail = ttk.Label(frame, style="Hint.TLabel", wraplength=620, justify="left")
        self.power_detail.grid(row=2, column=2, sticky="w")

        ttk.Label(frame, text="Activity:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Label(frame, textvariable=self.activity_var).grid(row=3, column=2, sticky="w", pady=(6, 0))
        return frame

    def _build_power_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="Projector power", padding=10)
        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.status_button = ttk.Button(
            row, text="Check Power Status", style="Power.TButton", command=self.on_query
        )
        self.status_button.pack(side="left")
        self.on_button = ttk.Button(row, text="Turn On", style="Power.TButton", command=self.on_power_on)
        self.on_button.pack(side="left", padx=(8, 0))
        self.off_button = ttk.Button(
            row, text="Turn Off (Standby)", style="Power.TButton", command=self.on_power_off
        )
        self.off_button.pack(side="left", padx=(8, 0))

        ttk.Label(
            frame,
            text=(
                "Turn Off sends the projector's normal shutdown command. The projector "
                "goes into standby and its cooling fan may keep running for a while — "
                "leave mains power connected until cooling finishes. Check Power Status "
                "is read-only and is the safest first test. Closing this app never turns "
                "the projector off."
            ),
            style="Hint.TLabel",
            wraplength=680,
            justify="left",
        ).pack(fill="x", pady=(8, 0))
        return frame

    def _build_log_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="Communication log", padding=10)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(frame, height=14, wrap="word", state="disabled", font=("TkFixedFont", 10))
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        for category, color in LOG_COLORS.items():
            self.log_text.tag_configure(category, foreground=color)
        self.log_text.tag_configure("SIM", foreground=COLOR_SIM)

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Clear Log", command=self.clear_log).pack(side="left")
        ttk.Button(buttons, text="Save Log…", command=self.save_log).pack(side="left", padx=(8, 0))
        return frame

    # -- control state ------------------------------------------------------

    def update_controls(self) -> None:
        idle_disconnected = not self.connected and not self.busy
        simulate = self.simulate_var.get()

        def enable(widget, on: bool, on_state: str = "normal") -> None:
            widget.configure(state=on_state if on else "disabled")

        enable(self.port_combo, idle_disconnected and not simulate)
        enable(self.refresh_button, idle_disconnected and not simulate)
        enable(self.baud_combo, idle_disconnected and not simulate, "readonly")
        enable(self.simulate_check, idle_disconnected)
        enable(self.scenario_combo, idle_disconnected and simulate, "readonly")
        enable(self.connect_button, idle_disconnected)
        enable(self.disconnect_button, self.connected)
        for button in (self.status_button, self.on_button, self.off_button):
            enable(button, self.connected and not self.busy)

    def _mark_busy(self, activity: str) -> None:
        # Disable buttons immediately so a second click cannot queue a command.
        self.busy = True
        self.activity_var.set(activity + "…")
        self.update_controls()

    # -- actions ------------------------------------------------------------

    def refresh_ports(self) -> None:
        try:
            ports = list_serial_ports()
        except Exception as exc:
            self.port_devices = {}
            self.port_combo.configure(values=[])
            self.append_log("ERROR", f"Could not list serial ports: {exc}", simulated=False)
            return
        self.port_devices = {port.label: port.device for port in ports}
        labels = list(self.port_devices)
        self.port_combo.configure(values=labels)
        if self.port_var.get() not in labels and self.port_var.get() not in self.port_devices.values():
            self.port_var.set(labels[0] if labels else "")
        if labels:
            self.append_log("INFO", f"Found {len(labels)} serial port(s): " + "; ".join(labels), False)
        else:
            self.append_log(
                "WARN",
                "No serial ports found. Plug in the USB-to-RS232 adapter (install its "
                "driver if needed) and click Refresh Ports, or type a port path.",
                False,
            )

    def _selected_device(self) -> str:
        text = self.port_var.get().strip()
        return self.port_devices.get(text, text)

    def on_connect(self) -> None:
        if self.simulate_var.get():
            scenario = SimulationScenario(self.scenario_var.get())
            transport = SimulatedTransport(scenario)
        else:
            device = self._selected_device()
            if not device:
                messagebox.showwarning(APP_TITLE, "Select or type a serial port first.", parent=self.root)
                return
            transport = SerialTransport(device, int(self.baud_var.get()))
        self._mark_busy("Connecting")
        if not self.controller.connect(transport):
            self.append_log("WARN", "Busy - please wait for the current operation.", False)

    def on_disconnect(self) -> None:
        self.disconnect_button.configure(state="disabled")
        self.controller.disconnect()

    def on_query(self) -> None:
        self._send(self.controller.query_power, f"Checking power status ({POWER_STATUS.text})")

    def on_power_on(self) -> None:
        self._send(self.controller.power_on, f"Turning on ({POWER_ON.text})")

    def on_power_off(self) -> None:
        confirmed = messagebox.askyesno(
            APP_TITLE,
            "Send the shutdown command to the projector?\n\n"
            "It will go into standby. The cooling fan may keep running for a "
            "while; do not unplug mains power during cooling.",
            parent=self.root,
        )
        if confirmed:
            self._send(self.controller.power_off, f"Turning off ({POWER_OFF.text})")

    def _send(self, submit, activity: str) -> None:
        if not self.connected or self.busy:
            return
        self._mark_busy(activity)
        if not submit():
            self.append_log("WARN", "Busy - please wait for the current operation.", False)

    # -- events from the worker -----------------------------------------------

    def process_events(self) -> None:
        try:
            for _ in range(200):
                event = self.controller.events.get_nowait()
                self.handle_event(event)
        except queue.Empty:
            pass
        self.root.after(POLL_INTERVAL_MS, self.process_events)

    def handle_event(self, event: object) -> None:
        if isinstance(event, LogEvent):
            self.append_log(event.category, event.text, event.simulated, event.timestamp)
        elif isinstance(event, BusyEvent):
            self.busy = event.busy
            self.activity_var.set(event.activity + "…" if event.busy else "Idle")
            self.update_controls()
        elif isinstance(event, ConnectionEvent):
            self._handle_connection(event)
        elif isinstance(event, PowerStatusEvent):
            self._handle_power(event)

    def _handle_connection(self, event: ConnectionEvent) -> None:
        self.connected = event.connected
        self.connected_simulated = event.simulated and event.connected
        if self.connected_simulated:
            self.sim_banner.grid()
        else:
            self.sim_banner.grid_remove()

        if event.connected:
            text = "Simulated port connected (no hardware)" if event.simulated else "Serial port connected"
            self._set_connection_indicator(f"{text} — {event.description}", COLOR_SIM if event.simulated else COLOR_OK)
        elif event.change is ConnectionChange.CONNECT_FAILED:
            self._set_connection_indicator("Not connected (open failed)", COLOR_ERROR)
            messagebox.showerror(APP_TITLE, event.error or "Could not open the port.", parent=self.root)
        elif event.change is ConnectionChange.LOST:
            self._set_connection_indicator("Connection lost", COLOR_ERROR)
            self.refresh_ports()
        else:
            self._set_connection_indicator("Not connected", COLOR_OFF)
        self.update_controls()

    def _handle_power(self, event: PowerStatusEvent) -> None:
        suffix = " — SIMULATED" if event.simulated and self.connected else ""
        if event.state is PowerState.ON and event.confirmed:
            self._set_power_indicator("ON (confirmed by reply)" + suffix, COLOR_OK, event.detail)
        elif event.state is PowerState.OFF and event.confirmed:
            self._set_power_indicator(
                "OFF / standby (confirmed by reply)" + suffix,
                COLOR_OFF,
                event.detail + " The cooling fan may still be running.",
            )
        else:
            color = COLOR_WARN if self.connected else COLOR_OFF
            label = "No response / status unknown" if "No response" in event.detail else "Unknown"
            self._set_power_indicator(label + suffix, color, event.detail)

    def _set_connection_indicator(self, text: str, color: str) -> None:
        self.conn_dot.configure(foreground=color)
        self.conn_text.configure(text=text, foreground=color)

    def _set_power_indicator(self, text: str, color: str, detail: str) -> None:
        self.power_dot.configure(foreground=color)
        self.power_text.configure(text=text, foreground=color)
        self.power_detail.configure(text=detail)

    # -- log --------------------------------------------------------------------

    def append_log(self, category: str, text: str, simulated: bool, timestamp: datetime | None = None) -> None:
        stamp = (timestamp or datetime.now()).strftime("%H:%M:%S.%f")[:-3]
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp} ")
        if simulated:
            self.log_text.insert("end", "[SIM] ", "SIM")
        self.log_text.insert("end", f"{category:<5} {text}\n", category)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save communication log",
            defaultextension=".txt",
            initialfile=f"benq-log-{datetime.now():%Y%m%d-%H%M%S}.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.log_text.get("1.0", "end-1c"))
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not save the log:\n{exc}", parent=self.root)
            return
        self.append_log("INFO", f"Log saved to {path}", False)

    # -- lifecycle --------------------------------------------------------------

    def on_close(self) -> None:
        # Stops the worker and closes the port. No power command is sent.
        self.controller.shutdown(timeout=3.0)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ProjectorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
