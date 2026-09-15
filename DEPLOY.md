# Putting the site online

Start to finish, about 30 minutes, most of it waiting for the price collection
to run. It costs nothing — no card, no trial, no monthly bill.

Written assuming you have never used GitHub. Every command goes in **Terminal**
(press ⌘+Space, type "Terminal", hit Enter), and every command starts by moving
into the project folder, so it is safe to copy them one at a time.

---

## What you are about to build

```
   YOUR MAC                    GITHUB                      ANYONE
   ────────                    ──────                      ──────
   collects prices   ──push──▶ rebuilds the site  ──────▶  opens a fast page
   once a week                 (free, ~2 minutes)          at your own URL
```

Your Mac does the work that needs a real Indian internet connection. GitHub
does the hosting. Visitors never touch a retailer — they read pages that were
already built.

**Cost: ₹0/month.** GitHub Pages is free for public repositories, and Actions
minutes are free and unlimited on them too. There is no paid tier you drift
into; the limits are 1 GB of site (you'll use ~20 MB) and 100 GB of traffic a
month (you'd need tens of thousands of visitors).

**The trade you are making:** the code and the price data are public. The code
being public is the point — people can check what it does. The price data is
public information that seven retailers already display on their own websites.
Your database file, with your local working copy, is *not* published.

---

## Step 1 — Collect some prices first

An empty site is a bad first impression, and the build will refuse to run
without data. So do this before anything else.

```bash
cd ~/PycharmProjects/lego-price-tracker
source .venv/bin/activate
python -m legotracker catalog        # refresh the set list, ~10 seconds
python -m legotracker priceall       # check every retailer, ~45 minutes
```

Leave it running. It prints a line per set. If you stop it with Ctrl+C, nothing
is lost — it skips anything already priced in the last 7 days, so running it
again carries on where it left off.

Then build the site and look at it:

```bash
python -m legotracker publish --open
```

That writes `data/catalog.json` and `data/observations.csv`, builds the whole
site into `site/`, and opens it in your browser. **Click around.** Everything
you see now is what visitors will see.

---

## Step 2 — Put your name on it

Open `content/site.md` in any text editor and change the top lines:

```
title: baijalbuilds
tagline: Live LEGO price comparison across Indian retailers, plus what I'm building.
instagram_handle: baijalbuilds
```

Then `content/builds/2026-09-current-build.md` — replace the placeholder with
whatever you are actually building. Run `python -m legotracker publish --open`
again to see the change.

---

## Step 3 — Create the GitHub repository

Go to **https://github.com/new** (make an account first if you need one).

| Field | What to put |
|---|---|
| Repository name | `lego-price-tracker` |
| Description | Free LEGO price comparison for India |
| Public / Private | **Public** — Pages and Actions are only free on public repos |
| Add a README | **Leave unticked** — you already have one |
| .gitignore / licence | **Leave both as "None"** — you already have them |

Click **Create repository**. The next page shows a URL like
`https://github.com/yourname/lego-price-tracker.git`. Keep that tab open.

---

## Step 4 — Push your code up

Back in Terminal:

```bash
cd ~/PycharmProjects/lego-price-tracker

# Housekeeping from the setup. The first two lines remove a leftover transfer
# archive and a stale git lock file; both are harmless but git will refuse to
# work until the lock is gone.
rm -f lego-website.tar.gz .git/index.lock
rm -rf web scripts/com.rb.legotracker.plist

git init
git add -A
git commit -m "LEGO price tracker for India"
git branch -M main
git remote add origin https://github.com/YOURNAME/lego-price-tracker.git
git push -u origin main
```

Replace `YOURNAME` with your actual GitHub username.

**On the password prompt:** GitHub does not accept your account password here.
When it asks, go to **https://github.com/settings/tokens** → *Generate new
token (classic)* → tick **repo** → generate → copy it, and paste that as the
password. macOS will remember it, so this is a one-time annoyance.

**Before you push, sanity-check what's going:**

```bash
git status --short | head -50
```

You should see your code, `content/`, and the three files in `data/`. You
should **not** see `lego.sqlite3`, `.venv`, or anything ending `.env`. Those are
excluded on purpose. If you do see them, stop and say so rather than pushing.

---

## Step 5 — Turn on GitHub Pages

On your repository page:

1. **Settings** (top right of the repo, not your account settings)
2. **Pages** in the left sidebar
3. Under **Source**, choose **GitHub Actions** — *not* "Deploy from a branch".
   This matters: the branch option would serve the raw repository instead of
   the built site.

That's the whole configuration. There is nothing to save.

---

## Step 6 — Watch the first build

Click the **Actions** tab. You should see "Build and publish site" running.
It takes about two minutes.

- **Green tick** → done. Your site is at
  `https://YOURNAME.github.io/lego-price-tracker/`
- **Red cross** → click into it and read the last red line. The build checks
  its own output, so a failure usually names the problem directly (most often:
  the `data/` files didn't get committed).

The URL also appears under Settings → Pages once the first deploy finishes.

---

## Step 7 — Make it update itself

```bash
cd ~/PycharmProjects/lego-price-tracker
./scripts/install-schedule.sh
```

Every Sunday at 03:00 your Mac now collects prices, rebuilds the data files and
pushes. GitHub republishes the site a couple of minutes later. You do nothing.

Test it immediately rather than waiting a week:

```bash
launchctl start com.legotracker.weekly
tail -f data/weekly.log          # Ctrl+C to stop watching
```

To stop it later: `./scripts/install-schedule.sh --remove`

**If the Mac is asleep at 03:00**, launchd runs the job as soon as it wakes —
but it cannot wake a sleeping Mac by itself, and if the Mac is off or the lid
is shut all week, nothing collects locally.

**`.github/workflows/collect.yml` covers that gap.** It runs the same three
steps — collect, publish, push — on GitHub's own infrastructure, on the same
Sunday-03:00-IST schedule, whether or not your Mac is awake. Since a fresh
Actions runner has no database (nothing survives between runs except what's
in the repo), it starts with `python -m legotracker rebuild`, which
reconstructs the working SQLite database from the committed
`data/catalog.json` + `data/observations.csv` — the same two files your Mac's
own `publish` step already writes — before pricing anything. The two
schedules don't coordinate and don't need to: whichever one runs, `priceall`
skips anything priced in the last 7 days, so the other one just finds less
work to do. You can also trigger a collection run by hand from the repo's
**Actions** tab → *Collect prices* → *Run workflow* — useful right after
setup, without waiting for either Sunday.

If both the Mac and Actions miss a week, the site keeps working and starts
labelling prices "last checked 12 days ago" in amber. That is the honest
failure mode, and it is why every price on the site carries a date.

---

## Day-to-day

**Change what you're building** — edit
`content/builds/2026-09-current-build.md` directly on github.com: open the
file, click the pencil icon, edit, *Commit changes*. The site rebuilds itself
in about two minutes. Works from your phone.

**Add a finished build** — on github.com, go to `content/builds/`, click *Add
file* → *Create new file*, name it something like `2026-10-hogwarts.md`, and
paste the contents of `_TEMPLATE.md` with your own details. Set
`status: complete` and it files itself under finished builds.

**Add photos** — `content/media/` → *Add file* → *Upload files*, drag them in,
then list the filenames under `photos:` in a build post. Resize them first; a
photo straight off a phone is about 4 MB and will make the page crawl on mobile
data.

**Force a price update now** — `./scripts/weekly.sh`

**Rebuild the site without collecting** — `python -m legotracker publish` then
`git add -A && git commit -m "update" && git push`

---

## Your own domain (optional, ~₹900/year)

Only worth it if you want `legoindia.in` instead of
`yourname.github.io/lego-price-tracker`. Buy the domain anywhere, then
Settings → Pages → **Custom domain**, and follow the DNS instructions GitHub
shows. Tick **Enforce HTTPS** once it offers to. Nothing about the build
changes.

---

## When something breaks

**The site shows old prices.** Your Mac missed its run. `./scripts/weekly.sh`
fixes it now; check `data/weekly.log` for why.

**`data/lego.sqlite3` won't open ("database disk image is malformed").** This
is the local working copy, not your data — it is gitignored and gets rebuilt
from `data/catalog.json` + `data/observations.csv`, which are the real,
committed record. Move the corrupt file aside and rebuild it:

```bash
mv data/lego.sqlite3 data/lego.sqlite3.bad
python -m legotracker rebuild
```

**A retailer shows "no match" everywhere.** They changed their page layout.
`python -m legotracker diagnose` tells you which one and whether it was
unreachable (a network problem) or reached-but-unparsed (a layout change). Fixes
live in one file per retailer under `legotracker/sources/`.

**The Actions build went red.** Open the Actions tab, click the failed run, read
the last red line. The build deliberately fails loudly rather than publishing a
half-empty site.

**You pushed something you shouldn't have.** Tell someone before deleting
anything — git keeps history, so removing a file in a new commit does *not*
remove it from the repository's past. The fix depends on what it was.

---

## What this costs, honestly

| | |
|---|---|
| GitHub Pages hosting | ₹0 |
| GitHub Actions builds | ₹0 (unlimited on public repos) |
| Price collection | ₹0 — your Mac, your broadband |
| Set images | ₹0 — loaded from the retailers' own servers |
| Domain name | optional, ~₹900/year |

The only thing that would start costing money is making the repository private,
which would also switch off free Pages. Don't.
