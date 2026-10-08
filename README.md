# Matcha Alert

Checks matcha stock on online stores every 15 minutes and messages you on Telegram when something changes:

- 🟢 **Back in stock**: an item that was sold out can be bought again
- 🔴 **Sold out**: an item just sold out
- 🆕 **New listing**: a new product appeared in a watched collection

It's a small Python script with no AI in the loop, so each scan costs nothing. It only messages you when something changes (plus one summary on the very first run), so you won't get pinged every 15 minutes.

## Files

| File | What it is |
|---|---|
| `matcha_alert.py` | The scanner. Python 3.9+, standard library only, nothing to install. |
| `config.json` | The stores and products to watch. Edit this to add more. |
| `state.json` | Created automatically. Remembers the last stock status and which chats are subscribed (by ID only, no names). |
| `.github/workflows/scan.yml` | Runs the scan every 15 minutes on GitHub for free. |
| `run_local.sh` | Optional: run it from your own computer with cron instead. |

---

## Step 1: Create your Telegram bot (about 5 minutes)

1. Open Telegram and search for **@BotFather** (it has a blue verified tick).
2. Tap **Start**, then send `/newbot`.
3. BotFather asks for a **name**. Anything works, e.g. `Matcha Alert`.
4. It asks for a **username**. This must end in `bot` and be unique, e.g. `amber_matcha_alert_bot`.
5. BotFather replies with a **token** that looks like `7412345678:AAH3x...`. Copy it. This is your `TELEGRAM_BOT_TOKEN`.
   Keep it secret: anyone with it can send messages as your bot. If it leaks, send `/revoke` to BotFather to get a new one.

## Step 2: Choose who gets the alerts

There are two ways, and you can use both.

**Groups and people subscribe themselves (no chat ID needed).** Any Telegram group that adds the bot is subscribed automatically, and so is anyone who opens the bot and sends `/start`. Within 15 minutes (at the next scan) the group gets a welcome message with the current stock list, then an alert whenever stock changes.
- To add the bot to a group: open the group → tap its name → **Add members** → search your bot's username → Add.
- To unsubscribe: remove the bot from the group, or send `/stop`.
- `/status` in a subscribed chat sends the full stock list at the next scan.

