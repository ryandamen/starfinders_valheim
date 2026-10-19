# starfinders_valheim
Modpack for the Starfinders private Valheim & Friends server.

## ThunderStore publishing
This repository publishes to ThunderStore automatically when a git tag is pushed.

### Required GitHub secret
Create this repository secret in GitHub:

- `THUNDERSTORE_API_TOKEN` — your ThunderStore API token

### Release process
Create a version tag and push it:

```bash
VERSION=2.3.2
git tag $VERSION
git push origin $VERSION
```

The workflow uses the tag name as the package version, so `2.0.1` becomes the ThunderStore package version.

The required mods are passed through the publishing action's `deps` input in the workflow. The action replaces the dependency list in `thunderstore.toml`, so keep that input in sync with the manifest.

The workflow publishes the package and then creates a GitHub Release for the same tag if the upload succeeds.