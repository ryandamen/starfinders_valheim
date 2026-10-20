# starfinders_valheim
Modpack for the Starfinders private Valheim server.

## Set up

### GitHub secret

- `THUNDERSTORE_API_TOKEN` your ThunderStore API token
- `DISCORD_WEBHOOK_URL` your discord bots webhook url

### Configuration

The dependancies for the mods are declared in the `Publish to ThunderStore` step's `deps` input. The format for adding mods is:

```bash
<creator>-<mod_name>@<version>

# Example:
denikson-BepInExPack_Valheim@5.4.2351
```

This action replaces the dependency list in `thunderstore.toml` so that one can be ignored.

## Initiating the workflow
Create a version tag and push it:

```bash
VERSION=3.2.3
git tag $VERSION && git push origin $VERSION
```

The workflow uses the tag name as the package version, so `2.0.1` becomes the ThunderStore package version.

## Workflow

The workflow runs on GitHub Actions whenever a tag is pushed. The tag name is used as
the package version.

Pipeline steps:

1. **Check out the repository** so the workflow can package its contents.
2. **Build the package and publish to Thunderstore website** uses the
   `THUNDERSTORE_API_TOKEN` secret to push to our team.
5. **Wait for availability in Thunderstore.**  There is a delay between the package appearing on the website and the app so it checks the Valheim Thunderstore package feed every 5 minutes (max 4 hours) until the version is available in the Tunderstore app.
6. **Create a GitHub Release** from the tag.
7. **Notify Discord** that the package is available, with links to the Thunderstore package and GitHub Release. This step uses the `DISCORD_WEBHOOK_URL` secret to send the message to Discord.

