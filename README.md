# 🥃 Monthly Whisky Hunter Agent (`June 7, 2012` & `November 8, 2014`)

An automated monthly cloud agent running on **GitHub Actions** that hunts for whiskies **distilled OR bottled/dumped** on **June 7, 2012** or **November 8, 2014** with **shipping to Israel** (prioritizing **Whizita**, **Home of Malts**, **Master of Malt**, local `.co.il` shops, and verified UK/EU stores & auctions that ship to Israel).

When it finds a **new or restocked** match, it sends an instant **Telegram Bot alert** (and optionally opens a GitHub Issue / email); when nothing new is available, it stays completely silent.

---

## How It Works

1. **Schedule:** Runs automatically on the **21st of every month at 09:00 UTC** (timed right after **Whisky Auctioneer**'s monthly auction goes live around the 20th), plus anytime on-demand via **"Run workflow"**.
2. **Hybrid 2-Layer Search:**
   - **Layer 1 — Direct Store & Watchlist Checks (Zero API keys required):** Directly checks known exact-date product pages (such as The Whisky Exchange's dedicated `75cl` US-batch *Ardbeg Galileo `L12 158`* listing, Master of Malt, The Whisky Barrel *Craigellachie 1993–2012*, Fine Drams *Kalkwijck Batch III*, Whisky Club Italia *Ardnamurchan `AD/11:14 CK.342`*) plus search endpoints on **Whizita**, **Home of Malts**, and **Hedonism Wines**.
   - **Layer 2 — Gemini API + Google Search Grounding:** Queries `gemini-2.5-flash` with live Google Search grounding to discover **newly released 2012/2014 single casks** (Signatory Vintage, Gordon & MacPhail, SMWS, Single Malts of Scotland, Cadenhead's, Kavalan Solist `120607`/`141108`, Blanton's `6-7-12`) and live auction lots across **WhiskyAuction.com**, **Whisky Auctioneer**, and **Scotch Whisky Auctions**, then cross-verifies every candidate URL.
3. **Built-In False-Positive Protection:**
   - Rejects US `MM/DD/YYYY` date traps (e.g., `08/11/2014` = August 11 on US catalogs like *Strathisla `SI 19 013`*).
   - Rejects laser clock timestamps (`L21 08/11 14:24`).
   - Distinguishes *Ardbeg Galileo* `L12 158` (`750ml`/`50ml`, bottled June 7, 2012) from `L12 162`/`L12 163` (`70cl` UK/EU, bottled June 11–12, 2012).
   - Blocks known fake/scraper spirits domains and past/sold-out listings.
4. **Deduplication & Restock Tracking:** Saves alerted URLs in [`state/seen_matches.json`](./state/seen_matches.json) so you are only notified once per listing—and automatically re-alerts if a sold-out watched bottle later comes back in stock.

---

## Quick 3-Minute Setup

### Step 1: Create Your Telegram Bot & Get Your Chat ID
1. Open Telegram, message **[@BotFather](https://t.me/BotFather)**, send `/newbot`, follow the prompts, and copy your **Bot Token** (looks like `123456789:AAH...`).
2. Open a chat with your newly created bot and click **Start** (or send it `"hello"` — a bot cannot message you until you send it at least one message).
3. Find your numeric **Chat ID** using either method:
   - Message **[@userinfobot](https://t.me/userinfobot)** on Telegram — it replies immediately with your `Id` (e.g. `123456789`), **or**
   - Visit `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in your browser and copy `"chat":{"id": ... }`.

### Step 2: Add Repository Secrets in GitHub
In this repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret Name | Required? | Value |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | **Yes (for Telegram)** | Token from `@BotFather` |
| `TELEGRAM_CHAT_ID` | **Yes (for Telegram)** | Your numeric Telegram Chat ID |
| `GEMINI_API_KEY` | **Recommended** | Free API key from [Google AI Studio](https://aistudio.google.com/apikey) (powers Layer 2 live web discovery; Layer 1 direct checks still run even without it) |

*(Optional)* Notifications go **only to Telegram** by default (no GitHub Issues / email noise). If you also want backup GitHub Issues opened on matches, go to **Settings → Secrets and variables → Actions → Variables → New repository variable** and set `ENABLE_GITHUB_ISSUE` = `true`.

### Step 3: Send Yourself a Test Ping Right Now
1. Go to the **Actions** tab → **Monthly Whisky Hunter**.
2. Click **Run workflow** → check **`send_test_ping`** → click **Run workflow**.
3. Within ~30 seconds, your Telegram bot will send `"✅ Whisky Hunter Agent Connected!"` confirming everything is live!

---

## Sharing With a Friend (Custom Dates — Zero Code Edits!)

Anyone can run their own monthly Whisky Hunter for **any** birth/anniversary date(s) in ~2 minutes without touching the code:

1. **Fork this repository** on GitHub (click **Fork** in the top-right corner).
2. In the forked repo, go to **Actions** and click **"I understand my workflows, go ahead and enable them"**.
3. Go to **Settings → Secrets and variables → Actions → Variables → New repository variable**:
   - **Name:** `TARGET_DATES`
   - **Value:** Comma-separated `YYYY-MM-DD` date(s), e.g. `2016-03-15,2019-10-22` *(supports 1 or multiple dates!)*
4. Go to the **Secrets** tab (**New repository secret**) and add your own:
   - `TELEGRAM_BOT_TOKEN` (from [@BotFather](https://t.me/BotFather))
   - `TELEGRAM_CHAT_ID` (your Telegram Chat ID)
   - `GEMINI_API_KEY` (free key from [Google AI Studio](https://aistudio.google.com/apikey))
5. Go to **Actions → Monthly Whisky Hunter → Run workflow**, check **`send_test_ping`**, and run it!

*(Tip: You can also run a quick one-off search for a friend's date anytime without changing your saved configuration — just click **Run workflow** and enter `YYYY-MM-DD` into the **`target_dates`** input box!)*

---

## Running Locally / Running Tests

No external pip packages are required (uses Python 3.11+ standard library):

```bash
# Run the unit test suite
python3 -m unittest discover -s tests -v

# Dry-run locally (defaults to 2012-06-07 & 2014-11-08, or pass --dates)
GEMINI_API_KEY="your_key" python3 -m src.main --dates "2016-03-15,2019-10-22" --dry-run
```
