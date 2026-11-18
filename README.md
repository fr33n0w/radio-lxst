# radio-lxst

Internet radio over [Reticulum](https://reticulum.network). Put a web radio stream, a folder of music or a live sound card input on the air, and listeners tune in by **calling** the station, like a phone call, with [LXST](https://pypi.org/project/lxst/): from the radio-lxst client, Sideband or rnphone.

Version 0.2 · [Reticulum License](LICENSE)

| Server | Client |
|---|---|
| ![server](screenshots/server.png) | ![client](screenshots/client.png) |

## What it does

- **Server**: plays an existing stream (Icecast, Shoutcast, Zeno, any http audio) a folder (shuffle, next track) or a live input (mic, line in, VB-Cable, an SDR app, Loopback of what the PC plays), with volume and limiter. Many listeners at once, each on its own encrypted Reticulum Link. Announces itself so clients can find it.
- **Client**: list of the stations heard on the network, a steady 2 s buffer, volume, output device, and the station's name, listeners and track during the call.
- **Compatible**: any LXST phone (Sideband, rnphone) can listen too, it just calls the station's Call ID.

## Quickstart

You need Python 3.10+ and a working Reticulum setup (if `rnsd` is running the programs use it, otherwise they start Reticulum with your `~/.reticulum` config). The server also needs [ffmpeg](https://ffmpeg.org).

### Windows

```
pip install -r requirements.txt
winget install Gyan.FFmpeg
```

- Server: double click `radio-gui.bat`, choose the source in **Input**, press **On air**, copy the **Call ID**.
- Client: double click `client-gui.bat`, paste the Call ID (or double click a station in the list), press **Play**.

### Linux (Debian/Ubuntu)

```
sudo apt install python3-pip python3-tk ffmpeg
pip install -r requirements.txt --break-system-packages
python3 radio_gui.py           # server
python3 radio_client_gui.py    # client
```

The client plays through PulseAudio or PipeWire. If `pip` fails on `pycodec2` (pulled in by `lxst`, not used here): `sudo apt install libcodec2-dev` and retry.

## Terminal

Server (also fine on a headless box):

```
python radio_lxst.py --url https://example.com/live.mp3 --name "My Radio"
python radio_lxst.py --folder ~/music --shuffle --name "My Radio"
python radio_lxst.py --list-inputs
python radio_lxst.py --input "CABLE Output" --name "My Radio"
```

Options: `--announce-every MIN`, `--gain DB`, `--ceiling DBFS`, `--no-limiter`, `--frame-ms 60|120`, `--identity FILE`, `--rnsconfig DIR`.

Client:

```
python radio_client.py <Call ID>
python radio_client.py --list 60      # wait 60 s for station announces
```

Options: `--quality SHQ|HQ|MQ|LBW|VLBW|ULBW`, `--buffer SEC`, `--volume DB`, `--device NAME`.

On Linux use `python3`.

## Using the GUIs

Server:
- **Input**: stream URL, folder or line input (pick the device from the list, **Refresh** after plugging one in). **Switch now** changes source on air, listeners stay connected.
- **Output**: station name, quality, announce on air and every N minutes, go on air at start.
- **Listeners**: who is tuned in, quality, time, kbit/s.
- Volume and limiter work live; the display shows how much the limiter pulls down.

Client:
- Double click a station to listen. "Play last station at start" resumes on launch.
- Buffer: more seconds = fewer dropouts on slow paths.

Line input, for example from an SDR program:
- Send the SDR audio to **CABLE Input** (VB-Audio Virtual Cable) and choose **CABLE Output**, or
- leave it on the speakers and choose **Loopback: <your speakers>**: everything the PC plays goes on air.
- On Linux the PulseAudio/PipeWire monitor sources work as loopback.

Addresses are copied with a click: the CALL ID line on the display, the addresses in Output, right click on a station or a listener.

## Listening with Sideband or rnphone

- **Sideband**: Voice, enter the Call ID, call. Use profile **SHQ** and **half duplex**. To see the station name instead of an anonymous caller, save its LXMF address as a contact.
- **rnphone** (`pip install lxst`): type the Call ID and press Enter; Enter again hangs up.

## How it works

- The station announces three destinations from one identity: `lxst.telephony` (the number to call), `lxmf.delivery` (its name in Sideband) and `radiolxst.station` (name, listeners, track, on/off air, for the client list).
- ffmpeg decodes stream and files, the sound card input is read directly; radio-lxst encodes Opus once per quality in use and sends the same frames to every listener on that quality.
- The quality follows the caller's profile: Opus stereo 48 kHz for HQ/SHQ, stereo 24 kHz for MQ, mono for the low bandwidth profiles.
- **Steady** (default): 120 ms packets, about 28 kbit/s per listener. **High**: 60 ms packets, up to 56 kbit/s. Meant for internet links (TCP, I2P), not LoRa.
- During the call the station also sends name, listeners and track in a msgpack key (`"radiolxst"`). LXST only reads the signalling and audio keys, so other phones ignore it.
- The caller's microphone is ignored.

## Changes

- 0.2: Added line input source (mic, sound card, VB-Cable, loopback).
- 0.1: First release.

## Files

`radio_lxst.py` server engine and CLI, `radio_gui.py` server window, `radio_client.py` client engine and CLI, `radio_client_gui.py` client window, `ui_common.py` shared look.

Settings are saved next to the scripts (`settings.json`, `client_settings.json`), identities and the station list in `storage/`. Run one server per identity.

## Responsible use

Whoever runs a station is solely responsible for what it broadcasts and must hold the rights to it. The author strongly discourages music piracy: broadcast only your own works, free-licensed music, or content you are allowed to share.

## License

Released under the **Reticulum License**, the same license as [Reticulum](https://github.com/markqvist/Reticulum): free to use, modify and share, but not in systems meant to harm people and not for AI/ML training datasets or models. Full text in [LICENSE](LICENSE).
