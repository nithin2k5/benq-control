# benq-control

A small desktop app (Tkinter + pySerial) for controlling a **BenQ MX528** projector
over its RS232 port. It is mainly meant for turning the projector off from the computer,
and it can also turn it on and query its power status.

It sends the standard BenQ ASCII RS232 commands. Each one is framed by a carriage
return before and after:

| Action | Bytes sent |
| --- | --- |
| Power ON | `\r*pow=on#\r` |
| Power OFF (normal shutdown to standby) | `\r*pow=off#\r` |
| Power status query (read-only) | `\r*pow=?#\r` |

> **Compatibility note:** these are BenQ's standard commands. Whether your MX528
> (and its firmware) accepts them must be verified on the actual projector. This project
> has been tested only against simulated transports, not against real MX528 hardware.

## Features

- Lists serial ports with their descriptions (Windows `COM3`, macOS `/dev/cu.usbserial-…`,
  Linux `/dev/ttyUSB0`, …). You can also type a port path by hand.
- Baud rate is selectable (9600, 14400, 19200, 38400, 57600 or 115200). The format is fixed at
  8 data bits, no parity, 1 stop bit and no flow control, with finite read and write timeouts.
- The **serial connection** and the **projector power status** have separate indicators.
  Opening the port only shows "Serial port connected". That does not mean the projector has
  answered.
- A power state is shown as *confirmed* only when the projector sends a recognized reply
  (`*POW=ON#` / `*POW=OFF#`) to the status query. Command echoes are never treated
  as proof. Unrecognized replies are logged as received, and the app does not guess
  what they mean.
- After Turn On or Turn Off, the app waits, then queries the status, retrying a limited
  number of times. **A power command is never sent again automatically.**
- All serial I/O runs on a single worker thread that takes commands from a queue, so
  reads and writes never overlap. The GUI is updated only from the Tk main thread.
- The communication log is timestamped and scrollable. It shows raw TX/RX bytes, how
  each reply was read and any errors. You can clear it or save it to a file.
- **Simulation mode** works without hardware. It can simulate normal replies, delayed
  and fragmented replies, no response, unsupported commands and a connection failure.
  Simulated output is labelled `SIMULATED` / `[SIM]` everywhere.
- Connecting and launching the app never send power commands. Closing the app never
  turns the projector off; it only closes the port.

## Installation and launch

Requires Python 3.9 or newer with Tkinter. Tkinter is included with the python.org
installers for Windows and macOS. On Debian/Ubuntu, install it with `sudo apt install python3-tk`.

```bash
git clone https://github.com/nithin2k5/benq-control.git
cd benq-control
python3 -m venv .venv

# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
python main.py
```

Run the tests (standard library `unittest`, no hardware needed):

```bash
python -m unittest discover -s tests -t .
```

## Hardware: adapter and cable

### Use a real USB-to-RS232 adapter

The projector's RS232 port uses **RS232 voltage levels**: roughly ±3 to ±15 V, with a
negative voltage meaning logic 1. Use a USB-to-RS232 adapter with a DB-9 connector
and a proper line driver. Adapters based on FTDI FT232R or Prolific PL2303 with a
MAX232-class transceiver are common. Install the vendor's driver if your OS doesn't
detect it.

### Why a USB-to-TTL adapter is not a substitute

USB-to-TTL/UART boards (bare FTDI, CH340 or CP2102 modules with 3.3 V/5 V pins) use
**0 V / 3.3–5 V logic with the opposite polarity**. Connected directly to RS232:

- the projector will not read their signals correctly, so you get no response or garbage, and
- the projector's RS232 voltages can **damage** the TTL board.

They only work with a level shifter such as a MAX3232 in between, and at that point you have
built an RS232 adapter.

### Verify the MX528 pinout and whether you need a crossover cable

Check the pin assignment in the RS232 section of the MX528 user manual or BenQ's RS232
command document for your model. Do not assume it. What has to be true:

- the computer's **TX** must reach the projector's **RX**,
- the projector's **TX** must reach the computer's **RX**,
- **GND** (pin 5 on a DB-9) must be connected.

On a standard PC-style DB-9 (DTE), pin 2 is RXD, pin 3 is TXD and pin 5 is GND. If the
projector's port uses the same layout, a **null-modem (crossover)** cable that swaps pins 2
and 3 is required. If the projector's port is wired the other way (DCE), use a
straight-through cable. Many BenQ manuals specify a crossover cable, but confirm this for
the MX528 from its documentation, or check with a multimeter (in idle state, the TX pin of
each side sits at a negative voltage relative to GND).

