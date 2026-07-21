# AI Paper Trading Bot

This is a beginner starter bot for Alpaca Paper Trading only.

It is designed to act like a tiny `$50` experiment account even if the Alpaca paper account shows `$100,000`.

## Safety Rules

- Paper trading only.
- Blocks live Alpaca endpoints.
- Starts with human approval before any order.
- Uses fractional shares with a tiny dollar amount.
- No options, futures, margin, or leverage.
- Logs every run to `trade_log.csv`.

## Setup

1. Install Python from `python.org` if you do not already have it.
2. Put this folder somewhere easy, like your Desktop.
3. Copy `.env.example` and rename the copy to `.env`.
4. Open `.env` and replace the fake values with your Alpaca paper keys.
5. Do not share your `.env` file with anyone.

## Run It

Open PowerShell in this folder and run:

```powershell
python paper_bot.py
```

The bot will show the account status and ask before submitting a paper order.

To check status without placing an order:

```powershell
python paper_bot.py status
```

## First Test

For the first test, use:

- Symbol: `SPY`
- Dollars: `5`

If the market is closed, the order may be rejected. That is okay. It will still prove the bot can talk to Alpaca.

## Version 2 Safety Upgrades

The bot now:

- Shows market open/closed status.
- Shows open positions.
- Shows open orders.
- Stops if there is already an open order.
- Stops if it already submitted an order today.
- Stops if the market is closed.
- Picks a simple default symbol from the allow-list.

## Version 3 Report Upgrades

The status check now creates `daily_report.txt` with:

- Open order status.
- Recent order history.
- Open position value.
- Current paper profit/loss.
- A plain-English next action.

Use `check_status_windows.bat` whenever you want to check the bot from your PC without placing a trade.

## Discord Phone Control

Discord control lets you check the bot and stage/approve tiny paper trades from your phone.

Important: do not paste your Alpaca keys or Discord bot token into Discord or ChatGPT.

### Discord Setup

1. Go to the Discord Developer Portal.
2. Create a new application.
3. Add/create a bot user.
4. Copy the bot token and put it in `.env` as `DISCORD_BOT_TOKEN`.
5. Enable the bot's Message Content Intent.
6. Invite the bot to a private server/channel where only you can use it.
7. Run `install_discord_windows.bat` once.
8. Run `run_discord_bot_windows.bat`.
9. In Discord, type `!whoami`.
10. Copy the ID it gives you into `.env` as `DISCORD_ALLOWED_USER_ID`.
11. Restart `run_discord_bot_windows.bat`.

### Discord Commands

```text
!help
!whoami
!channelid
!status
!brief
!pnl
!journal
!recap
!suggest
!analyze
!trade SPY 5
!approve CODE
!cancel
!cancelorder
!cancelorder all
!autotest
```

Trades still require a two-step flow:

1. `!trade SPY 5`
2. `!approve CODE`

This keeps the bot from placing a trade from one accidental message.

## Strategy Brain v1

The `!analyze` command checks this watchlist:

```text
SPY, QQQ, AAPL, MSFT, NVDA
```

It gives each symbol a simple 0-3 trend score:

- Price above 5-day average.
- 5-day average above 20-day average.
- Latest close is above the prior close.

It will still say `WAIT` if:

- The market is closed.
- There is already an open order.
- There is already an open position.
- The bot already submitted a trade today.
- No symbol scores 3/3.

If conditions are clean and a symbol scores 3/3, it suggests the exact Discord command to stage the paper trade. It does not place the trade automatically.

## GitHub + Koyeb Hosting

GitHub and Koyeb work together:

- GitHub stores the code.
- Koyeb runs the code 24/7 from GitHub.
- Koyeb stores your secret keys as environment variables.

Do not upload `.env` to GitHub. The `.gitignore` file blocks it.

### Files Added For Koyeb

```text
Procfile
runtime.txt
koyeb.yaml
.gitignore
```

The Discord bot also starts a tiny health server so Koyeb can confirm the app is alive.

### Environment Variables For Koyeb

Add these in Koyeb's environment/secrets section:

```text
APCA_API_KEY_ID
APCA_API_SECRET_KEY
APCA_API_BASE_URL=https://paper-api.alpaca.markets/v2
APCA_DATA_BASE_URL=https://data.alpaca.markets/v2
DISCORD_BOT_TOKEN
DISCORD_ALLOWED_USER_ID
```

Keep using paper keys only.

### Koyeb Start Command

If Koyeb asks for a run/start command, use:

```text
python discord_control.py
```

If it asks for a port, use:

```text
8000
```

## Oracle Cloud Always Free

If Koyeb asks for payment, use Oracle Cloud Always Free instead.

Oracle runs the bot on a tiny Linux VM. See:

```text
deploy/oracle/README_ORACLE.md
deploy/oracle/setup_oracle_vm.sh
```

## Version 5 Auto Reports

The bot can send short Discord updates automatically when market/order/position state changes.

Add these to `.env`:

```text
DISCORD_REPORT_CHANNEL_ID=your_channel_id
AUTO_REPORTS_ENABLED=true
REPORT_INTERVAL_SECONDS=300
DAILY_RECAP_ENABLED=true
AUTO_ANALYZE_AT_OPEN=true
STOP_LOSS_PCT=3
TAKE_PROFIT_PCT=5
```

To get the channel ID, type this in Discord:

```text
!channelid
```

After adding the channel ID on Oracle, restart the bot:

```bash
sudo systemctl restart ai-paper-trader
```

Then test:

```text
!autotest
```

## Version 6 Fill Alerts And Recaps

The bot now watches for order and position changes and sends clearer Discord alerts:

- `Market opened.`
- `Market closed.`
- `Order filled: ...`
- `New open position: ...`
- `Position no longer open: ...`

It also sends one daily recap after it sees the market change from open to closed.

You can request the same recap manually:

```text
!recap
```

## Version 7 Control And Risk Alerts

The bot now has more phone controls:

```text
!pnl
!journal
!cancelorder
!cancelorder all
```

What changed:

- `!cancelorder` requests cancellation of the newest open Alpaca paper order.
- `!cancelorder all` requests cancellation of all open Alpaca paper orders.
- `!journal` shows the local bot trade log plus recent Alpaca orders.
- `!pnl` shows virtual capital, open market value, open unrealized P/L, and estimated experiment value.
- When the market opens, the bot can automatically run `!analyze` and post the result.
- The bot sends stop-loss and take-profit alerts based on `.env` percentages.

These risk alerts do not sell anything automatically. They are notification-only.


## SQLite trading memory

The bot automatically creates a durable SQLite database at:

```text
~/ai-paper-trader-data/trading_bot.db
```

This location is outside the Git repository, so auto-deploys do not erase it.
Set `TRADING_DB_PATH` to use a different location. The database stores scan
decisions, trade events, and a daily account snapshot. Use `!stats` in Discord
to confirm that records are being collected. SQLite is built into Python, so no
extra package or database server is required.
