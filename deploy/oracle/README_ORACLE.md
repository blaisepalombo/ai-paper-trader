# Oracle Cloud Always Free Deployment

This guide runs the Discord paper-trading bot on an Oracle Cloud Linux VM so it can stay online when your PC is off.

Use **paper trading keys only**.

## What You Need

- Your GitHub repo URL for this bot.
- Your Alpaca paper API key and secret.
- Your Discord bot token.
- Your Discord allowed user ID.
- An Oracle Cloud Always Free compute instance.

## Recommended Oracle VM

Use Ubuntu if Oracle offers it during instance creation.

Recommended:

- Image: Ubuntu
- Shape: Always Free eligible
- Public IP: yes
- SSH keys: let Oracle generate/download them, or paste your own public key

## On The Oracle VM

SSH into your VM, then run:

```bash
git clone YOUR_GITHUB_REPO_URL ai-paper-trader
cd ai-paper-trader
cp .env.example .env
nano .env
```

Fill in `.env` with your real values:

```text
APCA_API_KEY_ID=your_alpaca_paper_key
APCA_API_SECRET_KEY=your_alpaca_paper_secret
APCA_API_BASE_URL=https://paper-api.alpaca.markets/v2
APCA_DATA_BASE_URL=https://data.alpaca.markets/v2
DISCORD_BOT_TOKEN=your_discord_bot_token
DISCORD_ALLOWED_USER_ID=your_discord_user_id
DISCORD_REPORT_CHANNEL_ID=your_discord_channel_id
AUTO_REPORTS_ENABLED=true
REPORT_INTERVAL_SECONDS=300
```

Save nano:

- Press `Ctrl + O`
- Press `Enter`
- Press `Ctrl + X`

Then run:

```bash
bash deploy/oracle/setup_oracle_vm.sh
```

## Check Status

```bash
sudo systemctl status ai-paper-trader --no-pager
```

## View Logs

```bash
journalctl -u ai-paper-trader -f
```

## Restart Bot

```bash
sudo systemctl restart ai-paper-trader
```

## Enable Auto Reports

In Discord, type:

```text
!channelid
```

Copy the number into `.env`:

```text
DISCORD_REPORT_CHANNEL_ID=that_channel_id
AUTO_REPORTS_ENABLED=true
REPORT_INTERVAL_SECONDS=300
```

Restart:

```bash
sudo systemctl restart ai-paper-trader
```

Then test in Discord:

```text
!autotest
```

## Stop Bot

```bash
sudo systemctl stop ai-paper-trader
```

## Update Bot After GitHub Changes

```bash
cd ~/ai-paper-trader
git pull
bash deploy/oracle/setup_oracle_vm.sh
sudo systemctl restart ai-paper-trader
```

## Safety Notes

- Do not commit `.env` to GitHub.
- If `.env` ever gets uploaded, regenerate the Alpaca paper keys and Discord token.
- Keep this paper-only until the bot has been tested for a while.
