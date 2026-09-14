# PreLoadedFunds

This folder is where the shipped fund bundle goes. It arrives empty in a
fresh clone, because the bundle is **not** tracked in git — it is
attached to each release as a download:

**<https://github.com/jdderijke/PorxPy/releases/latest/download/porxpy_funds.zip>**

That link always resolves to the newest bundle. Save the file here, then
import it from **Settings → backup & restore**.
[GETTING_STARTED.md](../GETTING_STARTED.md) §4 owns that procedure and
describes it in full; this file only says where the file comes from.

`PreLoadedFunds/*.zip` is gitignored, so your own exports can live here
too without ever colliding with a `git pull`. That is why the bundle
moved out of the repository: git cannot three-way-merge a 30 MB zip, so
while it was tracked, anyone who had re-exported over their copy was met
with *your local changes would be overwritten by merge* on every pull.
