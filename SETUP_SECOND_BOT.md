# Setting up DontMissPlacements next to your existing bot

You already run **DontMissThoseMails**. This guide sets up **DontMissPlacements** as a second, fully separate bot:
own code, own Meta app, own database, own server. Nothing in the old bot changes.

About 45 minutes. Everything is free.

---

## What you can copy and what must be new

| Thing | Copy from the old bot? | Why |
|---|---|---|
| Meta developer account + Business portfolio | ✅ same | |
| Meta **system user** (`bot`) | ✅ same user, but **new token** | a token is tied to one app |
| Meta **app** | ❌ **new app** | each app has one webhook address; a shared app would send every message to the old bot |
| Test number / `WHATSAPP_PHONE_NUMBER_ID` | ❌ new (comes with the new app) | |
| `WHATSAPP_APP_SECRET` | ❌ new (from the new app) | |
| `WHATSAPP_VERIFY_TOKEN` | ❌ new (Render generates it) | |
| `WHATSAPP_RECIPIENT` | whoever uses this bot | your number, or another person's |
| Mailbot Gmail (`IMAP_USERNAME`, `IMAP_PASSWORD`) | ✅ same **if this bot reads the same inbox**, otherwise a new mailbot | both bots only *read*, so sharing is fine |
| Outlook forwarding rule | ✅ nothing to do if same inbox | |
| Gemini keys (`GEMINI_API_KEYS`) | ⚠️ can reuse, **new is better** | both bots would share the free quota; reading posters uses more |
| Neon database (`DATABASE_URL`) | ❌ **new project** | two bots on one database would mix up each other's data |
| Render | ❌ **new account** | the free plan gives ~750 h/month per account, one always-on bot uses ~730 |
| `ADMIN_TOKEN` | ❌ new | |
| cron-job.org | ✅ same account, **add a second job** | |
| `TIMEZONE`, `MAIL_PROVIDER=imap`, `IMAP_HOST`, `IMAP_PORT` | ✅ same (preset in `render.yaml`) | |

> If the same person uses both bots on the same inbox, they'll get two messages about the same mail. Either turn
> `placement off` in one of them, or just let the old one go.

---

## Step 1. Put the code on GitHub (5 min)

1. Unzip `DontMissPlacements.zip` somewhere (it already contains its own git history).
2. On GitHub: **New repository** → name `DontMissPlacements` → **Private** is fine → *don't* add a README → **Create**.
3. In a terminal inside the unzipped folder:
   ```powershell
   git remote add origin https://github.com/rudra20-6/DontMissPlacements.git
   git push -u origin main
   ```

---

## Step 2. New Meta app (10 min)

Same steps as the old bot's HOW_TO_RUN step 5, in the **same** Meta developer account and business portfolio:

1. <https://developers.facebook.com/apps/> → **Create app** → **Other** → **Business** → name it `DontMissPlacements`.
   Pick your **existing** business portfolio.
2. **Add product → WhatsApp → Set up**.
3. **WhatsApp → API Setup**:
   - Copy the **Phone number ID** → `WHATSAPP_PHONE_NUMBER_ID`.
     ✅ **Check:** it must be *different* from the old bot's (`1304633239405337`). If Meta shows you the same test
     number, the two bots can't share it. Add a real phone number to this app instead (**API Setup → Add phone
     number**; the number must not be on WhatsApp) and use its Phone number ID.
   - Copy the **WhatsApp Business Account ID** (WABA ID). You need it in step 5.
   - **To → Manage phone number list** → add the WhatsApp number that will use this bot and enter the code it gets.
     → `WHATSAPP_RECIPIENT` (digits only, with country code, e.g. `91XXXXXXXXXX`).
4. **Permanent token** (reuse your system user):
   <https://business.facebook.com/settings/system-users> → select `bot` → **Assign assets** → **Apps** →
   `DontMissPlacements` → **Full control**. Also assign the new **WhatsApp account** if it's listed.
   Then **Generate token** → app **DontMissPlacements** → expiry **Never** → permissions
   `whatsapp_business_messaging` + `whatsapp_business_management` → copy → `WHATSAPP_TOKEN`.
   (Don't touch the old bot's token. It keeps working.)
5. **App settings → Basic → App secret → Show** → `WHATSAPP_APP_SECRET`.

---

## Step 3. New Neon database (3 min)

In your existing Neon account: **New project** → name `dontmissplacements` → region Singapore → **Connect** → copy the
connection string → `DATABASE_URL`.

## Step 4. Gemini keys (optional, 5 min)

Recommended: 2 new keys, each in a **new** Google Cloud project (<https://aistudio.google.com/apikey> →
**Create API key** → **Create key in new project**). Reusing the old bot's keys also works.
→ `GEMINI_API_KEYS` = `KEY1,KEY2`

