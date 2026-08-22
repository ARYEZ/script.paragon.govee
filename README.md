# Paragon Home

Control your LAN smart home from inside Kodi, instead of reaching for a
separate app per brand.

Started as Govee light control and grew a driver layer, so it now speaks
Govee, Broadlink and Tuya. Adding a fourth vendor is a fourth driver, not a
change to the menus, the scene engine or the device registry.

Part of the **Paragon TV** project, alongside
[`script.paragontv`](https://github.com/Aryez/script.paragontv),
[`script.paragonsentry`](https://github.com/Aryez/script.paragonsentry) and
[`skin.paragon`](https://github.com/Aryez/skin.paragon).

Built for **Kodi 17.6 (Krypton)** — `xbmc.python 2.25.0`, Python 2.7.

---

## What it does

* **Govee lights** over the Govee LAN protocol, the Govee cloud API, or both —
  power, brightness, RGB colour and colour temperature.
* **Broadlink RM blasters** — learn an IR code from a remote and send it back,
  over the LAN with no Broadlink account.
* **Tuya smart plugs** — GHome, Gosund, Smart Life and most no-name plugs,
  protocol 3.1 to 3.4. Multi-outlet plugs are listed one device per outlet.
* **TP-Link Kasa plugs** — HS100, HS103, HS110 and the KP series. No account,
  no key, no setup: found is the same as usable.
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
3. Open **Paragon Home** → it offers to search. Or **Settings → Lights →
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
Govee (34)
Broadlink (2)
Tuya (5)
Scenes...
Refresh devices
Diagnose device search...
Settings
```

One row per kind of device rather than one per device. With three drivers and
forty devices a flat list ran off the screen, and a Broadlink blaster sat
among the bulbs offering a brightness it does not have. Sorting by driver
first means every menu below this point can offer only what that kind of
device actually does — a plug gets Toggle/On/Off and a status read, a blaster
opens straight into its learned codes, and only Govee rows carry a `[LAN]`
tag, because Govee is the only driver with more than one way to reach a
device.

A driver appears once it has found something. **Diagnose device search** stays
at the top level on purpose: the time you need it is when a driver found
nothing and so has no menu of its own.

Picking a driver gives you its own devices and nothing else:

```
Govee (34)
  All Govee (34)
  Back Office Left Low  [LAN]
  ...
  Manage Govee devices...
```

The title bar shows the running version, which is the quickest way to confirm
a `git pull` took effect.

Pick a light (or **All Lights**) for power, brightness, colour and colour
temperature. **Manage devices** lets you rename a light, exclude one from
"All Lights", or flash it to work out which physical unit it is
(ten flashes, cancellable, and the light is put back as it was).

### Naming your lights

LAN discovery has no names to work with, so lights start out as `H6008 (1E3E)`
— the model plus the last four of the Govee id. Two ways to fix that:

**Set a Govee API key** (*Settings → Govee Cloud*) and run **Refresh
devices**. The names you already gave the lights in the Govee app are pulled
in and applied to all of them at once. The key is used only for discovery and
naming — control still goes over the LAN, so nothing about speed or rate
limits changes. This is by far the least work.

**Or name them by hand:** **Manage devices → Name lights one by one…** Each
light comes on bright magenta in turn and stays lit while the keyboard is up,
so you can look at the room and type what you see. Each one is put back to its
previous state before the walk moves on. Cancel the keyboard to stop; names
entered up to that point are kept, and the walk offers to cover only the
lights still on placeholder names.

### Where names live, and keeping them

Names are stored in `devices.json` in the add-on's profile directory:

```
Windows   %APPDATA%\Kodi\userdata\addon_data\script.paragon.govee\
          (portable: <kodi>\portable_data\userdata\addon_data\...)
Linux     ~/.kodi/userdata/addon_data/script.paragon.govee/
LibreELEC /storage/.kodi/userdata/addon_data/script.paragon.govee/
```

They are written every time a light is renamed or a search completes, so
there is no "save" step. Copy that file somewhere if you want a backup — it
is also how you move your names to another Kodi box.

**Switching transports does not affect names.** Any name that is not the
model-plus-id placeholder is carried across every refresh, so names pulled in
from the Govee cloud stay put after switching to **LAN only** — you can drop
the API key afterwards and keep them. The same is true of names typed by hand
and of the enabled/disabled flag.

A light that does not answer a search keeps its entry rather than being
dropped, so one sleeping bulb cannot erase its own name. The refresh summary
says how many did not answer. For a light that is genuinely gone, use
**Forget this light** in its Manage devices menu.

### Colours (the speed dial)

The named colours offered whenever you pick a colour. **Manage colours…** at
the bottom of any colour menu covers the lot:

* **Add a colour…** — enter a hex code (6 or 8 digit), then name it
* **Rename**, **Change colour**, **Move up/down**, **Delete** on any entry
* **Reset to the built-in colours**

Rows show their hex, e.g. `Paragon Purple  #963CDC`. Order is the menu order,
which is what **Move up/down** is for.

The list lives in `palette.json` next to `scenes.json` and `devices.json`, so
it survives upgrades and can be copied to another box. The same colours are
offered in the scene editor, so anything added once is available everywhere.

A saved colour can also be driven by name:

```
RunScript(script.paragon.govee,action=color,value=Govee Pink)
```

### Scenes

**Scenes…** applies a preset; **Manage scenes…** edits them. A scene sets any
combination of power, brightness and colour/temperature, and can target every
light or just a few. The starter set is `Movie Night`, `Paused`, `Lights Up`,
`Warm Evening`, `Paragon Purple` and `All Off`.

Scenes live in `scenes.json` in the add-on's profile directory, so they survive
upgrades and can be hand-edited or copied between boxes.

### Mixing several colours over the lights

A scene can hold a *set* of colours rather than one. In the scene editor:

**Appearance → Mix of colours (spread over the lights)…** then tick as many
saved colours as you like.

Applying the scene deals those colours across the target lights:

* **Evenly** — each colour is used `floor(n/k)` or `ceil(n/k)` times. With 25
  lights and 3 colours that is 9/8/8, not whatever chance produces.
* **Randomly** — which light gets which is shuffled, so the same scene
  arranges differently every time you apply it. Re-apply for a new pattern.
* Which colour gets the spare light is randomised too, so no colour is
  systematically favoured over repeated applications.

Brightness and power apply to every light as usual; only the colour varies.

Mix colours are **copied into the scene**, not referenced by name, so editing
or deleting a palette colour later cannot silently change a scene you already
built. A per-light entry from a **capture** still wins over the mix, so the
two compose.

### Cycling a mix

A mix scene can move the colours along on a timer. In the scene editor:

**Cycle colours →** *Every minute* (or 15s / 30s / 2m / 5m / 15m, or off).

Every light steps to the next colour in the mix on each tick. The background
service does the stepping, so it keeps going while you browse Kodi and resumes
after a Kodi restart.

Stepping **rotates** the arrangement rather than re-dealing it. That keeps the
even spread exactly — 9/8/8 becomes 8/8/9, never 12/7/6 — and guarantees every
light genuinely changes colour, which a fresh random deal would not.

Applying any other scene **stops** a running cycle, so dimming for a film is
not overwritten a minute later. While one is running the main menu shows a
**Stop cycling (…)** row.

A cycle step sends only the colour — power and brightness are already where
the previous step left them — and sends are paced a few milliseconds apart, so
a step is a manageable trickle rather than a burst of datagrams to every light
at once.

**Cycling is a LAN feature.** Every step drives every light, which is free over
the LAN and metered over the cloud. 25 cloud-driven lights on a one-minute
cycle is roughly 36,000 API calls a day against a Govee limit near 10,000 — the
lights would stop responding partway through the day. The add-on warns before
letting you set an interval that would do this.

### Govee Tap-to-Run scenes

Tap-to-Run scenes cannot be triggered through the Govee API. They are not part
of the LAN protocol, and the developer API key does not reach them either —
they run over Govee's undocumented AWS IoT channel, which needs your Govee
account email and password.

Use **Capture lights as a scene…** instead — it is on the main menu, and
also inside **Scenes…**:

1. Run the Tap-to-Run in the Govee app, so the lights are how you want them.
2. In Kodi, capture it and give it a name.

The add-on reads every light's current power, brightness, colour and colour
temperature and stores them as a scene that holds a *different setting per
light*. Replaying it is pure LAN — instant, no account credentials, no cloud,
no rate limit — which also makes it safe to drive from playback.

**The real limit — read this before relying on capture.** The Govee LAN
protocol reports only power, brightness, RGB and colour temperature. It has no
concept of a *scene*. When the Govee app drives a bulb into one of its scenes,
that happens through the cloud and the bulb's LAN status keeps reporting
whatever was last set locally — so capture faithfully records a state that is
not what you are looking at.

The tell is a capture that disagrees with the room. A real example: 25 bulbs
visibly pink, of which 22 reported `RGB 0,0,0, 3800K, brightness 1`.

This has been confirmed on hardware. An H6008 was set to RGB `255,0,255` at
40% over the LAN and reported back exactly that, so the status is honest — it
simply reflects the last state set *over the LAN*, and a cloud-applied app
scene layered on top is invisible to it.

If you suspect a different model behaves worse, pick a single light and run
**Check status reporting…** from its menu. It sets that bulb to a known colour,
reads the status back, and tells you whether the two agree. If they don't, that
model never updates its LAN status — capture cannot work on it, and Toggle
cannot tell whether a light is already on.

So capture is reliable for lights set **through this add-on** (or anything
else driving plain colour/brightness over the LAN), and unreliable for lights
set by a Govee app scene or Tap-to-Run.

What works instead — **build the look in Kodi**:

1. **Scenes… → Manage scenes… → Add a new scene…**
2. Set **Appearance** (colour or colour temperature) and **Brightness**.
   Under **Colour → Custom hex…** you can paste a code straight out of the
   Govee app — 8-digit codes are accepted as well as 6-digit ones.
3. Leave **Lights** as *all lights* for a uniform look, or pick a subset.
4. **Test this scene** until the room is right, then **Save**.

That is the whole thing for a uniform look, and it is a better result than
capture would give you: it applies over the LAN, so it is instant, needs no
account credentials, and is safe to drive from playback.

Capture then becomes useful in its own right — a way to snapshot a per-light
arrangement you built up by hand, since anything set over the LAN *is*
reported back correctly.

A capture is also a snapshot of a *static* state: an animated Govee scene
(a DIY effect, "autoplay", anything that cycles) is captured as whatever frame
the lights happened to be on.

### Colour codes

Colour entry takes `RRGGBB`, the `RGB` shorthand, and the 8-digit codes the
Govee app produces.

**Govee emits `AARRGGBB` with the alpha always `FF`** — confirmed from real
codes out of the app:

| Govee code | Colour |
|---|---|
| `FF3C447F` | `#3C447F` — deep slate blue |
| `FF7F3C3C` | `#7F3C3C` — deep brick red |

Read the other way round those would mean alphas of `7F` and `3C`, i.e. bulbs
at 50% and 23% transparency, which is meaningless for a light.

The end is still inferred rather than hardcoded, because other sources differ
— Android writes `AARRGGBB`, CSS Color 4 writes `RRGGBBAA`. An alpha in a
colour picker is almost always `FF`, so whichever end reads `FF` is taken as
the alpha; when both ends are `FF` or neither is, alpha-first wins, which is
also the right answer for Govee.

The confirmation always says how a code was read — `#FF2896 (read as
AARRGGBB)` — because dropping the alpha off the wrong end produces a
different colour, and that should be visible rather than a surprise on the
wall. If a code comes out wrong, re-enter it with the byte order swapped.

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
RunScript(script.paragon.govee,action=color,value=FF8800)        # RRGGBB, AARRGGBB,
RunScript(script.paragon.govee,action=color,value=Govee Pink)    #   or a saved colour name
RunScript(script.paragon.govee,action=temp,value=2700)           # Kelvin
RunScript(script.paragon.govee,action=scene,name=Movie Night)
RunScript(script.paragon.govee,action=refresh)
RunScript(script.paragon.govee,action=diagnose)                  # why no lights?
RunScript(script.paragon.govee,action=verifystatus)              # does status work?
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

## Smart plugs (TP-Link Kasa)

**Nothing to set up.** Run **Refresh devices** and they appear, already
carrying the names you gave them in the Kasa app. No account, no local key, no
protocol version to match — a Kasa plug that answers a search can be switched
with what the search returned.

That is worth stating next to the Tuya section, which needs a cloud project
and a 16-character key before a plug will do anything. The difference is the
vendor's, not the add-on's.

Kasa's local protocol obfuscates its JSON with a running XOR rather than
encrypting it. **Anything on your network can switch these plugs** — that was
true before this add-on existed, and it is why they need no key. Worth knowing
if your network is shared.

Multi-outlet models — the KP303 and HS300 strips — report their outlets in
their system info, so they are listed one device per outlet under the names
each outlet already has. A single plug like the HS103 stays one device.

### If a search finds some but not all of them

That is almost always the access point, not the plugs. Mesh systems and guest
networks routinely drop or fail to flood broadcast traffic, so a broadcast
search reaches some devices and not others — and the ones it misses look
broken when they are not. The Kasa app broadcasts too, so it can show fewer
devices than it should for the same reason.

The search handles it: after broadcasting it addresses every host on the
subnet directly, which does not depend on broadcast working at all. When that
turns up devices the broadcast missed, it says so rather than quietly papering
over a real network fault.

The sweep assumes a /24, which is right on essentially every home network. It
is a few hundred small datagrams to port 9999 and takes about a second.

### If a search finds nothing at all

**Diagnose device search → Kasa plugs** says which addresses the search went
out from and lists the causes in order of likelihood. In short: these plugs are
2.4GHz only and neither broadcast nor the sweep crosses subnets, so a plug on a
guest SSID or a separate VLAN from Kodi will never be heard.

One cause has no workaround. Newer TP-Link firmware on some models has closed
the local protocol and talks only to the cloud. A plug that works in the Kasa
app but answers nothing here is almost certainly that, and no add-on can get
around it.

---

## Smart plugs (Tuya)

Everything Tuya-branded is the same platform underneath, whatever the box
says: GHome, Gosund, Smart Life, Teckin and most no-name plugs.

**Discovery needs nothing.** Tuya devices announce themselves by UDP
broadcast, so **Refresh devices** finds them with no account and no setup.

**Switching one needs a local key.** This is the one genuine hurdle, and it is
not something the add-on can work around: a Tuya device will not hand out its
own key, and the only copy lives in Tuya's cloud. Discovery is still useful
without it — it tells you the device id you need in order to go and fetch the
key.

### Getting the key

On any PC with Python 3:

```
pip install tinytuya
python -m tinytuya wizard
```

The wizard needs a free [Tuya IoT Platform](https://iot.tuya.com) account:

1. **Cloud → Create Cloud Project**. Pick the data center that matches the
   region your phone app account was *created* in — a US account is almost
   always **Western America**, and this cannot be changed on the app side
   afterwards. A mismatch here is the usual reason the next step fails.
2. Accept the default API services. Three of them matter: IoT Core,
   Authorization Token Management and Smart Home Basic Service.
3. **Devices → Link App Account → Add App Account → Tuya App Account
   Authorization**, then scan the QR from your phone app under **Me → scan**
   (not the scanner inside "Add Device", which expects a device label).
4. Run the wizard with the project's Access ID and Access Secret. It writes
   `devices.json` with a 16 character `key` per device.

Treat that file as a password file: anyone on your LAN holding the key can
switch the plug.

The cloud project is only ever used to fetch keys. Control is pure LAN, and a
key keeps working after the project's free trial lapses — you would only need
the portal again after re-pairing the plug, which regenerates its key.

### Entering it

**Manage devices → the plug → Set local key**, then **Test connection**, which
reads the plug back and says whether the key was accepted. Worth doing: 16
characters typed on a remote control is an easy thing to get one keystroke
wrong, and a wrong key otherwise shows up later as a plug that will not
switch.

### Multi-outlet plugs

A three-outlet strip is three independent switches in one box. Once a plug has
its key, the next **Refresh devices** asks it what it has and lists one device
per outlet — `Tuya 9ABC Outlet 1`, `Outlet 2`, `Outlet 3`, `USB` — so a scene
can switch one outlet without touching the others. Rename them as you would
any other device. The old single entry disappears at that refresh; it was the
same hardware and would no longer switch anything.

A single-outlet plug stays one device rather than being called "Outlet 1".

### The whole plug at once

Alongside the outlets there is an **All outlets** entry that switches the
whole box — the one to put on a bedtime scene or a leaving-the-house button.

There is no master relay to switch: nothing in a Tuya socket's instruction set
turns the box off as a unit, and the phone app's "all off" is simply every
outlet set in one command. This does the same. Because it is one command, the
outlets go together rather than in sequence, and it is a single round trip
instead of one per outlet.

It reads as **on when anything is drawing power**, not only when everything is
— otherwise a strip with one outlet live would report itself off, and the
button that follows would turn it further on.

A bulk action like "everything off" uses the whole-plug entry and skips its
outlets, since they would be the same instruction sent three more times. A
*scene* is left alone: a scene can tell outlets different things, so folding
them together there would silently drop one.

### Plugs in scenes

A plug has power and nothing else, so a scene that sets brightness and colour
simply switches the plug and sends it nothing it cannot do. One scene can
therefore drive bulbs and plugs together with no special handling.

### Protocol versions

The device list shows what each plug speaks (`Tuya 3.3`). Versions **3.1 to
3.4** are driven.

3.4 is a different protocol wearing the same version number: it negotiates a
session key on every connection, signs each packet with HMAC-SHA256 instead of
a CRC, renumbers both verbs, and moves the version header inside the
encryption. That is all handled — a 3.4 plug behaves like any other, and a
wrong key is caught at the handshake rather than several steps later as an
unreadable reply.

**3.5** encrypts with AES-GCM, which is a different cipher rather than a bigger
version number, and is not built. A 3.5 plug is still discovered and listed,
and says so rather than failing obscurely.

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

**No lights found.** Run **Diagnose LAN search...** from the control panel
first — it tells these apart instead of leaving you guessing, and writes the
full detail to `kodi.log`:

* *Could not open UDP port 4002* — another Govee program holds the reply port.
  Close the **Govee Desktop app**, and any Home Assistant Govee integration on
  the same machine, then search again.
* *Nothing answered* — in order of likelihood: your model does not implement
  the Govee LAN API; inbound UDP 4002 is blocked for Kodi by the firewall; or
  LAN Control is off for the lights in the Govee Home app.
* *Replies that are not Govee* — something else on the network answered; the
  raw contents are in the log.

On Windows, allow `kodi.exe` through Windows Defender Firewall on private
networks — the scan goes out fine but the replies get dropped otherwise.

**The Govee Desktop app sees my lights but the add-on doesn't.** That does not
prove the lights speak the LAN API. Govee's desktop app uses its own protocol,
and the documented LAN API covers only certain models — largely the RGBIC
strips and lamps. If your lights have no **LAN Control** toggle in the Govee
Home app, they are cloud-only: set a Govee API key in Settings and switch
**How to reach the lights** to Automatic or Cloud only.

On a multi-homed box the scan used to leave the wrong interface; since v1.0.2
it goes out on every interface plus a broadcast. If you still need to pin it,
set **Advanced → Network address to send from** to the LAN address.

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
python3 tests/test_paragon_home.py      # 354 tests
python3 tests/check_py2.py              # Python 2.7 syntax gate
python3 tests/validate_addon.py         # manifest and settings cross-check
python3 tools/make_assets.py            # regenerate icon.png / fanart.png
```

The tests run without Kodi and without hardware: `tests/kodistubs/` replaces the
Kodi API, a UDP socket on loopback stands in for a Govee light, and a local HTTP
server stands in for the Govee cloud. Broadlink and Tuya have fakes too, each
speaking its real wire format — the Tuya one negotiates a 3.4 session key and
rejects a status query framed the way a control should be. That covers protocol
encoding, transport selection, the scene engine and the menu walks.

### Why the add-on id still says "govee"

The display name is **Paragon Home**, but the add-on id is still
`script.paragon.govee` and will stay that way. The id is what Kodi uses to name
the settings folder, so changing it would strand every device name, scene,
colour, learned IR code and Tuya key in an `addon_data` directory the add-on no
longer reads — and would break any keymap or favourite holding a
`RunScript(script.paragon.govee,…)` line. A cosmetic rename is not worth
re-naming thirty-four bulbs over.

Inside the code, `govee_lan.py`, `govee_cloud.py` and `GoveeController` keep
their names on purpose too: those really are the Govee driver, not the add-on.

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
resources/lib/paragon_home.py  the add-on session
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
