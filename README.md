# Private Telethon userbot on Render

This is a single-account userbot. It does not provide a shared login bot or collect other users' OTPs/2FA credentials. Each account owner should operate an isolated instance.

## Commands
`.help`, `.ping`, `.id`, `.cal 2+2`, `.todo text`, `.todos`, `.done 1`, `.remind 10m text`, `.reminders`, `.afk reason`, `.back`.

## Render deployment
1. Push `main.py`, `requirements.txt`, `render.yaml`, and `.gitignore` to a private GitHub repository. Do not upload `.env`, `data/`, or any `.session` file.
2. In Render, create a Blueprint from that repository. The YAML defines a background worker and a 1 GB persistent disk mounted at `/var/data`. This requires a plan that supports background workers and persistent disks; verify current pricing.
3. Set `TG_API_ID` and `TG_API_HASH` in the service's environment settings. Obtain them from https://my.telegram.org.
4. After the service is created, open its Shell feature. Run `python main.py --login`. Enter your phone number, OTP, and 2FA password only into this private shell prompt. This creates the session on `/var/data`.
5. Restart the worker. It should connect using the existing session.

If the Shell feature is unavailable on your plan, do not use a public Telegram bot to collect OTPs or session files. Use a supported secure initialization method or a plan that provides Shell access.

## Security
- The `.session` file is a credential; anyone who gets it may access your Telegram account.
- Keep the persistent disk private and restrict service access.
- If leaked, revoke the session in Telegram Settings > Devices.
- Avoid unsolicited or high-volume automation; Telegram may rate-limit or restrict accounts.