**Your own chat, always (optional).** If you want your personal chat to get alerts no matter what, set `TELEGRAM_CHAT_ID` (Step 3). To find it:
1. Open your bot, press **Start**, and send it `hi`.
2. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates` with your token in place of `<TOKEN>` (keep the word `bot` in front).
3. Find `"chat":{"id":123456789`. That number is your chat ID. Group IDs start with a minus sign; include it. Several IDs can be separated with commas.

Do this before the first scan runs: once the scanner is running it reads (and clears) these updates itself.

**Who can add your bot?** Anyone who knows its username can add it to their group and get alerts. That costs you nothing, but if you'd rather keep it to your own groups, add the bot to them first, then send BotFather `/setjoingroups`, pick your bot, and choose **Disable**. Groups already added keep working. To turn self-subscribing off completely, set `"group_subscriptions": false` in `config.json`.

## Step 3: Put it on GitHub so it runs every 15 minutes

1. Create a GitHub account if you don't have one, then create a **new repository** (Private is fine), e.g. `matcha-alert`.
2. Upload all the files from this folder to it, including the hidden `.github` folder. (On the repo page: **Add file → Upload files**, then drag the folder contents in. If the `.github` folder doesn't upload, create the file manually: **Add file → Create new file**, name it `.github/workflows/scan.yml`, and paste the contents.)
3. Add your two secrets: go to the repo's **Settings → Secrets and variables → Actions → New repository secret** and add:
   - Name `TELEGRAM_BOT_TOKEN`, value: your token from Step 1
   - Name `TELEGRAM_CHAT_ID`, value: your number from Step 2 (optional; skip it if you'll only use groups)
4. Allow the workflow to save its memory file: **Settings → Actions → General → Workflow permissions →** choose **Read and write permissions** → **Save**.
5. Do a first run by hand: go to the **Actions** tab → **Matcha stock scan** → **Run workflow**. Within a minute or so you should get a stock summary on Telegram (in your own chat, plus any group you've already added the bot to). 🎉

From then on it runs every 15 minutes by itself.

Good to know about GitHub's scheduler:
- Runs can start **a few minutes late** (sometimes 10+ minutes when GitHub is busy). Fine for restocks, but not to-the-second.
- In a **public** repo, GitHub pauses scheduled runs after 60 days with no repository activity. The bot commits `state.json` whenever stock changes, which normally keeps it active, but if alerts ever stop, check the Actions tab and press "Enable workflow".
- Private repos get 2,000 free Actions minutes a month. Each scan takes well under a minute, but GitHub rounds every run up to a full minute, so 15-minute scans use about 2,900 minutes a month. **Make the repo public to avoid that limit** (public repos get unlimited minutes; your secrets stay hidden either way), or change the schedule to `*/30 * * * *` in a private repo.

To get a full stock list on demand: **Actions → Matcha stock scan → Run workflow**, tick **Send a full stock summary**.

## Alternative: run it on your own computer (Mac/Linux)

Use this if you'd rather not use GitHub. Your computer has to be on for it to scan.

1. Put this folder somewhere, e.g. `~/matcha-alert`.
2. Create a file called `.env` in that folder:

   ```
   TELEGRAM_BOT_TOKEN=7412345678:AAH3x...
   TELEGRAM_CHAT_ID=123456789
   ```

3. Test it:

   ```bash
   cd ~/matcha-alert
   set -a; . ./.env; set +a
   python3 matcha_alert.py --test-telegram   # should send "Matcha Alert is connected"
   python3 matcha_alert.py --dry-run         # shows current stock, sends nothing
   ```

4. Schedule it: run `crontab -e` and add this line (fix the path):

   ```
   */15 * * * * /Users/amber/matcha-alert/run_local.sh
   ```

   Output goes to `scan.log` in the same folder.

On Windows, use Task Scheduler to run `python matcha_alert.py` in this folder every 15 minutes, with the two variables set as user environment variables.

---

## Adding more stores and products

Everything lives in `config.json`. After editing, commit the change (on GitHub, just edit the file in the browser).

**Another product on the same store:** add its link to `products`.

**A new Shopify store** (most small matcha shops are Shopify; if the site has `/products/...` and `/collections/...` links, it probably is): add another block to `sites`:

```json
{
  "name": "Some Tea Shop",
  "adapter": "shopify",
  "collections": ["https://sometea.com/collections/matcha"],
  "products": ["https://sometea.com/products/ceremonial-matcha-30g"]
}
```

**A non-Shopify store:** use `"adapter": "html"` and product links only. It reads the stock status that most shops embed for Google, and otherwise looks for words on the page. If it gets it wrong, tell it what words the site uses:

```json
{
  "name": "Other Shop",
  "adapter": "html",
  "products": ["https://othershop.example/item/123"],
  "in_stock_text": ["add to cart"],
  "out_of_stock_text": ["sold out", "notify me when available"]
}
```

Test any change with `python3 matcha_alert.py --dry-run` before relying on it. If a store needs something smarter, a new adapter can be added in `matcha_alert.py` next to `shopify_product`.

**Collection vs product links:** a collection link watches every product in that collection (and tells you about new ones). Product links are your favourites; they're marked ⭐ in summaries. Set `"only_watched_products": true` under `notify` if you only want alerts for your ⭐ products.

## Settings (`notify` in config.json)

| Setting | Default | Meaning |
|---|---|---|
| `back_in_stock` | true | Alert when something comes back |
| `sold_out` | true | Alert when something sells out |
| `new_product` | true | Alert when a new product appears in a collection |
| `removed` | false | Alert when a product disappears from the store |
| `errors` | true | Alert (once) when a site can't be read |
| `summary_on_first_run` | true | Send a full list the first time it runs |
| `only_watched_products` | false | Only alert about products listed under `products` |
| `group_subscriptions` | true | Groups that add the bot (and people who send `/start`) get alerts |
| `allow_private_chats` | true | Let individual people subscribe with `/start`, not just groups |

## Troubleshooting

- **Group didn't get the welcome message:** wait for the next scan (up to 15 minutes, or press Run workflow). If it still doesn't come, remove and re-add the bot.
- **No message at all:** run `python3 matcha_alert.py --test-telegram`. A `chat not found` error means you haven't pressed Start in the bot chat, or the chat ID is wrong.
- **`Unauthorized`:** the token is wrong or was revoked.
- **No alerts for a while:** that's normal. It only messages when stock changes. Use "Run workflow" with summary ticked to see current stock.
- **"couldn't read" alert:** the store was down or blocked the request. It keeps trying every 15 minutes and won't send false "sold out" alerts in the meantime.
