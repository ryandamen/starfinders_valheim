# Changelog

All notable changes to the Starfinders Valheim modpack will be documented in this file.

## [Unreleased]

### Added
- Server-specific configuration files and pack structure prepared for the private server
- ThunderStore release automation workflow for publishing modpack releases
- Modpack packaging and publishing flow for GitHub releases

### Changed
- Simplified workflow to publish from GitHub Release tags using the release version as the package version
- Kept the package metadata in the modpack manifest while storing only the API token as a GitHub secret

### Fixed
- Pass the modpack dependencies through the ThunderStore publishing action so they appear in the package's Required list

### Notes
- ThunderStore community is fixed to `valheim`
- Namespace is fixed to `starfinders`
- Package name is derived from the manifest
