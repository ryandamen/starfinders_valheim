# starfinders_valheim
Modpack for the Starfinders private Valheim server.

## Set up

### GitHub secret

- `THUNDERSTORE_API_TOKEN` your ThunderStore API token
- `DISCORD_WEBHOOK_URL` the Discord webhook for completion/online notices
- `DISCORDBOT_AWL_WEBHOOK_URL` the AWL command-channel webhook used for
  restart countdown and save messages
- `TCADMIN_API_KEY` a TCAdmin API key with permission to read and restart this
  service
- `TCADMIN_SERVICE_ID` the numeric TCAdmin service ID for the Valheim server
- `SFTP_USERNAME` the username for the SFTP account
- `SFTP_PASSWORD` the password for the SFTP account
- `SFTP_KNOWN_HOSTS` a verified OpenSSH `known_hosts` entry for the SFTP
  server, including its non-default port

`SFTP_KNOWN_HOSTS` SFTP server's verified host key/
the output format from `ssh-keyscan -p 8822 23.111.240.210`.

### Configuration

Declare direct mod dependencies and their exact versions under
`[package.dependencies]` in `thunderstore.toml`.

Running the deployment script directly performs a live package transfer and
server restart. Use it only when the restart is intended and all required
environment values are configured.

```powershell
python .github/scripts/update_valheim_server.py --version 0.0.1
```

## Initiating the workflow
Create a version tag and push it:

```bash
git tag --sort=-version:refname | head -n 2
# pick a version thats higher then the latest one e.g.
VERSION=4.2.0
git tag $VERSION && git push origin $VERSION
```

The workflow uses the tag name as the package version, so `2.0.1` becomes the ThunderStore package version.

## Workflow

See the [deployment workflow diagram](docs/workflow.mmd)


### DiscordBot_AWL configuration

1. In Discord Developer Portal, enable the bot's **Message Content Intent**.
   Keep its token on the Valheim server only; the GitHub workflow does not use
   the bot token.
2. Create or choose the Discord channel where AWL reads commands. Enable
   Developer Mode in Discord and copy that channel's numeric ID.
3. In `BepInEx/config/RustyMods.DiscordBot.cfg`, configure:
   - `[4 - Commands] Channel ID` to the chosen channel ID.
   - `[4 - Commands] Discord Admin` to include `OnlyMods`.
   - `[5 - Setup] BOT TOKEN` with the server's Discord bot token.
4. Give the AWL bot permission to view the channel and read its message history.
5. To allow `!message` and `!save` from the workflow webhook, configure AWL's
   trusted incoming webhooks on the server in `[4 - Commands]`:
   - `Allow Trusted Webhooks = On`
   - `Trusted Webhooks` to
     `<webhook-id>:<command-channel-id>:<guild-id>`
   - `Trusted Webhook Command Allowlist = !message,!save`
   The webhook ID is the numeric ID in the webhook URL; keep the full URL/token
   secret. The channel ID must match `[4 - Commands] Channel ID`. This trusted
   webhook setting is separate from `Discord Admin`, which is for human users.
   The update workflow uses `DISCORDBOT_AWL_WEBHOOK_URL` for countdown, save,
   and restart messages.
