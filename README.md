# Paragon Govee

Control your Govee lights from inside Kodi, instead of reaching for the Govee
desktop or phone app.

Part of the **Paragon TV** project, alongside
[`script.paragontv`](https://github.com/Aryez/script.paragontv),
[`script.paragonsentry`](https://github.com/Aryez/script.paragonsentry) and
[`skin.paragon`](https://github.com/Aryez/skin.paragon).

Built for **Kodi 17.6 (Krypton)** — `xbmc.python 2.25.0`, Python 2.7.

---

## What it does

* **Finds your lights** over the Govee LAN protocol, the Govee cloud API, or both.
* **Controls them** — power, brightness, RGB colour and colour temperature.
* **Scenes** — named presets (`Movie Night`, `Paused`, `All Off`, …) that you
  can edit in the add-on and re-use everywhere.
* **Playback lighting** — an optional service that dims when playback starts,
  brings the lights part-way up on pause, and restores them on stop.
* **Remote and keymap friendly** — every action is reachable via `RunScript()`,
  so it can go on a button or in Favourites.

### Why two transports

| | LAN API | Cloud API |
|---|---|---|
| Needs internet | No | Yes |
| Needs an API key | No | Yes |
| Latency | Milliseconds | Hundreds of ms |
| Rate limited | No | Yes (daily + per-device) |
| Requires | "LAN Control" on in the Govee app | Nothing on the device |

The default mode is **Automatic**: LAN when the light supports it, cloud when it
doesn't. This matters for the playback service — every play/pause would
otherwise eat cloud quota.

---

## Installing

1. Download this repository as a ZIP (**Code → Download ZIP** on GitHub).
2. In Kodi: **Settings → Add-ons → Install from zip file**.
3. Pick the ZIP.

Kodi requires the folder inside the ZIP to be named `script.paragon.govee`.
GitHub's ZIP names it `script.paragon.govee-main`, so either rename the folder
before zipping, or copy the folder straight into your Kodi `addons` directory
and restart Kodi.

---

## First run

### For LAN control (recommended)

1. In the **Govee Home** app, open each light → **Settings** → turn on
   **LAN Control**. Not every model has it; check Govee's supported list.
2. Make sure the lights and the Kodi box are on the same subnet, and that
   UDP ports **4001**, **4002** and **4003** aren't blocked between them.
3. Open **Paragon Govee** → it offers to search. Or **Settings → Lights →
   Search for lights now**.

### For cloud control

1. In the **Govee Home** app: **Profile → About Us → Apply for API Key**. The
   key arrives by email.
2. Paste it into **Settings → Govee Cloud → Govee API key**.
3. Run a device search.

Lights found on both transports are merged into one entry, and the friendly
name you set in the Govee app is used.

---

## Using it

Launch the add-on for the control panel:

```
All Lights (3)
Living Room Strip  [LAN]
Bedside Lamp       [LAN+CLOUD]
Scenes...
Refresh devices
Manage devices...
Settings
```

Pick a light (or **All Lights**) for power, brightness, colour and colour
temperature. **Manage devices** lets you rename a light, exclude one from
"All Lights", or flash it to work out which physical unit it is.

### Scenes

**Scenes…** applies a preset; **Manage scenes…** edits them. A scene sets any
combination of power, brightness and colour/temperature, and can target every
light or just a few. The starter set is `Movie Night`, `Paused`, `Lights Up`,
`Warm Evening`, `Paragon Purple` and `All Off`.

Scenes live in `scenes.json` in the add-on's profile directory, so they survive
upgrades and can be hand-edited or copied between boxes.

### Playback lighting

**Settings → Playback lighting → Change the lights with playback.**

Choose a scene for playing, paused and stopped. Options let you limit it to
video only, require fullscreen, and delay the dim (handy if you skip trailers).

The service only restores lights on stop if it was the thing that dimmed them —
stopping a stream will never switch on lights you deliberately left off.

### RunScript actions

For keymaps, Favourites, or other add-ons:

```
RunScript(script.paragon.govee)                                  # control panel
RunScript(script.paragon.govee,action=toggle)
RunScript(script.paragon.govee,action=on)
RunScript(script.paragon.govee,action=off)
RunScript(script.paragon.govee,action=brightness,value=20)       # 1-100
RunScript(script.paragon.govee,action=color,value=FF8800)        # RRGGBB
RunScript(script.paragon.govee,action=temp,value=2700)           # Kelvin
RunScript(script.paragon.govee,action=scene,name=Movie Night)
RunScript(script.paragon.govee,action=refresh)
```

Add `target=<name or device id>` to any of them to hit one light:

```
RunScript(script.paragon.govee,action=off,target=Bedside Lamp)
```

Example `keymaps/govee.xml` in your Kodi userdata:

```xml
<keymap>
  <global>
    <keyboard>
      <f9>RunScript(script.paragon.govee,action=toggle)</f9>
      <f10>RunScript(script.paragon.govee,action=scene,name=Movie Night)</f10>
    </keyboard>
  </global>
</keymap>
```

---

## Settings

| Setting | Default | Notes |
|---|---|---|
| How to reach the lights | Automatic | Or force LAN-only / cloud-only |
| LAN search time | 3s | Raise it on a busy or slow network |
| Repeat each LAN command | 2 | UDP has no acknowledgement, so commands are sent more than once |
| Search for lights when Kodi starts | off | Handy if lights get new DHCP addresses |
| Govee API key | empty | Only needed for cloud control |
| Verify the Govee TLS certificate | on | Only turn off if an old box has an unusable CA bundle |
| Change the lights with playback | off | The playback service |
| Network address to send from | automatic | Set it if the box is multi-homed (VPN, docker bridge) |
| Verbose logging | off | Adds per-command detail to `kodi.log` |

---

## Troubleshooting

**No lights found.** Confirm LAN Control is on in the Govee app, and that Kodi
and the lights are on the same subnet. On a multi-homed box (VPN or docker
interface) the discovery multicast may leave the wrong interface — set
**Advanced → Network address to send from** to the LAN address.

**Commands work but "Show status" fails.** Status replies arrive on UDP 4002.
If another Govee integration (Home Assistant, a second Kodi) already holds that
port, status is unavailable while control still works — control needs no reply.

**Cloud says "rate limit reached".** The Govee cloud API is metered per day and
per device per minute. Enable LAN Control on the lights, or don't drive them
from playback over the cloud.

**Toggle turns lights on when they were already on.** Toggle reads state first;
if no state can be read (see above) it defaults to switching on, because a
button that appears to do nothing is worse.

---

## Development

```
python3 tests/test_paragon_govee.py     # 90 tests
python3 tests/check_py2.py              # Python 2.7 syntax gate
python3 tools/make_assets.py            # regenerate icon.png / fanart.png
```

The tests run without Kodi and without hardware: `tests/kodistubs/` replaces the
Kodi API, a UDP socket on loopback stands in for a Govee light, and a local HTTP
server stands in for the Govee cloud. That covers protocol encoding, transport
selection, the scene engine and the menu walks.

`check_py2.py` exists because Krypton runs Python 2.7 and a 2.7 interpreter is
no longer easy to come by — it walks the AST of every shipped file and fails on
anything 2.7 cannot parse (f-strings, annotations, `yield from`, Python-3-only
imports, classes with no base).

### Layout

```
addon.xml                     add-on manifest
default.py                    script entry point + RunScript actions
service.py                    playback-reactive lighting service
resources/settings.xml        Krypton-format settings
resources/lib/compat.py       Python 2/3 shims
resources/lib/addon_utils.py  logging, settings, notifications, JSON store
resources/lib/govee_lan.py    Govee LAN (UDP) client
resources/lib/govee_cloud.py  Govee Cloud (HTTPS) client
resources/lib/devices.py      device model + transport selection
resources/lib/scenes.py       scene model and application
resources/lib/paragon_govee.py  the add-on session
resources/lib/gui.py          dialog-driven control panel
```

### Targeting Kodi 19+

The Python sources already run unmodified on Python 3. To move to Matrix or
later, change `xbmc.python` in `addon.xml` to `3.0.0` and convert
`resources/settings.xml` to the Kodi 18+ settings format — Kodi 19 refuses to
load the old format. The code paths for `xbmcvfs.translatePath` and the removal
of `LOGNOTICE` are already handled.

### Not included

The add-on ships English-only, with literal strings rather than a
`resources/language/` bundle. Adding one means moving every UI string to an id
in `strings.po`; worth doing if the add-on is ever translated, but it would only
cost readability today.

---

## Licence

GPL-3.0. See [LICENSE.txt](LICENSE.txt).

Govee is a trademark of its respective owner. This add-on is unofficial and not
affiliated with or endorsed by Govee.