**Adapter loopback test:** with the adapter unplugged from the projector, briefly bridge
pins 2 and 3 on the adapter's DB-9 and click **Check Power Status**. You should see your own
command come back, which the app reports as "Only the command echo was received". This shows
the adapter and port work. It says nothing about the projector, and the app will never
treat an echo as a power state.

## Finding the correct serial port

1. Start the app with the adapter **unplugged** and note the ports listed.
2. Plug the adapter in and click **Refresh Ports**. The new entry is your adapter.
   - **Windows:** `COMn`. Device Manager → *Ports (COM & LPT)* also shows it.
   - **macOS:** `/dev/cu.usbserial-XXXX` or `/dev/cu.PL2303-XXXX`. Prefer `cu.*` over `tty.*`.
     Bluetooth entries like `/dev/cu.Bluetooth-Incoming-Port` are not it.
   - **Linux:** `/dev/ttyUSB0` (most adapters) or `/dev/ttyACM0`. Run `dmesg | tail` right
     after plugging in to confirm.
3. You can type a path into the port box if it isn't listed.

## Matching the projector's baud rate

The baud rate in the app **must equal** the projector's setting. Find **Baud Rate** in the
projector's on-screen menu (on BenQ models usually under the advanced system setup menu;
the exact location depends on the firmware) and select the same value in the app. The
app's default (115200) is just a starting point and is not necessarily your projector's
setting. A mismatch usually causes no reply or garbled replies.

## First test: use the read-only status query

1. Connect the cable, start the app, choose the port and baud rate, and click **Connect**.
   This only opens the port. Nothing is sent.
2. Click **Check Power Status**. This command is read-only and changes nothing on the projector.
3. Read the log:
   - `*POW=ON#` or `*POW=OFF#` → communication works and the state is confirmed.
   - Only the echo of `*pow=?#` → see *Troubleshooting*. Often wiring or a loopback.
   - `Illegal format` / `Unsupported item` / `Block item` → the projector is answering
     but rejected the command. `Block item` often means it is warming up or cooling down.
   - No reply → see *Troubleshooting*.
4. Use **Turn Off (Standby)** or **Turn On** only after the status query works.

**About shutdown:** Turn Off sends the projector's normal shutdown command. The projector
goes into standby, and **its cooling fan may keep running for a while**. Do not cut mains
power during cooling, and do not try to restart it immediately. This app never switches
mains power or relays. Commands sent during warm-up or cool-down may be answered with
`Block item`.

## Troubleshooting

### No response / status unknown

- Baud rate does not match the projector menu setting. Check this first.
- The adapter is USB-to-TTL rather than RS232 (see above).
- TX/RX are not crossed correctly. Try the other cable type (null-modem vs straight-through)
  after checking the pinout.
- GND (pin 5) is not connected, or the cable is loose.
- The projector has no mains power, or it does not answer RS232 in its current standby mode.
  Some models have a standby setting in the menu that keeps control ports active.
- The projector is warming up or cooling down. Wait and query again.

### Only an echo comes back

The echo can come from a loopback in the cable, adapter or wiring, not from the projector.
The app shows "status unknown" in this case. Check the wiring and the cable type.

### Serial port errors

- **"Port … was not found"**: the adapter is unplugged, or its name changed after you
  replugged it. Click **Refresh Ports**.
- **"already in use" / "Permission denied" / "Access is denied"**: another program has the
  port open, such as a serial terminal, another copy of this app or vendor software.
  Close it. On Linux, also add your user to the `dialout` group
  (`sudo usermod -aG dialout $USER`) and log out and back in.
- **"Connection lost"**: the adapter was unplugged or failed while in use. Reconnect it,
  click **Refresh Ports** and connect again.
- **"Write timed out"**: the adapter or driver is not accepting data. Replug the adapter
  or reinstall its driver.

## Project layout

| File | Purpose |
| --- | --- |
| `main.py` | Tkinter interface and application lifecycle |
| `projector_controller.py` | Commands, reply parsing, serial and simulated transports, worker thread |
| `tests/` | Tests for parsing, fragmented replies, timeouts, retries and command serialization |
| `requirements.txt` | pySerial |

## License

MIT. See [LICENSE](LICENSE).
