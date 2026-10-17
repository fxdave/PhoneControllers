# Phone Racing Wheel

Use your phone as a racing wheel for Linux games. The phone opens a web page (no app install);
the PC exposes a virtual joystick through `uinput` that games, Steam and Proton see as a real device.

Controls:

- **Steering wheel**: drag your thumb around the wheel. It turns as many times as the
  configured rotation allows (default 900°, i.e. ±450°), so you can lift and regrab
  to keep turning, like a real wheel.
- **Clutch, brake, gas**: analog. Finger height on the pedal sets how far it's pressed;
  lifting releases it.
- **12 buttons**: Shift −/+ paddles plus 10 general buttons. Labels are only hints; bind
  them to anything in the game.

## Setup

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py            # --port 8765 by default
```

The server prints a URL and QR code. Open it on a phone on the same network, turn the phone
to landscape, and tap **Fullscreen** (Android).

### `/dev/uinput` permission

On most desktop distros the logged-in user already gets access through the `uaccess` ACL
(check with `getfacl /dev/uinput`). If the server fails with a permission error:

```sh
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo usermod -aG input "$USER"
sudo modprobe uinput && sudo udevadm trigger
```

Log out and in after changing groups.

## Device mapping

Device name: `Phone Racing Wheel` (joystick, not gamepad).

| Control  | evdev     | Range           |
|----------|-----------|-----------------|
| Steering | `ABS_X`   | -32767 … 32767  |
| Gas      | `ABS_Y`   | 0 … 1023        |
| Brake    | `ABS_Z`   | 0 … 1023        |
| Clutch   | `ABS_RZ`  | 0 … 1023        |
| Buttons  | `BTN_TRIGGER` … `BTN_BASE6` (12) | |

Test it with `jstest-gtk`, `evtest`, or Steam's controller settings.

## Notes

- If the phone disconnects or the page goes to the background, every axis and button is
  released, so the gas never sticks.
- The rotation range and the optional spring-back-to-center are in **Settings** on the phone.
  Spring-back is off by default because you lift your thumb to regrab when turning several times.
- The phone page uses plain HTTP, so screen wake lock isn't available. Turn the screen
  timeout up on the phone.
- Vibration works in Chrome and Samsung Internet on Android; Firefox and iOS browsers don't
  support it. Android drops browser vibrations of 1 s or less when the phone's "Touch
  feedback" setting is off (Settings → Sound & vibration → Vibration & haptics), so turn
  it on for button vibration.
- If the phone can't reach the page, check that a firewall allows TCP port 8765.