---

## Step 5. Deploy on a new Render account (10 min)

1. Generate a new admin password: `py -c "import secrets; print(secrets.token_urlsafe(24))"` → `ADMIN_TOKEN`.
2. Sign up at <https://dashboard.render.com> with a **different email** than the old bot's account.
3. **New → Blueprint** → connect GitHub → pick `DontMissPlacements`. Fill in:

   | Key | Value | Same as old bot? |
   |---|---|---|
   | `ADMIN_TOKEN` | step 5.1 | new |
   | `APP_BASE_URL` | leave empty for now | new |
   | `DATABASE_URL` | step 3 | new |
   | `IMAP_USERNAME` | mailbot Gmail address | same (if same inbox) |
   | `IMAP_PASSWORD` | its 16-letter app password | same (if same inbox) |
   | `GEMINI_API_KEYS` | step 4 | new or same |
   | `WHATSAPP_TOKEN` | step 2.4 | **new** |
   | `WHATSAPP_PHONE_NUMBER_ID` | step 2.3 | **new** |
   | `WHATSAPP_RECIPIENT` | step 2.3 | your call |
   | `WHATSAPP_APP_SECRET` | step 2.5 | **new** |

   `PLACEMENT_MODE=true`, `MAIL_PROVIDER=imap`, `IMAP_HOST`, `IMAP_PORT`, `TIMEZONE` are preset, and
   `WHATSAPP_VERIFY_TOKEN` is generated.
4. After the first deploy: copy the URL (e.g. `https://dontmissplacements-xyz.onrender.com`) → **Environment** →
   `APP_BASE_URL` = that URL, no trailing slash → **Save**.
5. Open `https://YOUR-NEW-APP.onrender.com/health` → `{"ok":true,…}`.

*If Render won't connect the same GitHub account twice:* make the repo public and use **New → Web Service → Public Git
Repository** with build command `pip install -r requirements.txt`, start command
`uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`, and add the variables from the table above plus
`MAIL_PROVIDER=imap`, `IMAP_HOST=imap.gmail.com`, `IMAP_PORT=993`, `PLACEMENT_MODE=true` and your own
`WHATSAPP_VERIFY_TOKEN` (any random text).

---

## Step 6. Connect the webhook (10 min)

Exactly like the old bot (HOW_TO_RUN step 8), but on the **new app** with the **new** URL and values:

1. **WhatsApp → Configuration**: Callback URL `https://YOUR-NEW-APP.onrender.com/webhook/whatsapp`, Verify token =
   `WHATSAPP_VERIFY_TOKEN` from Render → **Verify and save**.
2. **Webhook fields** → `messages` → **Subscribe**.
3. **App settings → Basic**: Privacy Policy URL `https://YOUR-NEW-APP.onrender.com/privacy`, a Category → **Save**.
   Then flip the app to **Live**.
4. Graph API Explorer: Meta App = **DontMissPlacements**, Access Token = the **new** `WHATSAPP_TOKEN`,
   **POST** `NEW_WABA_ID/subscribed_apps` → `{"success": true}`.
5. From the recipient's phone, send `hi` to the **new** test number → you get the help menu.
   Then send `placement` → *Placement mode is ON*.

## Step 7. Keep it awake (2 min)

cron-job.org (same account) → **CREATE CRONJOB** → title `DontMissPlacements tick`, URL
`https://YOUR-NEW-APP.onrender.com/cron/tick?token=NEW_ADMIN_TOKEN`, every 5 minutes.

---

## Step 8. Try the placement features

| Do this | Expect |
|---|---|
| Forward a placement-group message with a deadline | 💼 card with company, deadline, link and *Tracking it*. Reminders at 72/48/24/12/6/3/2/1 h and 30 min before, plus nags at 10:00, 14:00, 18:00 and 21:00 until ✅ Done |
| Forward the same message again | *I already have this one* |
| Forward a poster image (or a PDF) | the same, read from the image |
| Forward a message with no deadline | card + **🔔 Nag me till done**: nags at 10:00 and 18:00 daily until done |
| *remind me to register for this company* (after forwarding) | a to-do for that company, with its link |
| *remind me to fill the Amazon form by Friday 6pm* | an aggressive 💼 placement deadline |
| `move uber to Fri 5pm` on a to-do | it becomes a real deadline |
| `placement off` / `placement on` | track everything / only placements |

If Gemini gets the placement call wrong on some mail, send me those mails as examples and I'll tune the filter
(`PLACEMENT_DEFINITION` in `app/services/decisions.py`).

## If something's off

Everything in HOW_TO_RUN's troubleshooting applies, using the **new** URL and token:
`/status?token=…`, `/admin/whatsapp-check?token=…` and Render → Logs.
