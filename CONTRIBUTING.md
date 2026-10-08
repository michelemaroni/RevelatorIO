# Contributing to io24

Thanks for helping make the io24 easier to use on Linux.

Most changes are easy to check locally. Because this project also controls real
audio hardware, ordinary pull-request checks must stay hardware-free. They
should not claim USB interfaces, write device state, change firmware, alter
audio routing, or assume that an io24 is attached.

## Development setup

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q tests/test_publication_docs.py tests/test_io24_release_package.py
```

Once those pass, run the focused tests for the code you changed. The complete
release command is in [PUBLICATION.md](PUBLICATION.md). Tests that compare
against locally retained Universal Control evidence skip clearly when that
local evidence is absent.

Live tests belong in a separately authorized local campaign. Record the exact
device, route, stimulus, restoration, and result boundary. Live hardware is
never a CI requirement.

Keep user documentation honest about evidence. A successful USB reply proves a
write was sent, not cold-boot persistence, stored-body readback, or audibility.
Full Host setups, UC scenes, UC Device Presets, and the front-panel `Stat`
blocks are different workflows. Voice FX is one shared processor assigned to
one input. Each model has a stored `on` field, while UC and both controllers
normalize the rack so only one model is On at a time.

Do not add Universal Control installers, vendor DLLs or firmware, recovered
factory data, device serials, local absolute paths, captures, VMs, or agent work
records. See [PUBLICATION.md](PUBLICATION.md) for the public-source boundary.
